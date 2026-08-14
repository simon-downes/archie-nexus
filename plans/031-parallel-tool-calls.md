# 031 — SPEC: Parallel Tool Calls

## Objective

Update the main agent loop so independent tool calls requested in a single LLM response execute concurrently instead of sequentially.

The change applies to every tool call dispatched through `run_loop()`, including native tools, `exec`, and future tools such as `task`. It is a loop-level execution change only; it does not introduce subagents, tool-specific fan-out, new wire events, or a concurrency configuration surface.

## Context

`archie-nexus/agent/src/archie_agent/loop.py` currently collects all `ToolUseBlock`s from one provider response, emits one `ToolCall` event per block, then awaits `execute_tool(block)` in input order. Results are accumulated into one following `Turn(role="user", content=[ToolResultBlock, ...])` as required by the provider protocol.

Plan 028 (`plans/028-native-subagent-tool.md`) currently describes parallelism only inside the future `task` handler. That would leave the main loop sequential and make concurrency a special property of subagent dispatch. This plan moves the reusable primitive into `run_loop()`: when a model returns multiple tool calls in one response, all eligible calls start without waiting for siblings, and the loop resumes only after the batch has produced a result for every call.

Existing contracts that remain authoritative:

- Provider tool calls and results are correlated by `tool_use_id`.
- All results from one assistant response are sent in one subsequent user turn.
- Canonical persisted events associate each tool call/result with its request and `tool_use_id`; this plan does not change their schema.
- `ToolResultBlock` ordering in the model-facing history is deterministic and follows the model's tool-call order.
- A raised tool handler is converted into an error result and does not fail sibling calls or the whole turn.

## Requirements

### Parallel dispatch

- MUST execute all tool calls from one LLM response concurrently when `execute_tool` is provided.
  - AC: two instrumented handlers overlap in time; the second starts before the first completes.
- MUST not add a concurrency limit to `run_loop()` in this plan.
  - The loop dispatches the complete batch. Tool-specific limits, such as a future subagent semaphore, belong to the tool implementation.
- MUST preserve the existing single-tool behavior.
  - AC: one tool call has the same result, events, history, and error behavior as before.
- MUST preserve the order of `ToolResultBlock`s in the next user turn according to the order of `result.tool_use_blocks`, regardless of completion order.
  - AC: a slower first call and faster second call still produce `[result_1, result_2]` in history.

### Events and observability

- MUST emit one `ToolCall` event for each requested tool before tool execution begins, in the
  input order of `result.tool_use_blocks`.
  - AC: the `ToolCall` event sequence has the same tool-use IDs and order as the provider response.
- MUST emit one `ToolResult` event for each completed or cancelled tool call.
- MUST correlate every result event and result block with the original `tool_use_id`.
- MAY yield `ToolResult` events in completion order, because live clients benefit from showing completed work immediately; the model-facing result block list MUST remain input-ordered.
- MUST NOT add or change wire event types, canonical event fields, protocol version, or session log schema.
- MUST leave request usage, request completion, and canonical event accounting unchanged.

### Error isolation

- MUST catch exceptions from each individual `execute_tool` coroutine and convert them to an error `ToolResultBlock`, matching the current error content format (`<ExceptionType>: <message>`).
- MUST allow sibling calls to complete when one call raises.
  - AC: one failing call and two successful calls produce three results and the next LLM request occurs.
- MUST not let cancellation of one asyncio task implicitly cancel unrelated sibling tool calls, unless the whole turn is interrupted.

### Interrupt behavior

- MUST preserve the existing user-visible semantics of an untargeted turn interrupt: no next LLM request is made and `TurnInterrupted` is yielded.
- On interruption before the batch starts, MUST synthesize a cancelled error result for every requested tool and repair history with a result for every tool use.
- On interruption while the batch is running, MUST signal cancellation to all in-flight tool tasks and await their termination before yielding `TurnInterrupted`.
- MUST synthesize cancelled results for calls that did not produce a normal result, so every assistant `ToolUseBlock` has a matching `ToolResultBlock` in `working_messages`.
- MUST not assume that setting the shared `threading.Event` cancels arbitrary tool handlers. The loop should cancel its asyncio tasks and then collect their outcomes; tool implementations remain responsible for stopping any underlying subprocess or I/O when their own cancellation contract supports it.
- MUST avoid leaking background tasks after `run_loop()` returns.

### Compatibility

- MUST retain the public `run_loop()` signature unless implementation requires a backwards-compatible optional parameter.
- MUST support existing synchronous test doubles and async `execute_tool` callables through the current `Awaitable[ToolResultBlock]` contract.
- MUST leave provider adapters unchanged; batching and provider-specific message translation already support multiple tool blocks.
- MUST leave TUI grouping unchanged: all tool calls from one LLM response remain in one iteration block.

## Non-goals

- No native subagent or `task` tool implementation.
- No bounded semaphore or global tool concurrency configuration.
- No parallel execution of separate LLM requests or separate `run_loop()` instances.
- No reordering of tool calls before dispatch.
- No dependency-aware scheduling between tool calls. Calls in one model response are treated as independent because the model requested them together.
- No changes to `ToolSpec`, `ToolRegistry`, provider adapters, wire protocol events, canonical events, session accounting, or persistence formats.
- No targeted interrupt protocol. The existing interrupt applies to the whole active turn.

## Design

### Dispatch shape

Replace the sequential loop in `run_loop()` with a private batch helper in `loop.py`. The helper
must expose completions incrementally rather than return only after collecting the whole batch; this
allows `run_loop()` to yield live `ToolResult` events as calls finish while separately retaining an
input-ordered result map for history. A suitable shape is an async generator such as:

```python
async def _execute_tool_batch(
    blocks: list[ToolUseBlock],
    execute_tool: Callable[[ToolUseBlock], Awaitable[ToolResultBlock]],
    interrupt: threading.Event,
) -> AsyncGenerator[tuple[int, ToolResultBlock], None]:
    ...
```

The exact private interface may differ, but it MUST provide both behaviours: incremental completion
notification `(input_position, result)` and a complete input-ordered result collection available to
`run_loop()` after the batch finishes. Because an async generator cannot return a useful value to its
caller, `run_loop()` should own the result map (or the helper should return a separate collection
object after consuming an internal completion queue); the plan does not require an async-generator
return value. Responsibilities:

1. Check the shared interrupt before creating work.
2. Create one asyncio task per block.
3. Wrap each task in an exception-isolating worker that returns a normalized result rather than raising.
4. Watch the threading event through the explicit asyncio-side interrupt bridge described below.
5. Yield each completed result as soon as it is available, including its input position.
6. Map results by position and fill missing positions with cancelled/error blocks during cleanup.
7. Ensure all tasks and the interrupt watcher are awaited before returning. Normal task cancellation
   MUST complete through asyncio cancellation; if a tool suppresses `CancelledError` and remains
   pending, cleanup MUST await it rather than leak it, and MUST log a warning if a bounded cleanup
   timeout is needed. The timeout MUST NOT cause an orphaned task to survive `run_loop()`.

A reasonable implementation uses `asyncio.create_task()` for workers, `asyncio.as_completed()` or a
completion queue for live results, and a list/map keyed by input position for deterministic history
assembly. The implementation must not use `list.index(block)` to identify work: duplicate or
equal-looking blocks must remain independently addressable by position and `tool_use_id`.

### Result normalization

Every worker returns a result for exactly one input block:

- Normal handler return: use the returned `ToolResultBlock`.
- Handler exception: create an error block with the input `tool_use_id`.
- Interrupt/cancellation before normal completion: create a cancelled error block with the input `tool_use_id`.

The helper must ensure the final result list has exactly one block per input block. If a cancellation or unexpected task failure prevents a worker from returning, fill that position with a cancelled/error block.

A handler result with a `tool_use_id` different from its input block is invalid for this dispatch. The
loop MUST replace it with an error `ToolResultBlock` using the input block's ID, preserving the
correlation invariant and reporting the mismatch in the error content. It MUST never append a
result under an unrelated ID or silently lose the requested call.

### Interrupt and task cleanup

> **Revision (post attempt-1 abort) — AUTHORITATIVE.** The original design mandated a busy-poll
> watcher (`while not interrupt.is_set(): await asyncio.sleep(0.05)`). That approach is **rejected**:
> in the first implementation it was the prime suspect for a turn hanging between the last
> `ToolResult` and the next iteration, and for leaked tasks under cleanup. The requirements below
> replace it. Do not reintroduce a polling watcher.

The interrupt MUST be delivered to the tool batch through an **event-driven asyncio bridge**, not by
polling the shared `threading.Event` from inside the loop.

- The harness owns an `asyncio.Event` (the "async interrupt") created on the running loop when a turn
  starts. When `interrupt()` is called (from any thread), the harness MUST set the shared
  `threading.Event` (existing behavior, used by provider streaming) AND schedule the async event via
  `loop.call_soon_threadsafe(async_interrupt.set)`. It MUST also continue to cancel the active tool
  subprocess (`_cancel_active_proc()`); killing the process group is what makes an in-flight tool
  return, since cancelling the asyncio task alone does not stop a subprocess.
- `run_loop()` MUST accept the async interrupt as a backwards-compatible optional parameter and pass
  it to the batch helper. When no async interrupt is supplied (e.g. direct unit-test callers),
  `_execute_tool_batch` MUST create its own `asyncio.Event`; such a locally-created event is only set
  via the harness bridge, so tests that pre-set the `threading.Event` before dispatch still take the
  "interrupt before batch" path via the initial `interrupt.is_set()` check.
- `_execute_tool_batch` MUST race the set of worker tasks against `async_interrupt.wait()` using a
  single `asyncio.wait(..., return_when=FIRST_COMPLETED)`. There MUST be no separate watcher task and
  no `asyncio.sleep` polling loop.
- The `async_interrupt.wait()` task created for the race MUST be cancelled and awaited in a `finally`
  so it never outlives the batch.

Cancellation handling must be structured:

- Cancel pending tasks when interruption is observed.
- Await all tasks with `return_exceptions=True`.
- Convert missing results to cancelled blocks.
- Append the assistant turn and complete tool-result repair before yielding `TurnInterrupted`.
- Do not yield a second `ToolResult` for a result already emitted before cancellation.
- Do not leave task references or callbacks running after the helper returns.

The existing `_stream_once()` provider worker remains unchanged. The shared interrupt still stops provider streaming; this plan extends interruption handling to the tool batch.

### Event ordering

The following ordering is required. `_stream_once()` only populates the request result;
`run_loop()` emits `RequestFinished` (when configured) and `Usage` after `_stream_once()` returns,
before dispatching the tool batch:

```text
IterationStart
[RequestFinished, when request accounting is configured]
[Usage, when provider usage is available]
ToolCall(tool_1)
ToolCall(tool_2)
...
ToolResult(completion order, live)
...
TurnComplete                  # only after the next terminal LLM request
```

For interruption, all emitted tool results precede `TurnInterrupted`. The persisted/model-facing repaired history is always:

```text
assistant: [text blocks, tool_1, tool_2, ...]
user:     [result_1, result_2, ...]
```

where result order matches tool-call order, not completion order.

## Files and interfaces

### Primary implementation

- `agent/src/archie_agent/loop.py`
  - Add parallel batch execution helper(s).
  - Replace the sequential `for block` execution path in `run_loop()`.
  - Preserve error conversion, history repair, event emission, and interruption behavior.

### Tests

- `tests/test_loop.py`
  - Extend the existing batched-tool tests and add concurrency, ordering, error, interruption, cancellation-cleanup, and duplicate-call coverage. This is the established home for pure `run_loop()` tests and already contains `test_two_tools_batched` plus interruption/error cases.
- `tests/test_harness.py` and `tests/test_ws_integration.py`
  - Run as regression coverage for harness event consumption and wire ordering. No existing test
    should assert sequential wall-clock execution; verify this while implementing and add a focused
    regression only if needed.

No other production files should need modification. If a test double needs instrumentation, keep that support local to the test or `FakeLLMClient`; do not expand public production interfaces unnecessarily.

## Milestones

### 1. Define and test the batch contract

Approach:

- Confirm the existing `run_loop()` and provider contracts with focused tests.
- Add test helpers that record active handler count, start/end order, and cancellation state.
- Establish the expected distinction between live `ToolResult` event order and history result order.

Tasks:

- Add a two-tool test whose handlers block on independent events.
- Assert both handlers are active concurrently before either is released.
- Assert the existing single-tool and batched-history expectations remain explicit.

Edge Cases:

- Interrupt already set before dispatch → do not start handlers; synthesize one cancelled result per block.
- Handler completes while interrupt is observed → retain its normal result; cancel and synthesize results only for missing positions.

Deliverable: failing tests that precisely describe parallel dispatch and deterministic result assembly.

### 2. Implement concurrent tool execution

Approach:

- Add a private batch helper using asyncio tasks.
- Run all blocks from the current response concurrently.
- Isolate per-tool exceptions and collect one normalized result per block.
- Reconstruct the result block list in input order.

Tasks:

- Replace the sequential dispatch loop in `run_loop()`.
- Keep `ToolCall` emission before any tool starts.
- Emit `ToolResult` events as results become available, without changing their correlation.
- Keep the existing next-iteration and terminal behavior.

Edge Cases:

- Handler raises → normalize only that position to an error result; continue all siblings.
- Handler returns a mismatched `tool_use_id` → replace it with an error result using the requested ID.
- Completion order differs from request order → emit live results as completed, but append history in request order.

Deliverable: multiple model-requested tools execute concurrently and the next LLM request receives one ordered result batch.

Verify:

```bash
uv run pytest tests/test_loop.py -q
```

### 3. Preserve failure and interruption guarantees

Approach:

- Add structured cancellation and cleanup around the batch.
- Treat exceptions and cancellation as per-tool outcomes where possible.
- Retain the repaired transcript invariant on every interrupted batch.

Edge Cases:

- Interrupt before worker creation → all requested calls receive cancelled results.
- Interrupt during execution → cancel all pending workers, await cleanup, emit at most one result per call, then yield `TurnInterrupted`.
- A worker suppresses cancellation → log the cleanup failure and preserve the no-leaked-task invariant before returning.

Tasks:

- Test one failing sibling does not prevent successful siblings or the next LLM request.
- Test interruption before dispatch produces cancelled results for all calls.
- Test interruption during concurrent execution cancels/awaits all tasks, emits no orphaned results, and yields `TurnInterrupted`.
- Test no asyncio tasks remain pending after the loop exits.
- Test duplicate/equal tool inputs still produce distinct results by position and ID.

Deliverable: parallel execution is safe under errors, interrupts, and cancellation.

Verify:

```bash
uv run pytest tests/test_loop.py tests/test_harness.py tests/test_ws_integration.py -q
uv run ruff check agent/src/archie_agent/loop.py tests/test_loop.py
```

### 4. Integrate plan/dependency documentation

Tasks:

- Mark this spec's implementation status in the plan progress convention used by the repository, if a progress file is created.
- Update plan 028 so its former Milestone 6 uses the main-loop parallel dispatch from plan 031 and retains only subagent-specific bounded fan-out, child registry, and targeted cancellation work.
- Ensure 028 no longer describes `run_loop` as intentionally sequential once this plan is implemented.
- Run the full test suite and formatter/linter for touched files.

Deliverable: plan 028 and plan 031 have non-overlapping responsibilities.

Verify:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check agent/src/archie_agent/loop.py tests/test_loop.py
```

## Acceptance summary

The spec is complete when:

1. A model response containing N tool calls starts all N handlers concurrently.
2. The model receives exactly one subsequent user turn containing N results in original request order.
3. Live result events remain correctly correlated and may reflect completion order.
4. One handler failure becomes one error result without cancelling successful siblings.
5. Interrupting a batch leaves no orphaned tool uses, no leaked tasks, and yields the existing `TurnInterrupted` event.
6. Single-tool behavior, provider adapters, wire protocol, canonical events, and session accounting remain backward compatible.
7. Plan 028 references this shared loop capability rather than owning a duplicate generic fan-out implementation.

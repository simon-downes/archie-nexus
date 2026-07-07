# 006 — Agent Loop Extraction & Session Log Split

## Objective

Factor the agent loop into a pure, testable core function separated from I/O
concerns (WebSocket broadcast, persistence, turn lifecycle). Switch the session
log from per-exchange entries to per-message entries for crash resilience and
natural tool-call representation. Introduce a `FakeLLMClient` for deterministic
testing without mocking boto3 internals.

## Context

Triggered by the tau architecture comparison which revealed that
`AgentLoop.handle_message()` (agent.py, ~130 lines) mixes five concerns:
LLM orchestration, session state mutation, WebSocket broadcasting, thread
management, and JSONL persistence. This makes testing require full mock stacks,
makes adding features (tool dispatch, steering messages, context management)
fragile, and couples the loop to the current sync-thread-to-async-queue bridge.

Additionally, the session log writes one JSONL line per complete exchange
(user + assistant bundled in `SessionLogEntry`). A crash mid-response loses the
user's prompt entirely. Future tool-calling requires representing multi-step
sequences that don't fit a single `{user, assistant}` line.

The tau project demonstrates: pure loop function → stateful harness → I/O layer,
with per-message persistence. We adopt these structural patterns while keeping
our container deployment model and wire protocol.

## Requirements

### Pure run loop

- MUST provide an async generator `run_loop()` that yields agent-level events
- MUST NOT import or reference WebSocket, Starlette, persistence, or session state
- MUST accept: messages list (`list[Turn]`), system prompt, LLM client, interrupt
  signal (`threading.Event`)
- MUST handle the sync→async thread bridge internally (the LLM client is sync;
  callers don't care about the bridge mechanism)
- MUST check the interrupt signal between yields
- MUST yield one of: `TextChunk`, `TurnUsage`, `TurnDone`, `TurnFailed`,
  `TurnInterrupted`
- MUST translate internal `StreamEvent`s (provider-shaped) to agent events
  (provider-neutral) at this boundary
- MUST NOT mutate the messages list (caller owns state)
- MUST close the LLM sync generator on interrupt (releases HTTP connection)
- MUST join the worker thread with a timeout and log a warning if it doesn't exit
- MUST propagate worker errors across the thread boundary via the queue (not
  instance state)

### Agent events

- MUST define agent-level event types in a dedicated module
- MUST be separate from wire protocol events (different lifecycle, different
  audience)
- `TurnUsage` MUST carry per-request token counts (NOT cumulative totals — the
  harness accumulates)
- Types: `TextChunk(text)`, `TurnUsage(input_tokens, output_tokens,
  cache_read_tokens, cache_write_tokens)`, `TurnDone(stop_reason)`,
  `TurnFailed(error)`, `TurnInterrupted`

### Harness

- MUST own: session (in-memory transcript), turn lifecycle (`_turn_active` flag,
  interrupt), persistence (calls `write_entry`), wire event translation and
  broadcast
- MUST reject concurrent messages (turn already active → `TurnError` broadcast)
  before invoking the loop
- MUST persist the user message immediately on receipt (before streaming begins)
- MUST persist the assistant message on completion (or partial text on interrupt)
- MUST translate agent events → wire protocol events and broadcast to connected
  WebSocket clients
- MUST accumulate token totals on session from `TurnUsage` events via
  `session.record_usage()`
- MUST compute per-message cost via `calculate_cost(session.model.cost, ...)`
  using the `TurnUsage` tokens (NOT `session.total_cost` which is cumulative)
- MUST compute `session.total_cost` and `session.context_pct` for `UsageUpdated`
  wire events (these ARE cumulative — for the status bar)
- MUST maintain the same external API surface that `app.py` consumes (clients set,
  `handle_message`, `interrupt`, `turn_active`, `session`)

### Per-message session log

- MUST replace `SessionLogEntry` (per-exchange) with `MessageEntry` (per-message)
- MUST remove `EntryMetadata`, `ToolCall` from `shared/session/log.py`
- MUST remove these symbols from `shared/session/__init__.py` imports AND `__all__`
- Each JSONL line: `{id, when, role, content, metadata?}`
- `role`: `"user"` | `"assistant"` (later: `"tool_result"`, `"tool_call"`)
- User entries: `metadata` is None (minimal — just id, when, role, content)
- Assistant entries: `metadata` populated (model, backend, tokens, cost,
  interrupted)
- `metadata.cost` is **per-message** cost (computed from that message's tokens),
  NOT cumulative session cost
- User message MUST be persisted before LLM streaming starts (crash-safe)
- Assistant message MUST be persisted after streaming completes
- On interrupt with zero assistant text: persist with `content=""` and
  `metadata.interrupted=True` (intentional — consistent, queryable)
- MUST NOT break existing test infrastructure (old log files are inert — no
  reader exists; new sessions get new format)
- MUST retain `write_entry(path, entry)` signature (append one JSONL line)

### FakeLLMClient

- MUST implement the `LLMClient` protocol (same `stream()` and `invoke()`
  signatures, including `tool_config` parameter)
- MUST yield scripted `StreamEvent` sequences (configurable per test)
- MUST support configurable delay between events (for interrupt testing)
- MUST replace the current `_make_mock_llm` / `MagicMock` pattern in all agent
  tests
- MUST be importable from `archie_agent.llm.fake`

### Session (trimmed)

- MUST retain: `Turn`, `Session` class with `turns` list, token accumulators,
  `add_turn()`, `context_pct`, `context_warning`, `total_cost`, `turn_index`,
  `next_turn_index()`, `model_id`, `model` (ModelEntry)
- MUST add `record_usage(input_tokens, output_tokens, cache_read_tokens,
  cache_write_tokens)` method that updates all four accumulators AND
  `_last_input_tokens` in one call. Keeps context-window logic encapsulated
  inside Session (where `context_pct` lives).
- MUST remove: `flush_turn()`, `TurnLog`, `log_path`, `_log_dir` — persistence
  moves to harness
- `session.model` is the **single authoritative source** for cost rates and
  backend name. The harness reads `session.model.cost` for cost computation and
  `session.model.provider.name` for backend.

### Integration

- MUST update `app.py` to construct the harness (not `AgentLoop`)
- MUST preserve all existing endpoint behaviour (`/status`, `/history`, `/stream`)
- MUST NOT change the wire event vocabulary or WebSocket protocol
- MUST update all tests to use `FakeLLMClient` instead of `MagicMock` patterns
- MUST delete `agent.py` after harness replaces it

## Technical Design

### Dependencies

No new third-party dependencies. All types use stdlib `dataclass` (agent events)
and `msgspec.Struct` (log schema, already present).

### Module layout (agent package after this plan)

```
agent/src/archie_agent/
├── app.py          (Starlette endpoints, lifespan — constructs harness)
├── harness.py      (NEW — AgentHarness: state + I/O + persistence)
├── loop.py         (NEW — pure run_loop async generator)
├── events.py       (NEW — agent-level event dataclasses)
├── session.py      (TRIMMED — in-memory transcript only)
├── cli.py          (unchanged — agent CLI entry point)
├── llm/
│   ├── __init__.py (LLMClient protocol — unchanged)
│   ├── _types.py   (StreamEvent — unchanged)
│   ├── bedrock.py  (BedrockClient — unchanged)
│   └── fake.py     (NEW — FakeLLMClient)
```

`agent.py` is deleted (replaced by `harness.py` + `loop.py`).

### Agent events (`events.py`)

Simple dataclasses. Union type `AgentEvent` for type annotations. These are the
boundary between the pure loop and the stateful harness — provider-neutral,
carrying per-request (not cumulative) data.

### Pure loop (`loop.py`)

`run_loop()` is an `AsyncGenerator[AgentEvent, None]`. It:
1. Spawns the sync LLM `stream()` call in a daemon thread (passing
   `tool_config=None` — the parameter exists on `LLMClient.stream()` already)
2. Bridges events to async via `asyncio.Queue` + `call_soon_threadsafe`
3. Drains the queue, translating `StreamEvent` → `AgentEvent`
4. Checks `interrupt.is_set()` between each dequeue
5. On interrupt: the worker thread closes `gen.close()` (releases HTTP connection
   on the Bedrock EventStream), then pushes a sentinel; the drain loop yields
   `TurnInterrupted`
6. On exception in thread: worker pushes an error sentinel wrapping the exception
   message (a `_WorkerError(msg)` dataclass pushed onto the queue — no instance
   state crossing the boundary); drain loop yields `TurnFailed(error=msg)`
7. **Bedrock ordering:** `Done` does NOT terminate the drain. The loop records
   `stop_reason`, continues draining until the worker's sentinel arrives (so
   trailing `Usage` events are captured). After sentinel, yields any pending
   `TurnUsage`, then yields `TurnDone(stop_reason)` last.
8. After the drain loop exits (any path): `thread.join(timeout=5.0)` + warning
   if still alive.

The thread/queue bridge is an implementation detail — if we switch to an async
LLM client in future, only this function changes.

**Unknown StreamEvent types** (`ToolUseStart`, `ToolUseEvent`): logged at debug
level, not yielded. No error, no crash.

**Field name mapping (Usage → TurnUsage):**
- `Usage.input_tokens` → `TurnUsage.input_tokens`
- `Usage.output_tokens` → `TurnUsage.output_tokens`
- `Usage.cache_read_input_tokens` → `TurnUsage.cache_read_tokens`
- `Usage.cache_write_input_tokens` → `TurnUsage.cache_write_tokens`

The `_input_` infix is a Bedrock-ism dropped at this boundary.

### Harness (`harness.py`)

`AgentHarness` replaces `AgentLoop`. Same external surface for `app.py`:
- `session` attribute (the `Session` instance)
- `clients: set[WebSocket]`
- `turn_active: bool`
- `handle_message(content: str) -> None`
- `interrupt() -> None`

Constructor: `session`, `llm_client`, `system_prompt`, `log_dir`.
No separate `model` parameter — the harness reads `session.model` for cost rates
and backend name (single authoritative source, no drift risk).

Internally `handle_message`:
1. Guards concurrent turns (`_turn_active`) — broadcasts `TurnError` if active
2. Increments turn index
3. Persists user `MessageEntry` via `write_entry(log_path, ...)`
4. Adds user turn to session transcript (`session.add_turn`)
5. Instantiates `run_loop(messages=session.turns, system=..., llm=..., interrupt=...)`
6. Consumes `AgentEvent`s:
   - `TextChunk` → accumulate text + broadcast `TextDeltaEvent`
   - `TurnUsage` → call `session.record_usage(event.input_tokens, ...)` (updates
     all accumulators + `_last_input_tokens` in one call); broadcast
     `UsageUpdated` with `session.total_cost`, `session.context_pct`
   - `TurnDone` → add assistant turn to transcript; compute per-message cost
     via `calculate_cost(session.model.cost, ...)` using `TurnUsage` tokens;
     persist assistant `MessageEntry` with per-message cost; broadcast
     `TurnComplete`
   - `TurnFailed` → broadcast `TurnError`
   - `TurnInterrupted` → add partial assistant turn; persist assistant
     `MessageEntry` with `interrupted=True` and `content=accumulated_text`
     (may be empty string); broadcast `TurnInterrupted`
7. Resets `_turn_active` in finally block

**Single TurnUsage per turn** in this iteration (Bedrock emits one `metadata`
event per request). Future multi-round tool turns may yield multiple — the
harness already accumulates, so this extends naturally.

### Session log format (`shared/session/log.py`)

```python
class MessageMetadata(msgspec.Struct):
    model: str
    backend: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float = 0.0
    interrupted: bool = False

class MessageEntry(msgspec.Struct):
    id: str                              # ULID
    when: str                            # ISO-8601 UTC
    role: str                            # "user" | "assistant"
    content: str                         # message text
    metadata: MessageMetadata | None = None  # None for user entries
```

Replaces `SessionLogEntry`, `EntryMetadata`, `ToolCall`. The `ToolCall` struct is
removed (placeholder that was never populated); tool-call representation will be
designed when tool-calling lands.

### FakeLLMClient (`llm/fake.py`)

Implements `LLMClient` protocol. Constructor takes `responses: list[list[StreamEvent]]`
(one list per call to `stream()`). Optional `delay: float` between yields for
interrupt testing. `model_id = "fake-echo"`. `invoke()` returns `"fake response"`.
`stream()` accepts `messages`, `system`, and `tool_config` (ignored).

If `stream()` is called more times than there are response lists → `IndexError`
(loud test failure, not silent).

### What gets deleted

- `agent/src/archie_agent/agent.py` — fully replaced by `harness.py` + `loop.py`
- `TurnLog` dataclass in `session.py`
- `flush_turn()` method on `Session`
- `log_path` property and `_log_dir` field on `Session`
- `SessionLogEntry`, `EntryMetadata`, `ToolCall` in `shared/session/log.py`
- These symbols from `shared/session/__init__.py`: imports of `EntryMetadata`,
  `SessionLogEntry`, `ToolCall`, `write_entry` + their entries in `__all__`
  (replaced by `MessageEntry`, `MessageMetadata`, `write_entry`)
- `_make_mock_llm`, `_make_mock_llm_slow` helpers in `tests/test_agent_loop.py`
- The entire `tests/test_agent_loop.py` file

---

## Milestones

### 1. Agent events + pure loop

**Approach:**
- Create `agent/src/archie_agent/events.py` with five dataclass event types +
  `AgentEvent` union type alias.
- Create `agent/src/archie_agent/loop.py` with `run_loop()`. Extract the
  thread-spawn + queue-drain + interrupt-check logic from current
  `handle_message()`.
- **Field name mapping (Usage → TurnUsage):** `event.cache_read_input_tokens` →
  `TurnUsage.cache_read_tokens`; `event.cache_write_input_tokens` →
  `TurnUsage.cache_write_tokens`. The `_input_` infix is a Bedrock-ism dropped at
  this boundary.
- **Bedrock event ordering:** Bedrock emits `metadata` (Usage) BEFORE `messageStop`
  (Done) in the EventStream, but the drain loop MUST NOT terminate on `Done`. It
  records the stop reason, continues draining until the worker thread's sentinel,
  then yields any pending `TurnUsage`, then yields `TurnDone` last. This matches
  the current `handle_message` behaviour.
- **Worker error propagation:** Worker thread pushes a `_WorkerError(msg: str)`
  dataclass onto the queue (not instance state). The drain loop checks for this
  type and yields `TurnFailed(error=msg)`.
- **Interrupt cleanup:** Worker thread calls `gen.close()` on the LLM sync
  generator when interrupt is set (releases Bedrock HTTP connection — current
  agent.py:243). After the drain loop exits, `run_loop` calls
  `thread.join(timeout=5.0)` and logs a warning if the thread is still alive.
- **Unknown StreamEvent types** (`ToolUseStart`, `ToolUseEvent`): logged at debug
  level, not yielded.
- Test with inline scripted event lists (not yet `FakeLLMClient` — that's
  milestone 2). Use a simple closure that yields `StreamEvent`s to satisfy the
  `LLMClient` protocol in tests.

**Wiring:**
- State: `run_loop` receives `messages: list[Turn]` as a **live reference** (not
  a copy). It passes the list to `llm.stream()` at thread spawn time and never
  reads it again — the harness may mutate the list after the LLM call starts
  (adding the assistant turn) but this is safe because Bedrock has already
  serialized the messages.
- `tool_config`: the `LLMClient.stream()` protocol already has
  `tool_config: list[dict] | None = None`. `run_loop` passes `tool_config=None`
  in this iteration.
- Producers: the background thread reading from `llm.stream()` pushes
  `StreamEvent`s (or `_WorkerError`) onto the async queue via
  `call_soon_threadsafe`.
- Consumers: the async drain loop translates and yields `AgentEvent`s.
- Interrupt: caller passes a `threading.Event`; loop checks between each dequeue.
  Worker thread also checks it between yields and calls `gen.close()` on set.
- Sentinel: worker always pushes a `_SENTINEL_DONE` object as its final queue
  item (after error or normal completion). This is how the drain loop knows
  the thread is finished.

**Edge cases:**
- LLM raises exception → worker pushes `_WorkerError(msg)` then sentinel → drain
  yields `TurnFailed(error=msg)`
- Interrupt set before first event → worker sees it immediately, closes gen,
  pushes sentinel → drain yields `TurnInterrupted`
- Empty response (Done without any TextDelta) → yields `TurnDone` (valid, no
  TextChunk events preceding it)
- Usage arrives after Done (current Bedrock behaviour) → drain continues past
  Done, yields TurnUsage, then TurnDone after sentinel

**Tasks:**
- Implement `events.py` (5 dataclasses + type alias)
- Implement `loop.py` (`run_loop` async generator with thread bridge, interrupt,
  error propagation, join/timeout)
- Write `tests/test_loop.py`: normal flow, interrupt, error, empty response,
  Usage-after-Done ordering

**Deliverable:** `run_loop()` yields correct `AgentEvent` sequences for all paths.

**Verify:** `uv run pytest tests/test_loop.py -v` — all pass.

---

### 2. FakeLLMClient

**Approach:**
- Create `agent/src/archie_agent/llm/fake.py`.
- Constructor: `responses: list[list[StreamEvent]]` (indexed per call),
  `delay: float = 0.0`. Implements `stream(messages, system, tool_config=None)`
  and `invoke(messages, system)`.
- `stream()`: yields events from `responses[call_index]`, incrementing index.
  If `delay > 0`, sleeps between yields (for interrupt timing tests). The delay
  applies between events, not before the first.
- `invoke()`: returns `"fake response"`.
- `model_id = "fake-echo"`.
- Update `llm/__init__.py` to export `FakeLLMClient`.
- Rewrite `tests/test_loop.py` to use `FakeLLMClient` instead of inline closures.

**Edge cases:**
- More `stream()` calls than responses → `IndexError` (loud test bug)
- `delay` only applies between events, not before first

**Tasks:**
- Implement `fake.py`
- Update `llm/__init__.py` exports
- Rewrite loop tests to use `FakeLLMClient`

**Deliverable:** `FakeLLMClient` works as drop-in for all loop test scenarios.

**Verify:** `uv run pytest tests/test_loop.py -v` — all pass (using FakeLLMClient).

---

### 3. Per-message session log

**Approach:**
- Rewrite `shared/src/archie_shared/session/log.py`:
  delete `SessionLogEntry`, `EntryMetadata`, `ToolCall`; add `MessageEntry`,
  `MessageMetadata`.
- `write_entry(path, entry)` signature unchanged (append one JSONL line via
  `msgspec.json.encode`).
- Update `shared/src/archie_shared/session/__init__.py`:
  - Remove imports: `EntryMetadata`, `SessionLogEntry`, `ToolCall`
  - Remove from `__all__`: `"EntryMetadata"`, `"SessionLogEntry"`, `"ToolCall"`
  - Add imports: `MessageEntry`, `MessageMetadata`
  - Add to `__all__`: `"MessageEntry"`, `"MessageMetadata"`
  - `write_entry` stays (import + `__all__` entry unchanged)
- Delete and rewrite `tests/test_session_log.py` entirely for the new schema.
  The old back-compat test (decoding old `SessionLogEntry` JSON) is meaningless
  with the new schema — delete it, don't adapt it. New tests cover: user entry
  (metadata=None), assistant entry (full metadata), round-trip encode/decode,
  append multiple, role as free string.

**Edge cases:**
- User entry has `metadata=None` → encodes as `"metadata":null` in JSON
- `role` is a free string (no enum) — future-proofed for `"tool_result"` etc.
- `content` is always a string (not a ContentBlock list — log is human-readable)

**Tasks:**
- Rewrite `log.py` (delete old structs, implement new)
- Update `shared/session/__init__.py` (remove old symbols, add new)
- Delete and rewrite `tests/test_session_log.py`

**Deliverable:** `MessageEntry` schema works for both user and assistant messages.

**Verify:** `uv run pytest tests/test_session_log.py -v` — all pass.

---

### 4. Trim session + implement harness

**Approach:**
- Trim `agent/src/archie_agent/session.py`: remove `flush_turn()`, `TurnLog`,
  `log_path` property, `_log_dir` field. `Session.__init__` no longer accepts
  `_log_dir`. Keep `Turn`, `Session` (transcript + accumulators + model).
- Create `agent/src/archie_agent/harness.py` with `AgentHarness`:
  - Constructor: `session: Session`, `llm_client: LLMClient`,
    `system_prompt: str`, `log_dir: Path`
  - No `model` parameter — reads `session.model` (single authoritative source
    for cost rates via `session.model.cost` and backend via
    `session.model.provider.name`)
  - Exposes: `session`, `clients`, `turn_active`, `handle_message`, `interrupt`
  - `log_path` property: `self._log_dir / f"{self.session.session_id}.jsonl"`
  - `handle_message` implements the full flow (see Technical Design § Harness)
  - On `TurnUsage`: sets `session._last_input_tokens = event.input_tokens`
    (fixes latent bug — context_pct now updates correctly)
  - Computes **per-message cost** for the assistant `MessageEntry.metadata.cost`
    using `calculate_cost(session.model.cost, usage.input_tokens, ...)` — NOT
    `session.total_cost` (which is cumulative, used only for `UsageUpdated`)
  - On interrupt with zero text: persists `MessageEntry(role="assistant",
    content="", metadata=MessageMetadata(..., interrupted=True))`

**Wiring:**
- State: `AgentHarness` holds `session` (Session), `_llm` (LLMClient),
  `_system_prompt` (str), `_log_dir` (Path), `_interrupt` (threading.Event),
  `_turn_active` (bool), `clients` (set[WebSocket])
- Cost/context flow: on `TurnUsage`, harness calls `session.record_usage(...)`
  which updates all accumulators + `_last_input_tokens` internally, then reads
  `session.total_cost` (property, cumulative) and `session.context_pct` (property)
  to populate the `UsageUpdated` wire event
- Per-message cost: harness stores the last `TurnUsage` event; on `TurnDone`,
  computes per-message cost from those token counts via `calculate_cost`
- Producers: `handle_message` mutates session via `add_turn`, persists via
  `write_entry`, broadcasts via `self.clients`
- Consumers: `app.py` reads `harness.session`, `harness.turn_active`,
  `harness.clients`
- Call site: `app.py` lifespan constructs
  `AgentHarness(session=..., llm_client=..., system_prompt=..., log_dir=home_dir() / "sessions")`

**Edge cases:**
- Turn already active → broadcast `TurnError`, return (no loop invocation)
- Empty assistant (interrupted before any text) → persist assistant entry with
  `content=""`, `metadata.interrupted=True`
- `write_entry` raises (disk full, permissions) → log warning, don't crash the
  turn (persistence is best-effort; the in-memory transcript is source of truth)
- Single `TurnUsage` per turn in this iteration (Bedrock emits one metadata
  event per request). Harness stores the last one for per-message cost.

**Tasks:**
- Trim `session.py` (remove persistence, `_log_dir`, `TurnLog`)
- Implement `harness.py`
- Update `app.py`: replace `AgentLoop` import with `AgentHarness`, construct in
  lifespan with `log_dir=home_dir() / "sessions"`, remove `_log_dir` from Session
  construction
- Delete `agent.py`

**Deliverable:** Agent starts, handles messages through harness, persists
per-message log, broadcasts wire events.

**Verify:** `uv run pytest tests/test_ws_integration.py -v` — all pass (proves
the external API surface is preserved).

---

### 5. Test migration

**Approach:**
- Delete `tests/test_agent_loop.py` (tests the deleted `AgentLoop`).
- Create `tests/test_harness.py`: exercise full path via `FakeLLMClient` →
  `run_loop` → `AgentHarness` → captured wire events. Use `FakeWebSocket` (same
  pattern as current tests) to capture broadcast output.
- Test scenarios: normal flow (text + usage + complete), interrupt mid-stream,
  LLM error, turn-already-active rejection, persistence verification (read JSONL
  file and assert per-message entries with correct role/content/metadata).
- Update `tests/test_ws_integration.py`: verify the existing `mock_env` fixture
  still works unchanged. It sets `ARCHIE_HOME_DIR` and `ARCHIE_SESSION_ID`; the
  sessions dir is auto-created by `write_entry` (mkdir parents=True). Session
  construction no longer takes `_log_dir` — but the fixture doesn't construct
  Session directly (app.py lifespan does), so no fixture changes are expected.
  Confirm by running the tests.
- `tests/test_bedrock.py` stays unchanged — isolated provider adapter test (mocks
  boto3 to test stream parsing and retry logic specifically).
- Run full suite including `test_bedrock.py` to confirm no regressions.

**Tasks:**
- Delete `test_agent_loop.py`
- Create `test_harness.py` (5+ test functions covering all paths + persistence)
- Update `test_ws_integration.py` fixture (remove `_log_dir` from Session ctor,
  ensure sessions dir exists under ARCHIE_HOME_DIR)
- Confirm `test_bedrock.py` passes unchanged
- Run full suite: `uv run ruff check && uv run pytest -v`

**Deliverable:** All tests pass using `FakeLLMClient`, no `MagicMock` LLM patterns
remain (except `test_bedrock.py` which mocks boto3 for the provider adapter).

**Verify:** `uv run ruff check && uv run pytest -v` — clean lint, all tests pass.

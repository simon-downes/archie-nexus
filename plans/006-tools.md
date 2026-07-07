# Plan 006: Tools — Exec Runner + Core Tools + Agent Tool-Use Loop

## Objective

Evolve the agent from text-only chat to a **tool-using agent** (VISION.md evolution
step 3, "the container is the agent"). Add a tool registry, six core tools
(`read`, `write`, `edit`, `grep`, `glob`, `shell`), a multi-round tool-use loop in the
agent, and wire events so the TUI can render tool activity. Tools run **in-process,
inside the agent container**, against the already-rw-mounted `/workspace`. No
sub-sandbox, no docker-in-docker.

## Context

Plans 001–005 delivered the container lifecycle, agent loop, TUI/WebSocket transport,
credential subsystem, and the cross-project session contract. The agent currently
streams Bedrock text responses but cannot act.

Much of the tool-use *plumbing* already exists and MUST NOT be rebuilt (verified against
source this session):

- `bedrock.py:105` — `BedrockClient.stream()` already accepts `tool_config` →
  `params["toolConfig"]`.
- `bedrock.py:151-194` — the stream already parses `tool_use` content blocks:
  `contentBlockStart/toolUse` → `ToolUseStart`; deltas accumulate JSON input;
  `contentBlockStop` → `ToolUseEvent` (parsed `input`, `input_truncated` flag on decode
  failure).
- `llm/_types.py` — `ToolUseStart` / `ToolUseEvent` exist in the `StreamEvent` union.
- `llm/__init__.py:23` — `LLMClient.stream` signature has `tool_config`.
- `shared/types.py` — `TextBlock` / `ToolUseBlock` / `ToolResultBlock` exist and are
  provider-agnostic. `bedrock.py:31-55` (`_turns_to_bedrock_messages`) already serializes
  all three (`toolUse` + `toolResult` with `status`).
- `session.py:127` — `Session.add_turn` accepts `str` **or** `list[ContentBlock]`.
- `app.py:118-160` — `GET /history` **already** serializes `tool_use` / `tool_result`
  blocks (reads in-memory `session.turns`).
- `shared/session/log.py:15-53` — the Plan 005 `SessionLogEntry` contract **already**
  reserves `tools: list[ToolCall]` with `ToolCall(name, source, result)` as a documented
  placeholder ("empty in v1"). Populating it is contract-compatible, NOT a breaking change.
- There is **no JSONL replay** into `session.turns` anywhere — the log is write-only
  (`log.py:6`); the agent never resumes turns from disk. So multi-round tool turns cannot
  break Bedrock continuation on restart *today*; the risk is purely log fidelity/audit.

The gap is: nothing *generates* a `tool_config`, nothing *executes* tools, the agent loop
is single-pass (never loops on `stop_reason == "tool_use"`), there are no tool wire events,
the system prompt is a stub, and the TUI drops non-text blocks on display.

### Architectural difference from nextgen

nextgen's agent runs on the **host** and docker-execs a `runner.py` envelope
(`async def main()`, audit wrap, `result.json`) into a **separate** sandbox container.
In nexus the agent **already runs inside** the container — so there is no sub-sandbox and
the runner envelope is NOT ported. Tools are fixed in-process handlers; the container
itself is the isolation boundary.

---

## Requirements

### Functional

1. MUST add a **tool registry**: name → (JSON input schema, async handler). MUST generate
   the Bedrock `tool_config` list from registered schemas.
   - AC: `tool_config` is passed to `stream()` on every LLM round.
2. MUST implement six core tools against `/workspace` using stdlib + subprocess only
   (no new dependency):
   - `read` (path, optional offset/limit), `write` (path, content),
     `edit` (path, old_string, new_string, replace_all), `grep` (pattern, path, include),
     `glob` (pattern, path), `shell` (command, timeout).
   - AC: relative paths resolve under `/workspace`; results are structured strings.
3. MUST restructure the agent loop into a **multi-round** loop:
   - On `stop_reason == "tool_use"`: persist the assistant turn as
     `[TextBlock?, ToolUseBlock...]`, execute each tool, append a **user** turn of
     `[ToolResultBlock...]`, re-invoke the LLM (with `tool_config`), and repeat until
     `stop_reason != "tool_use"`.
   - AC: MUST preserve the Bug 1 no-break drain — trailing `Usage` (emitted after
     `messageStop`) is consumed **every** round and accumulated into session totals.
   - AC: Threading — a fresh `_stream_worker` thread + queue is spawned **per round**
     and joined before the next round begins (or on interrupt). `_stream_worker` reads
     the current `tool_config` (and per-round messages) from instance state.
   - AC: Tools reach `AgentLoop` via a **new constructor param** (`tools: list[ToolSpec]`,
     default the built-in six); `app.py` passes the registry when building the loop
     (`app.py:88`).
   - AC: A `max_rounds` cap (default 25) terminates runaway loops with a `TurnError`.
   - AC: Interrupt (Esc) breaks cleanly between and within rounds; partial state is
     flushed to JSONL as today.
4. MUST guarantee **a tool cannot crash the agent/web-server process** (hard constraint):
   - AC: every handler invocation is wrapped so any exception → an error
     `ToolResultBlock` (`is_error=True`), never propagating to the WS server task.
   - AC: `shell` runs via `asyncio.create_subprocess_exec` with a timeout (default 60s,
     capped) and output caps; a hung/crashing child cannot block the event loop or kill
     the parent.
   - AC: handlers are `async` and non-blocking — file I/O via `asyncio.to_thread`;
     `grep`/`glob`/`shell` via subprocess.
5. MUST truncate oversized tool results with a marker (head+tail retained, elided byte
   count noted; ~16k char cap). **No artifact store in v1** (documented follow-up).
6. MUST add wire events `ToolCallStarted` and `ToolResult` (in `events.py`, registered in
   `_SERVER_EVENT_TYPES`) and broadcast them per tool call.
   - AC: `ws_client.py` needs no change (generic dispatch through `deserialize_event`).
7. MUST render tool activity in the TUI: handle the new events in
   `tui/app.py:_handle_event`, add a tool widget to `conversation.py`, and stop dropping
   tool blocks in `_load_history` (tui/app.py:134) on reconnect.
8. MUST inject tool documentation into the system prompt (replace the stub at
   `app.py:84`), listing available tools and `/workspace` conventions.
9. MUST persist tool activity to the session JSONL by populating the existing
   `SessionLogEntry.tools: list[ToolCall]` field (Plan 005 contract, no schema break):
   - AC: extend `TurnLog` with a `tool_calls: list[ToolCall]` field; the loop appends a
     `ToolCall(name, source, result)` per executed tool (`source` = JSON args,
     `result` = truncated output).
   - AC: the flush guard (`agent.py:188`) MUST also fire when there are tool calls but no
     assistant text (tool-only exchanges must not vanish from the log).
   - AC: per-round `Usage` is **summed** into the single `TurnLog` (not overwritten with
     the last round), so logged tokens/cost reflect the whole exchange.

### Non-Functional

10. SHOULD keep tool schemas and descriptions in one place so `tool_config` and the
    system-prompt docs stay in sync.
11. MUST NOT add dependencies. `code` (tree-sitter) and `multiedit` are explicitly
    deferred.
12. MUST maintain existing invariants: session_id host-authoritative; sync Bedrock
    generator via thread + `call_soon_threadsafe`. NOTE: the single-line-per-exchange
    JSONL model is **preserved** — one `SessionLogEntry` per user message, now with its
    `tools` list populated and summed usage (see Requirement 9); it is NOT one line per
    round.

---

## Design

### Technology Choices

- **Tools:** fixed async Python handlers, stdlib only. File I/O via `asyncio.to_thread`.
  `grep`/`glob`/`shell` via `asyncio.create_subprocess_exec`.
- **Isolation:** none beyond the container. Safety comes from (a) fixed handlers with
  structured params — no model-authored code execution, (b) exception→error-result
  wrapping, (c) subprocess timeouts/caps for `shell`.
- **Result budgeting:** char-limit truncation with a marker. No artifact store.

### Persistence (session JSONL) — addresses the review's critical gap

The log stays **one `SessionLogEntry` per user exchange** (Plan 005 contract). The
multi-round loop accumulates into a single `TurnLog`:
- `assistant_text` — concatenation of text emitted across all rounds.
- `tool_calls: list[ToolCall]` (NEW field) — one `ToolCall(name, source=json_args,
  result=truncated_output)` per executed tool, in order.
- usage fields — **summed** across every round's `Usage` (fixes under-counting).

`flush_turn` maps `TurnLog.tool_calls` → `SessionLogEntry.tools` (field already exists,
`log.py:53`). The flush guard changes from `if assistant_text or interrupted` to also
include `or tool_calls`, so tool-only exchanges are logged. No change to
`SessionLogEntry`/`EntryMetadata` structs — the shared contract is untouched.

In-memory `session.turns` still holds the full block-level detail
(`[ToolUseBlock...]` / `[ToolResultBlock...]` turns) for live `/history` and for
building Bedrock context within the session. (JSONL is not replayed into `turns` today,
so no restart-continuation concern; if resume-from-JSONL is added later it must
reconstruct tool blocks from `tools` — noted as a follow-up.)

### Bedrock caching caveat

`_turns_to_bedrock_messages` (`bedrock.py:124-125`) appends a `cachePoint` to the last
message. When the tail is a rapidly-changing `[ToolResultBlock...]` turn, the cache will
re-write each round (cache thrash, not a correctness bug). Acceptable for v1; note it.

### Structure

```
agent/src/archie_agent/
├── agent.py            # MODIFIED: multi-round tool-use loop; per-round threads
├── app.py              # MODIFIED: tool-aware system prompt; pass registry to AgentLoop
├── session.py          # MODIFIED: TurnLog.tool_calls; flush maps -> SessionLogEntry.tools
├── tools/
│   ├── __init__.py     # NEW: registry, ToolSpec, dispatch, tool_config builder, docs
│   ├── fs.py           # NEW: read, write, edit
│   ├── search.py       # NEW: grep, glob
│   └── shell.py        # NEW: shell (subprocess + timeout + caps)
└── llm/                # UNCHANGED (tool_config + parsing already present)

shared/src/archie_shared/
├── events.py           # MODIFIED: ToolCallStarted, ToolResult + registry entries
└── session/log.py      # UNCHANGED: ToolCall/tools field already present (Plan 005)

cli/src/archie_cli/
├── ws_client.py        # UNCHANGED (generic dispatch)
└── tui/
    ├── app.py          # MODIFIED: handle tool events; render tool blocks in history
    └── conversation.py # MODIFIED: ToolCallMessage widget
```

### Tool registry / ToolSpec

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str          # feeds both tool_config and system-prompt docs
    input_schema: dict        # JSON schema for Bedrock toolSpec.inputSchema.json
    handler: Callable[[dict], Awaitable[str]]

def build_tool_config(specs) -> list[dict]:   # -> [{"toolSpec": {...}}, ...]
def tool_docs(specs) -> str:                   # markdown block for system prompt
async def dispatch(name, input) -> tuple[str, bool]:  # (content, is_error) — never raises
```

### Wire Protocol additions

Server → Client (follow the existing `to_json` / `from_json(turn_index, data)` contract;
register in `_SERVER_EVENT_TYPES`):
```json
{"type": "tool_call_started", "turn_index": 3, "data": {"tool_use_id": "tu_1", "name": "read", "input": {"path": "..."}}}
{"type": "tool_result",       "turn_index": 3, "data": {"tool_use_id": "tu_1", "name": "read", "is_error": false, "summary": "42 lines"}}
```
`summary` is a short, already-truncated preview for display; full content lives in the
turn/JSONL, not the wire event.

### Agent loop restructure (agent.py)

Wrap the existing single-pass drain in a **round loop**. Per round:
1. `_stream_worker` calls `stream(messages, system, tool_config=...)`.
2. Drain as today, additionally collecting `ToolUseEvent`s into `tool_calls` and any
   streamed text into `assistant_text`. Keep the no-break-on-`Done` behaviour so the
   trailing `Usage` is consumed and accumulated.
3. On sentinel: build the assistant turn `[TextBlock(assistant_text)?, ToolUseBlock...]`
   and `add_turn`.
4. If no `tool_calls` (`stop_reason != "tool_use"`) → broadcast `TurnComplete`, flush,
   done.
5. Else: for each call, broadcast `ToolCallStarted`, `await dispatch(...)`, broadcast
   `ToolResult`, collect `ToolResultBlock`. `add_turn(role="user", [ToolResultBlock...])`.
   Increment round; if `round >= max_rounds` → `TurnError`. Loop.

Per-round threading: each round spawns a fresh `_stream_worker` thread + `asyncio.Queue`
and joins it before the next round (or on interrupt). `_stream_worker` reads the current
messages + `tool_config` from instance state. Interrupt and error handling reuse the
existing sentinel/`_interrupt` mechanism; a single `finally` block flushes ONE `TurnLog`
(accumulated across rounds) as described in Persistence.

### System prompt

Replace `app.py:84` with a prompt that includes `tool_docs(specs)`, `/workspace`
conventions (relative paths, prefer `read` before `edit`, use `shell` sparingly), and the
existing model line.

---

## Milestones

1. **Registry + tools** — `tools/` package: `ToolSpec`, registry, `build_tool_config`,
   `tool_docs`, `dispatch`; implement all six handlers with exception wrapping,
   truncation, and shell subprocess caps. Unit tests per tool + dispatch safety
   (exception → error result).
2. **Wire events** — add `ToolCallStarted` / `ToolResult` to `events.py` + registry;
   round-trip serialize/deserialize tests.
3. **Agent loop + persistence** — restructure `handle_message` into the multi-round loop
   (per-round worker threads); wire `tool_config` (via constructor + instance state),
   dispatch, and broadcasts; `max_rounds` cap. Extend `TurnLog.tool_calls`; sum per-round
   usage; map to `SessionLogEntry.tools`; fix the flush guard for tool-only turns. Tests:
   single tool round, multi-round, tool-only turn (no text) still flushes, tool error,
   interrupt mid-round, Usage summed across rounds (guard Bug 1 regression), JSONL entry
   has populated `tools` + summed tokens.
4. **System prompt** — inject `tool_docs` at `app.py`.
5. **TUI** — `ToolCallMessage` widget; handle new events in `_handle_event`; render tool
   blocks in `_load_history` (drop the text-only skip guard at `tui/app.py:135-136`).
6. **End-to-end** — rebuild image, run a real session exercising each tool against
   `/workspace`; verify TUI rendering, history replay, interrupt, and JSONL `tools` output.

## Testing

- Unit: each tool (happy path + error + truncation); `shell` timeout/caps; `dispatch`
  never raises; `build_tool_config` / `tool_docs` shape.
- Wire: serialize/deserialize round-trip for both new events.
- Agent: mocked LLM emitting `ToolUseEvent` then `end_turn`; assert assistant turn has
  `ToolUseBlock`s, follow-up user turn has `ToolResultBlock`s, `TurnComplete` fires once,
  Usage summed across rounds, `max_rounds` triggers `TurnError`, interrupt flushes
  partial state.
- Persistence: flushed `SessionLogEntry` has populated `tools` list (name/source/result)
  and summed tokens/cost; a tool-only exchange (no assistant text) is still written.
- Full suite (currently 119) stays green.

## Out of Scope / Follow-ups

- Artifact store + `retrieve_artifact` (revisit if truncation proves lossy in practice).
- `code` tool (tree-sitter) — separate plan; adds a dependency.
- `multiedit` tool.
- Per-tool permission prompts / approval UX.

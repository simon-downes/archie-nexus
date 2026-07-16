# 017 — TUI Improvements & Event Cleanup

## Objective

Improve the nexus TUI's clarity and usability by fixing iteration block boundaries (one
block per LLM request/response), adding live output token estimation during streaming,
enabling direct shell execution without LLM involvement, making `archie start` auto-attach,
and fixing numpad key input. As part of this work, align the event naming between agent-side
and wire protocol for consistency.

## Context

The current TUI groups all tool calls from an entire multi-turn agent loop into a single
visual block. When the model makes 3 sequential LLM requests (each with tool calls), they
all appear nested in one block — making it hard to see iteration boundaries. The `Usage`
event already fires after each LLM request (not just at turn end), making it a natural
block boundary signal without introducing a new event type.

The wire protocol event names diverge from agent-side names without good reason (TurnDone
vs TurnComplete, TurnFailed vs TurnError, TextChunk vs TextDeltaEvent), and `UsageUpdated`
currently carries cumulative session totals computed server-side when it should just carry
per-request values and let the TUI accumulate locally.

Additionally, `archie start` currently prints connection info and exits, requiring a
separate `archie attach`. The TUI input widget doesn't register numpad arithmetic keys
in some terminals.

## Requirements

### Event naming & structure

- MUST rename agent-side events to align with wire protocol naming:
  - `TextChunk` → `TextDelta`
  - `TurnUsage` → `Usage`
  - `TurnDone` → `TurnComplete`
  - `TurnFailed` → `TurnError`
  - AC: All internal references compile and tests pass with new names
- MUST rename wire protocol event classes to remove inconsistent suffixes:
  - `TextDeltaEvent` → `TextDelta`
  - `UsageUpdated` → `Usage`
  - `ToolCallEvent` → `ToolCall`
  - `ToolResultEvent` → `ToolResult`
  - AC: Wire classes and agent classes share the same base name (distinguished by module)
- MUST change wire type string `usage_updated` → `usage`
  - AC: JSON on the wire uses `"type": "usage"`
- MUST change wire `Usage` event to carry per-request values (not cumulative session totals)
  - AC: `Usage` event contains `turn_index`, `input_tokens`, `output_tokens`,
    `cache_read_tokens`, `cache_write_tokens`, `context_pct`
  - AC: Token fields are per-request (that single LLM call only)
  - AC: `context_pct` is the server-computed session-level context window percentage
    (retained because only the server knows system prompt + tool config sizes)
  - AC: No `cost` field on the wire event
- MUST keep all other wire `type` strings unchanged (`text_delta`, `tool_call`,
  `tool_result`, `turn_complete`, `turn_error`, `turn_interrupted`)
  - AC: Existing type string consumers continue to work

### Iteration block boundaries

- MUST create a new TUI iteration block per LLM request/response
  - AC: When the model makes 3 sequential requests with tool calls, 3 separate blocks
    appear in the TUI
- MUST use `Usage` event as the implicit block boundary signal
  - AC: After receiving a `Usage` event, the next `ToolCall` or `TextDelta` opens a new block
  - AC: No new wire event type is introduced for this purpose
- MUST group batched tool calls from a single model response within one block
  - AC: If model returns 3 tool calls in one response, all 3 appear in the same block

### Live output token estimation

- MUST estimate output tokens during streaming using `len(text) / 4`
  - AC: Status bar output counter increases as text streams in
- MUST reconcile estimated tokens with concrete values when `Usage` arrives
  - AC: Counter snaps to real accumulated value on `Usage` receipt
- MUST track cumulative session totals on the TUI side (not server-computed)
  - AC: TUI accumulates `input_tokens`, `output_tokens`, `cache_read_tokens`,
    `cache_write_tokens` locally across all `Usage` events
- MUST compute cost on the TUI side using model pricing rates
  - AC: Cost displayed matches `calculate_cost()` applied to accumulated totals
- MUST include model cost rates in `SessionInfo` so TUI can compute cost
  - AC: `SessionInfo` includes `cost` field with `input`, `output`, `cache_read`,
    `cache_write` rates

### Direct shell (`!` prefix)

- MUST intercept user input starting with `!` before sending to the agent
  - AC: `!ls -l` does not trigger an LLM call
- MUST execute the command in the session container via `docker exec`
  - AC: Command runs in `/workspace` working directory
  - AC: Command runs with the same user/mounts as the agent
- MUST display stdout and stderr output directly in the conversation
  - AC: Output appears as a distinct block (not user message, not assistant message)
- MUST show non-zero exit codes
  - AC: Exit code is visible when command fails
- MUST log the command and output to the session log
  - AC: Session JSONL contains a record of the direct shell execution
- SHOULD support interruption via Escape (consistent with turn cancellation)
  - AC: Pressing Escape while a `!` command runs kills the subprocess

### Auto-attach on `archie start`

- MUST launch the TUI automatically after container is ready
  - AC: `archie start` opens an interactive session without requiring a separate
    `archie attach`
- MUST support `-d`/`--detach` flag for headless start (current behaviour)
  - AC: `archie start -d` prints connection info and exits
- MUST print session ID to terminal before launching TUI
  - AC: If TUI crashes, session ID is visible in terminal scrollback for `archie attach`

### Numpad key support

- MUST register numpad arithmetic keys (`/`, `*`, `-`, `+`, `.`) in the input widget
  - AC: Pressing numpad `/` inserts `/` character into the input
  - AC: Same for `*`, `-`, `+`, `.`
- SHOULD map numpad Enter to regular Enter (submit message)
  - AC: Pressing numpad Enter submits the message

## Technical Design

### Overview

Refactor the wire protocol events to be per-request and consistently named, then update
the TUI to use `Usage` as an implicit block boundary, estimate output tokens live, compute
costs locally, and add three UX features (direct shell, auto-attach, numpad keys). The
agent side gets simpler (no cumulative tracking for wire broadcast), and the TUI gets
smarter (local accumulation, live estimation).

### Code Structure

Agent side:
- `agent/src/archie_agent/events.py` — rename event classes
- `shared/src/archie_shared/events.py` — rename wire event classes, change `Usage` shape
- `agent/src/archie_agent/harness.py` — simplify `Usage` broadcast (forward per-request)
- `agent/src/archie_agent/loop.py` — update event references
- `agent/src/archie_agent/app.py` — extend `SessionInfo` with cost rates

CLI/TUI side:
- `cli/src/archie_cli/tui/app.py` — block boundary logic, output estimation, local
  accumulation, `!` shell handler
- `cli/src/archie_cli/tui/conversation.py` — new `ShellOutput` widget
- `cli/src/archie_cli/tui/status.py` — no structural changes
- `cli/src/archie_cli/tui/input.py` — numpad key mapping
- `cli/src/archie_cli/cli.py` — `--detach` flag on `start`

### Data Flow

Usage event flow (new):
```
loop yields Usage(per-request) → harness broadcasts Usage(per-request + context_pct)
TUI receives Usage → accumulates locally → computes cost from SessionInfo rates → updates status
```

Block boundary flow:
```
TUI state: _new_block_pending = True (initial)

On Usage:        _new_block_pending = True, reconcile output estimate
On TextDelta:    if _new_block_pending → new block; append text
On ToolCall:     if _new_block_pending → new block; add tool entry
On TurnComplete: reset state for next turn
```

Output estimation flow:
```
On TextDelta: _estimated_output += len(text) // 4; display cumulative + estimated
On Usage:     _cumulative_output += event.output_tokens; _estimated_output = 0; display cumulative
```

### Key Decisions

- No `IterationStart` event — `Usage` already fires once per LLM request. The TUI uses
  it as implicit boundary. Avoids protocol expansion.
- Per-request on the wire — harness no longer accumulates for wire. Session-level tracking
  remains in `session.py` for persistence/context-pct, but wire forwards raw values.
- Context percentage stays on wire `Usage` — TUI can't compute it (doesn't know system
  prompt size or tool config size). The agent computes it.
- Container name derived from session_id — `archie-{session_id}` is deterministic. TUI
  derives it. No protocol change needed.
- `!` shell runs async — uses `asyncio.create_subprocess_exec` so UI stays responsive.
  Escape cancels via `process.kill()`.
- Model switch cost handling — on `ModelSwitched`, TUI updates its cost rates for future
  accumulation. Already-accumulated cost stays (no reset).

## Milestones

### 1. Event rename (agent + wire)

Approach:
- Rename in both `events.py` files (agent and shared), then fix all imports/references.
- Agent-side and wire-side classes share the same base name (`TextDelta`, `Usage`,
  `ToolCall`, `ToolResult`, `TurnComplete`, `TurnError`), distinguished by module.
- ⚠️ The harness imports both agent `ToolCall`/`ToolResult` and wire `ToolCall`/`ToolResult`.
  Use aliases: `from archie_shared.events import ToolCall as WireToolCall`.
- Wire type string for usage changes from `usage_updated` to `usage`. All other type
  strings unchanged.
- `_SERVER_EVENT_TYPES` mapping needs updating for new class names and changed key.

Tasks:
- Rename agent events in `events.py`: TextChunk→TextDelta, TurnUsage→Usage,
  TurnDone→TurnComplete, TurnFailed→TurnError
- Rename wire events in shared `events.py`: TextDeltaEvent→TextDelta,
  UsageUpdated→Usage, ToolCallEvent→ToolCall, ToolResultEvent→ToolResult
- Change wire type string `usage_updated` → `usage`
- Grep across agent/, shared/, cli/, and tests/ for all four old names (both agent and
  wire) to find every import site — don't rely on just known files
- Update all imports in harness, loop, app.py (agent), app.py (TUI), tests
- Use aliases where both agent and wire ToolCall/ToolResult are imported (harness, loop,
  and any test that exercises both)
- Run full test suite

Deliverable: All event classes renamed, wire type string updated, tests pass, ruff clean.

Verify: `uv run pytest tests/ -q && uv run ruff check .`

### 2. Per-request Usage on wire + TUI local accumulation

Approach:
- Wire `Usage` drops `cost` field, keeps `context_pct` (only server can compute this).
- Harness broadcasts raw per-request values from agent `Usage` event + computed `context_pct`.
- `SessionInfo` gains a `cost` object with per-million-token rates (mirrors `CostConfig`):
  `{"input": float, "output": float, "cache_read": float, "cache_write": float}`.
- `ModelSwitched` gains the same `cost` object so TUI updates rates mid-session.
- TUI accumulates *cost* (not just tokens) — each Usage event's cost is computed using
  the rates active at that time and added to a running total. This handles model switches
  correctly (old tokens were priced at old rates).
- TUI also accumulates token counts for display.
- Existing `session.py` accumulation stays (used for persistence, context tracking).

Wiring:
- State: `_cumulative_input`, `_cumulative_output`, `_cache_read`, `_cache_write` (ints),
  `_cumulative_cost` (float), `_cost_rates` (dict) — in TUI ArchieApp
- Producers: `Usage` handler adds per-request values to cumulatives, computes delta cost
- Consumers: status bar refresh reads cumulatives and `_cumulative_cost` for display
- Call site: on `SessionInfo` → store cost rates; on `ModelSwitched` → update cost rates;
  on `Usage` → compute delta cost, accumulate, refresh

Edge cases:
- SessionInfo with zero cost rates (ollama): cost displays as $0.0000
- Model switch mid-session: new rates apply to subsequent Usage events only; already-
  accumulated cost is correct
- Multiple Usage events per turn (one per iteration): all accumulate correctly

Tasks:
- Add `cost` field to `SessionInfo` wire event (dict: input, output, cache_read, cache_write)
- Add `cost` field to `ModelSwitched` wire event (same shape)
- Update agent `app.py` to include model cost rates in SessionInfo broadcast
- Change wire `Usage` shape: drop cost, keep turn_index + per-request tokens + context_pct
- Simplify harness: broadcast per-request values + context_pct (remove cumulative wire logic)
- Update TUI: store cost rates from SessionInfo, accumulate tokens + cost on Usage, refresh
- Update TUI ModelSwitched handler to update cost rates
- Update tests for new Usage shape, SessionInfo fields, ModelSwitched fields

Deliverable: Wire Usage carries per-request values, TUI accumulates and displays correct cost.

Verify: `uv run pytest tests/ -q` — then manual: start session, send message, confirm
status bar shows accumulated tokens and cost matching expected rates.

### 3. Block boundaries using Usage

Approach:
- TUI maintains `_new_block_pending: bool` flag, starts as `True`.
- On `Usage`: set `_new_block_pending = True` (iteration ended, next content starts new block).
- On `TextDelta` or `ToolCall`: if `_new_block_pending`, create new `IterationBlock`, clear flag.
- On `TurnComplete`: set `_new_block_pending = True` and clear `_iteration_block`.
- Remove old logic that resets block on streaming text finalization.
- ⚠️ Block logic only applies to live events (the `_handle_event` path). History replay
  uses `add_assistant_message()` / `add_user_message()` which bypass streaming entirely.

Edge cases:
- Model responds with only text (no tools): one block with streaming text, Usage sets flag
- Model responds with only tool calls (no text): block created on first ToolCall
- Model responds with text then tool calls (most common): TextDelta opens block, subsequent
  ToolCalls in the same iteration join the same block (no Usage between them)
- Empty Usage (0 tokens, error path): still sets the flag
- First event of session has no preceding Usage: initial flag state handles it
- History replay: not affected — uses separate rendering path

Tasks:
- Add `_new_block_pending = True` to `ArchieApp.__init__`
- Update Usage handler: set `_new_block_pending = True`
- Update TextDelta handler: check flag, create new block if needed
- Update ToolCall handler: check flag, create new block if needed
- Remove old `_iteration_block = None` on streaming finalization logic
- Update TurnComplete/`_end_turn`: set `_new_block_pending = True`, clear block reference

Deliverable: Each LLM request/response renders as a separate visual block in the TUI.

Verify: Manual test: send prompt that triggers multi-iteration tool use. Confirm separate
blocks per iteration.

### 4. Live output token estimation

Approach:
- During streaming, increment `_estimated_output` by `len(text_delta) // 4` on each TextDelta.
- Status bar shows `_cumulative_output + _estimated_output` as the output counter.
- On Usage: `_cumulative_output += event.output_tokens`, reset `_estimated_output = 0`.
- Estimate is intentionally rough — visual feedback, not precision.

Tasks:
- Add `_estimated_output: int = 0` to ArchieApp.__init__
- Update TextDelta handler: increment estimate, refresh status output display
- Update Usage handler: accumulate real values, reset estimate, refresh status
- Ensure status bar reads `cumulative + estimated` for output display

Deliverable: Status bar output counter ticks up during streaming, snaps to real on Usage.

Verify: Manual: send prompt producing long text. Observe counter increasing during stream,
stabilizing on completion.

### 5. Direct shell (`!` prefix)

Approach:
- Intercept in `on_message_input_submitted` before WebSocket send.
- Derive container name using `container_name(session_id)` from
  `archie_shared.session.identity` (not inline f-string).
- Use `asyncio.create_subprocess_exec("docker", "exec", "-w", "/workspace", container,
  "bash", "-c", command)` for async non-blocking execution.
- New `ShellOutput` widget in conversation.py — dimmed styling, `$` prefix, shows
  command + output.
- ⚠️ Don't use `subprocess.run` — blocks the event loop.
- Use a separate `_shell_active` flag (not `_turn_active`) — shell commands don't involve
  the LLM turn lifecycle. Reject a second `!` command if one is already running.
- Log to session via POST to a new `/shell` endpoint on the agent HTTP API. No auth
  needed (agent listens on 127.0.0.1 only, same as all existing endpoints).
- Session log entry uses `role: "shell"` with content as JSON: `{"command": "...",
  "exit_code": N, "output": "..."}`.

Wiring:
- State: `self._session_id` (str, from SessionInfo), `self._shell_proc` (optional Process),
  `self._shell_active` (bool)
- Producers: `_run_direct_shell()` sets `_shell_proc` and `_shell_active` during execution
- Consumers: `on_message_input_submitted` checks `_shell_active` to reject concurrent;
  `action_cancel` checks `_shell_proc` for Escape handling

Edge cases:
- Container not running (docker exec fails): show `ClientErrorMessage` (client-side error,
  not logged to session — the error never reaches the agent)
- Large output: truncate at 10,000 lines with "(truncated)" — truncation happens client-side
  before POST, so the log entry is bounded
- Command hangs: timeout after 30 seconds
- Escape during execution: kill subprocess, show "(interrupted)"
- Empty command (`!` alone): ignore
- Second `!` while one running: show "shell command already running" in conversation
- Block logic: shell output is not part of an iteration block (mounted directly to Conversation)
- No double-logging: `!` commands bypass the WebSocket message path entirely — the agent's
  normal turn-logging cannot fire because no MessageCommand is sent

Tasks:
- Store `self._session_id` from SessionInfo in ArchieApp
- Create `ShellOutput` widget in conversation.py (command header + dimmed output body)
- Intercept `!` prefix in `on_message_input_submitted` (before `_turn_active` check)
- Add `_shell_active` flag, reject concurrent shell commands
- Implement `_run_direct_shell()` with asyncio subprocess, capture, timeout
- Use `container_name(self._session_id)` for docker exec target
- Add `finally` block in `_run_direct_shell()` that always resets `_shell_active = False`
  and `_shell_proc = None` (covers normal exit, timeout, Escape kill, and exceptions)
- Truncate output to 10,000 lines client-side before display and before POST
- Add ShellOutput to conversation on completion
- Handle Escape cancellation (kill shell_proc — triggers finally cleanup)
- Add `/shell` POST endpoint to agent app.py (accepts JSON `{command, exit_code, output}`,
  writes MessageEntry with `role="shell"` to session log)
- POST command+output to `/shell` after execution (skip POST on docker exec failure since
  that's a client-side error)

Deliverable: `!ls -l` runs in container and shows output in TUI without LLM involvement.

Verify: Start session, type `!pwd`, confirm `/workspace` displayed. Type `!exit 1`,
confirm exit code shown. Type `!sleep 60`, press Escape, confirm interrupted.

### 6. Auto-attach on `archie start`

Approach:
- Add `-d`/`--detach` Click flag to start command.
- Default (no flag): print session ID, launch ArchieApp.
- With `--detach`: current behaviour (print info, exit).
- Print session ID before TUI launch (visible in scrollback if TUI crashes).
- Uses the same `ArchieApp(host, port)` instantiation and `app.run()` as `archie attach`,
  so `on_mount` → WebSocket connect → reconnect logic all exercises the same code path.

Edge cases:
- TUI crashes: container keeps running, user can `archie attach`
- Container fails to start: error before TUI launch (no change)

Tasks:
- Add `@click.option("-d", "--detach", is_flag=True)` to `start()`
- After `wait_for_ready()`: always print session ID
- If not detach: import and run `ArchieApp(host, port)`
- If detach: print container and agent URL (current behaviour)
- Update command help text

Deliverable: `archie start` opens interactive TUI; `archie start -d` starts headless.

Verify: Run `archie start` — TUI launches. Quit TUI, container still running.
Run `archie start -d` — text output only, no TUI.

### 7. Numpad key support (spike — requires user in the loop)

Approach:
- ⚠️ This milestone cannot be completed autonomously. Numpad key names vary by terminal
  emulator and Textual version. Implementation requires interactive testing in the user's
  terminal to discover actual key event names before writing the mapping.
- Most modern terminals with NumLock on send ASCII characters directly (already works).
  The issue occurs in terminals sending application-mode numpad sequences.
- Strategy: add debug logging, have user press keys, read logged names, add mapping.

Edge cases:
- NumLock off: numpad sends navigation keys — don't intercept
- Terminal sends regular characters (most terminals): already works, handler is no-op
- Key names vary by Textual version/terminal

Tasks:
- Add temporary debug logging in `_on_key` to print `event.key` for unhandled keys
- User tests in their terminal — reports key names back
- Add numpad key mapping dict and handler in `_on_key` using discovered names
- Map numpad Enter to submit if it has a distinct key name
- Remove debug logging after mapping confirmed

Deliverable: Numpad arithmetic keys insert their characters in the input widget.

Verify: In TUI, press numpad `/`, `*`, `-`, `+`, `.` — all insert their characters.

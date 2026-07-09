# Plan 007 — Tools: exec-first agentic loop

## Objective

Turn the single-shot agent loop into an **agentic multi-turn tool loop** and give the model
its first real capability: an `exec` tool that runs model-authored Python inside the container,
with filesystem (`read`/`write`/`edit`/`grep`/`glob`) and `shell` tools callable from that
Python. This is VISION step 3 ("Tools — exec runner, filesystem tools, shell tool inside
container"). When complete, the model can inspect and modify the mounted `/workspace` project
across multiple tool round-trips within a single user turn.

## Context

Plan 006 extracted a **pure** `run_loop()` async generator and a stateful `AgentHarness`. That
loop is deliberately single-shot: it streams one LLM response, translates provider `StreamEvent`s
to neutral `AgentEvent`s, and ignores `ToolUseStart`/`ToolUseEvent`
(`agent/src/archie_agent/loop.py:144-146`) while hardcoding `tool_config=None`
(`agent/src/archie_agent/loop.py:73`). The type system and Bedrock (de)serialization for a
full tool loop are **already complete and bidirectional** — `ToolUseBlock`/`ToolResultBlock`
(`shared/src/archie_shared/types.py:24,38`), `toolResult` status mapping and
`params["toolConfig"]` (`agent/src/archie_agent/llm/bedrock.py:31,134`), and
`ToolUseStart`/`ToolUseEvent` stream events (`agent/src/archie_agent/llm/_types.py:16,24`). What's
missing is everything above the LLM client.

> **Paths & tests:** production code lives under `agent/src/archie_agent/` and
> `shared/src/archie_shared/`; all tests live at the **repo-root** `tests/` directory (there is no
> `agent/tests/`). `uv run pytest tests/...` is run from the repo root.

A mature reference implementation exists in the sibling project **archie-nextgen**
(`~/dev/archie/archie-nextgen`). Its design (confirmed with the user for reuse):
- **Two-tier tool model**: the LLM sees only a small set of *host* tools (primarily `exec`); the
  filesystem/shell functions are *exec tools* the model calls **inside** exec'd Python. This
  keeps the LLM-facing tool surface tiny and lets the model compose operations in code.
- **Subprocess runner + file IPC**: `exec` writes the model's source to `main.py`, runs
  `runner.py <run_dir>` as a subprocess, and reads back a JSON `result.json` envelope. The runner
  does `compile`+`exec`, `redirect_stdout/stderr`, `asyncio.run(main())`, and captures
  return/stdout/stderr/exception/timing plus an audit log of tool calls.

nextgen runs on the *host* and `docker exec`s into a sandbox. archie-nexus **is** the container
("the container is the agent", VISION §72), so the runner runs **locally inside the same
container** — but still as a subprocess (nested `asyncio.run` + global stdout redirect are unsafe
on the Starlette event loop).

Decisions taken during planning (user-confirmed):
- Full scope: agentic loop + exec runner + exec tools, exec-first paradigm.
- v1 exec tools: **fs (read/write/edit/grep/glob) + shell only**. Web/code/brain deferred.
- **Incremental per-message persistence**: each message (assistant text/tool_use, tool_result) is
  written to the JSONL log as it completes, reusing the v2 `MessageEntry` schema and its
  free-string `role` (006 future-proofed `role` for exactly `tool_call`/`tool_result`). This
  preserves 006's crash-safety principle for the now-much-longer tool turn.
- Runner + exec tools live in the **agent** package (mounted `/opt/archie/agent:rw`,
  live-iterable), invoked via `/opt/archie/venv/bin/python -m archie_agent.exec.runner <run_dir>`.

---

## Requirements

### Agentic loop
- **MUST** loop within a single user turn: stream LLM → if the response contains tool-use →
  execute tools → append results as a tool turn → re-invoke the LLM → repeat until the model
  stops requesting tools.
  - AC: a user turn where the model calls `exec` twice then answers produces one user message,
    two assistant(tool_use)+tool_result cycles, and a final assistant text message.
- **MUST** terminate the loop when `stop_reason` is not `tool_use` (or `max_tokens`), OR when a
  configurable max-iteration cap is reached.
  - AC: with a `FakeLLMClient` scripted to always request a tool, the loop stops after the cap and
    surfaces a `TurnError`; it does not run forever.
- **MUST** send `tool_config` (the tool schemas) to the LLM on every request in the loop.
  - AC: `bedrock.py` receives a non-None `toolConfig`; a fake asserts the passed config includes
    `exec` (requires extending `FakeLLMClient` to capture the last `tool_config` — see M1).
- **MUST** batch all `ToolResultBlock`s from one assistant response into a **single** following
  user turn (Bedrock requires all tool results in one user message).
  - AC: two tool_use blocks in one response → one user turn containing two `ToolResultBlock`s.
- **MUST** repair history on interrupt so every `ToolUseBlock` has a matching `ToolResultBlock`
  before the turn ends (Bedrock rejects orphaned tool_use).
  - AC: interrupting mid-tool-execution still leaves a well-formed transcript that can be re-sent.

### exec runner
- **MUST** execute model-authored Python as a **subprocess** (not on the server event loop), via
  file-based IPC: write `main.py`, run the runner, read `result.json`.
  - AC: an `exec` call with `async def main(): return 1+1` yields a result envelope with `return: 2`.
- **MUST** validate the contract: source defines `async def main()` with no required args; syntax
  errors and contract violations are surfaced as structured errors, not crashes.
  - AC: source without `main` returns a `ContractError` envelope; bad syntax returns `SyntaxError`.
- **MUST** capture stdout, stderr, return value (JSON-serialised, else `repr`), exceptions (with
  traceback), duration, and an audit log of exec tool calls.
  - AC: `print("hi")` appears in `stdout`; a raised `ValueError` appears in `error` with type+traceback.
- **MUST** inject the exec tool functions as async callables into the exec namespace, plus
  `asyncio`.
  - AC: `await read(path=...)` inside `main()` works and is recorded in the `calls` audit list.
- **SHOULD** cap large envelope fields (10 MB) to protect the agent.

### Exec tools (fs + shell)
- **MUST** provide async `read`, `write`, `edit`, `grep`, `glob`, `shell` resolving paths against
  `/workspace`, raising typed `ToolError` subclasses on failure (not returning error strings).
  - AC: `read` of a nonexistent file raises `FileNotFoundError`; `edit` with missing `old` raises
    `EditError`; a path outside `/workspace` raises `PathValidationError`.
- **MUST** have `shell` return `{stdout, stderr, exit_code}` (non-zero exit is data, not an exception).
  - AC: `shell("false")` returns `exit_code: 1` without raising.

### LLM-facing tool surface
- **MUST** expose exactly one tool to the model in v1: `exec`, whose schema is a single
  `source: string` parameter.
  - AC: `to_tool_config()` returns one tool named `exec`.
- **MUST** auto-generate the exec tool documentation into the `exec` tool description from the
  exec tool signatures + docstrings (so docs can't drift from implementations).
  - AC: the `exec` description lists `read(...)`, `write(...)`, `shell(...)` etc. with one-line docs.

### Persistence & wire events
- **MUST** persist each message incrementally as it completes, reusing `MessageEntry` with roles
  `user`, `assistant`, `tool_call`, `tool_result`. Assistant/tool_use entries persist on `Done`;
  tool_result entries persist after execution.
  - AC: after a tool turn, the JSONL log contains ordered entries: user → assistant(tool_use) →
    tool_result → assistant(text), and a mid-turn crash leaves everything up to the last completed step.
- **Session log content format for tool entries:**
  - `tool_call`: `content` is JSON-serialised `{"name": "exec", "source": "async def main():\n ..."}`.
    `metadata` is None (token tracking is per-LLM-request via TurnUsage, not per-tool-call).
  - `tool_result`: `content` is the formatted model-facing result string (same text fed back to
    the LLM — already truncated/capped at `_MAX_RESULT_CHARS`). `metadata` is None.
  - This gives full session-log auditability: what the model asked for + what it got back, without
    needing the ephemeral run dirs.
- **MUST** broadcast tool activity to connected clients as wire events so the TUI can render it.
  - AC: connecting a `FakeWebSocket` during a tool turn receives tool-use / tool-result events.
- **MUST** accumulate token usage across all LLM round-trips in the turn into the session
  accumulators (each `record_usage` call updates totals and `_last_input_tokens`).
  - AC: a two-request turn's `total_input_tokens` equals the sum of both requests' input tokens.

### Non-functional
- **MUST** keep `loop.py` pure (no persistence/WebSocket/session-state imports) — tool *execution*
  is injected into the loop, not imported by it.
- **MUST** pass `ruff check` and the existing 132 tests, plus new tests for the loop, runner,
  exec tools, and harness tool paths.

---

## Technical Design

### Structural decisions
- **New package `agent/src/archie_agent/exec/`** holds the runner and exec tools (agent-owned,
  mounted `:rw`, live-iterable). Layout:
  - `exec/runner.py` — the subprocess entry point (`python -m archie_agent.exec.runner <run_dir>`).
    Ported ~verbatim from nextgen `sandbox/runner.py` (stdlib-only: contract validation, compile,
    exec, stdout/stderr capture, envelope, audit-wrap, SIGTERM partial-flush).
  - `exec/tools/` — `__init__.py` (`@tool` registry + `ToolError` hierarchy +
    `get_all_tools()`), `fs.py`, `shell.py`. Ported ~verbatim from nextgen
    `sandbox/capabilities/` (async, `/workspace`-relative, typed exceptions), renamed.
  - `exec/tool.py` — the host-side `exec` handler: `run_exec(source) -> Envelope`. Writes
    `main.py`, spawns the runner subprocess, reads `result.json`, formats the model-facing result.
    Also builds the auto-generated `exec` tool description via introspection.
- **New module `agent/src/archie_agent/tools.py`** — a provider-neutral tool registry:
  `ToolSpec{name, description, schema, handler}` and `ToolRegistry` with `to_tool_config()`
  returning **neutral** schemas. Bedrock-specific shaping
  (`{"toolSpec":{"inputSchema":{"json":...}}}`) moves into `bedrock.py`, NOT the registry
  (nextgen's registry was Bedrock-shaped — we keep nexus provider-neutral).
- **`loop.py` gains tool awareness but stays pure**: `run_loop` takes new params `tool_config`
  and `execute_tool` (an async callable `(ToolUseBlock) -> ToolResultBlock` injected by the
  harness). The loop collects `ToolUseBlock`s from the stream (translating `ToolUseEvent`), and on
  `Done(stop_reason="tool_use")` yields new neutral events and re-invokes. Tool *execution* is
  performed by the injected callable — the loop never imports the runner.
- **`harness.py` owns orchestration**: builds the registry, provides `execute_tool` (a native
  coroutine that simply `await`s `run_exec` — `run_exec` uses `asyncio.create_subprocess_exec`, so
  do NOT wrap it in `asyncio.to_thread`), consumes the new loop events, persists incrementally, and
  broadcasts wire events.

### New neutral AgentEvents (events.py)
- `ToolCall(tool_use_id, name, input)` — model requested a tool (post-accumulation).
- `ToolResult(tool_use_id, content, is_error)` — result the harness fed back.
- These are provider-neutral, distinct from wire events.

### New wire events (shared/events.py)
- `ToolCallEvent(turn_index, tool_use_id, name, input_summary)` and
  `ToolResultEvent(turn_index, tool_use_id, is_error, summary)` for TUI rendering, added to
  `serialize_event`. (TUI rendering itself is a later plan; events must exist now.)

### Runner invocation & run dirs
- **Run-root is `/tmp/archie-runs/{ulid}/`**: runs are ephemeral IPC (write source, read result,
  discard). No need to persist model-generated Python to the host — the session log captures
  tool_call content (source) and tool_result content (formatted result) for full auditability.
  Base is a module constant `_RUNS_ROOT = Path("/tmp/archie-runs")`, **overridable via a
  `run_root` param** so tests point it at `tmp_path`. File IPC: `main.py` in, `result.json` out.
- Runner invoked as `["/opt/archie/venv/bin/python", "-m", "archie_agent.exec.runner", run_dir]`.
  The `PYTHONPATH` already includes the agent package (mounted at `/opt/archie/agent`,
  installed in the venv). Exec tools import via `from archie_agent.exec.tools import ...`.
- Interrupt → send `SIGTERM` to the runner process; it flushes a partial `Cancelled` envelope.

### Reuse boundaries (what ports vs. adapts)
- **Ports ~verbatim** (change imports `capabilities.` → `archie_agent.exec.tools.`, rename
  `@capability` → `@tool`, `CapabilityError` → `ToolError`, `get_all_capabilities` →
  `get_all_tools`): nextgen `sandbox/runner.py`, `sandbox/capabilities/{__init__,fs,shell}.py`.
- **Adapts**: registry (make neutral), exec host handler (subprocess in same container, not
  `docker exec`; runs under `/tmp` not workspace; drop artifact-store spill for v1 — just
  cap+truncate), loop wiring (rewrite against nexus `ContentBlock` types + async-gen bridge;
  do NOT lift nextgen's `AgentLoop` class).
- **Deferred** (later plans): web/code exec tools, artifact store, brain/recall/skill tools,
  in-turn eviction, ACP subagents, TUI tool rendering.

---

## Milestones

### 1. Provider-neutral tool registry + Bedrock shaping
Approach:
- Create `agent/src/archie_agent/tools.py` with `ToolSpec{name, description, schema: dict,
  handler}` and `ToolRegistry` (register/get/`to_tool_config()`). `to_tool_config()` returns
  NEUTRAL dicts `{name, description, input_schema}` — NOT Bedrock-shaped.
- Move Bedrock shaping into `bedrock.py`: a helper converts neutral tool configs to
  `{"toolSpec":{"name","description","inputSchema":{"json": schema}}}` before the API call.
- ⚠️ nextgen's `ToolRegistry.to_tool_config` (tools/__init__.py:65) is Bedrock-shaped — do NOT
  copy that shape; keep the registry provider-neutral (matches nexus's LLMClient abstraction).
Tasks:
- Add `tools.py` with `ToolSpec`/`ToolRegistry`.
- Add neutral→Bedrock conversion in `bedrock.py`; have `stream()` accept neutral config and shape
  it internally (prefer shaping inside bedrock, so `LLMClient` stays provider-neutral). The
  `LLMClient` protocol signature is UNCHANGED — `stream(...tool_config=None)` already accepts the
  neutral list; only its previously-`None` value now carries neutral tool dicts.
- Extend `FakeLLMClient` (`agent/src/archie_agent/llm/fake.py:31`) to record the last `tool_config`
  it was called with (e.g. `self.last_tool_config`) — it currently accepts but discards the arg
  (fake.py:35). Needed for the M1 AC and the M5/M6 tool-config assertions.
Deliverable: a `ToolRegistry` that produces neutral tool configs, convertible to Bedrock format.
Verify: `uv run pytest tests/test_tools.py` — register a dummy tool, assert neutral output and the
Bedrock-shaped conversion.

### 2. Exec tools (fs + shell) ported into the agent package
Approach:
- Create `agent/src/archie_agent/exec/tools/` with `__init__.py` (the `@tool`
  registry, `ToolError` hierarchy, `get_all_tools()`), `fs.py`, `shell.py`.
- Port from nextgen `sandbox/capabilities/{__init__,fs,shell}.py`; rename `@capability` →
  `@tool`, `CapabilityError` → `ToolError`, `get_all_capabilities` → `get_all_tools`; change
  internal imports to `from archie_agent.exec.tools import ...` (drop `code, web, discovery`
  for v1).
- Keep `_WORKSPACE = Path("/workspace")` and `/workspace`-relative path resolution.
- ⚠️ These are async and raise typed exceptions — the runner (M3) depends on that contract.
Edge cases:
- Path outside `/workspace` → `PathValidationError`. Binary file read → `BinaryFileError`.
  `edit` old-text not found/ambiguous → `EditError`. `shell` non-zero exit → returned as data.
Tasks:
- Port `__init__.py`, `fs.py`, `shell.py`; rename symbols; fix imports; drop unused tools.
- Add `tests/test_exec_tools.py` exercising each against a `tmp_path` (patch `_WORKSPACE`).
Deliverable: importable async exec tools operating on a workspace root, with typed errors.
Verify: `uv run pytest tests/test_exec_tools.py` — read/write/edit/grep/glob/shell happy + error paths.

### 3. exec runner subprocess
Approach:
- Port nextgen `sandbox/runner.py` to `agent/src/archie_agent/exec/runner.py` ~verbatim: contract
  validation (AST requires `async def main()` no required args), `compile`, `exec(code, ns)`,
  verify coroutine, `redirect_stdout/stderr`, `asyncio.run(main())`, envelope, `_wrap_for_audit`,
  SIGTERM partial-flush, `main_cli()` reading `<run_dir>/main.py` → writing `<run_dir>/result.json`.
- Namespace built from `get_all_tools()` (M2) + `asyncio`. Import path
  `archie_agent.exec.tools`.
- ⚠️ Runs as its OWN process with its OWN `asyncio.run` — never import/run this on the server loop.
- ⚠️ `# noqa: S102` on `exec(code, ns)` — intentional; the container is the boundary.
Edge cases:
- Missing `main.py` → `ContractError` envelope. No `main`/not async → `ContractError`.
  Syntax error → `SyntaxError` envelope. Unserialisable return → `repr` + `return_repr=True`.
  SIGTERM → partial `Cancelled` envelope with captured-so-far stdout/stderr.
Tasks:
- Port `runner.py`; fix exec tools import. **Keep the `main_cli()` + `if __name__ == "__main__"`
  guard** so `python -m archie_agent.exec.runner <run_dir>` resolves (the public `run(run_dir)`
  function is what tests call directly).
- Add `tests/test_runner.py` invoking `run(run_dir)` directly on a `tmp_path` run dir with scripted
  `main.py` sources (patch `_WORKSPACE`/exec tools import as needed).
Deliverable: a runner that turns a `main.py` into a `result.json` envelope.
Verify: `uv run pytest tests/test_runner.py` — return value, print capture, exception, contract
errors, tool call recorded in `calls`.

### 4. exec host handler (subprocess IPC) + registered `exec` tool
Approach:
- Create `agent/src/archie_agent/exec/tool.py`: `async def run_exec(source: str, *, run_root: Path
  = _RUNS_ROOT, python: str = _PYTHON) -> dict` (RESOLVED: returns a **`dict`** envelope, matching
  the runner's JSON `result.json`; no dataclass). Steps: mkdir `run_root/{ulid}`, write `main.py`,
  spawn `[python, "-m", "archie_agent.exec.runner", run_dir]` via `asyncio.create_subprocess_exec`,
  register the returned `Process` for cancellation (see Wiring), `await proc.wait()`, read
  `result.json`; on missing envelope synthesise a `RunnerCrash` envelope (include the runner's
  captured stderr). Constants: `_RUNS_ROOT = Path("/tmp/archie-runs")`,
  `_PYTHON = "/opt/archie/venv/bin/python"`.
- Format the model-facing string (return/stdout/stderr/error/duration) and cap length at
  `_MAX_RESULT_CHARS = 16_000` (RESOLVED — reuse nextgen's budget, exec_tool.py:27; drop the
  artifact-store spill for v1, just truncate with an elision marker).
- Build the `exec` `ToolSpec`: schema `{type:object, properties:{source:{type:string}},
  required:[source]}`; description auto-generated from exec tool signatures+docstrings. **Adapt,
  don't port**, nextgen `_generate_capability_docs` (exec_tool.py:271): call
  `get_all_tools()` (M2) directly and introspect each fn via `inspect.signature` +
  `inspect.getdoc().split("\n")[0]`. **Drop** nextgen's `sys.path` mutation / filesystem-relative
  discovery / `importlib.reload` — those exist only because nextgen imports a bare top-level
  `capabilities` module; nexus imports a normal package.
- Provide `create_registry()` returning a `ToolRegistry` with just `exec` registered.
- ⚠️ Path/venv are container-specific — `run_root`/`python` are params/constants so tests run the
  runner via `sys.executable` against a `tmp_path` run-root.
Wiring:
- State: `run_dir` per exec call; the active `asyncio.subprocess.Process` handle for interrupt.
- Producers: `run_exec` creates the run dir + process and exposes the handle to its caller.
- Consumers: the harness's `_execute_tool` sets `self._active_proc = proc` before `await proc.wait()`
  and clears it in `finally` (see M6); `harness.interrupt()` calls `_cancel_active_proc()`, which
  sends `SIGTERM` (matching the runner's handler, runner.py:308) then, after a short grace timeout
  (2s), `proc.kill()`. Decide the register/deregister split: **`run_exec` accepts an optional
  `on_start(proc)` callback** the harness uses to capture the handle — keeps `tool.py` free of
  harness state.
Edge cases:
- `asyncio.create_subprocess_exec` itself raises (e.g. `FileNotFoundError` on a bad python path,
  `OSError` on exec) → no process exists, so the missing-`result.json` path won't fire. Catch
  `OSError` around subprocess creation directly and synthesise a `RunnerCrash` envelope
  (`error` = the exception message).
- Runner exits without `result.json` → synthesise `RunnerCrash` envelope (`error` = stderr,
  `return` = None). Result too large → truncate at `_MAX_RESULT_CHARS` with a `[…truncated]` marker.
- SIGTERM sent but process ignores it → `proc.kill()` after 2s grace; result read may be a partial
  `Cancelled` envelope (runner flushes on SIGTERM) or a `RunnerCrash` if none written.
Tasks:
- Implement `run_exec` (with `on_start` callback + `_RUNS_ROOT`/`_PYTHON`/`_MAX_RESULT_CHARS`
  constants), `exec` `ToolSpec`, adapted description auto-gen, `create_registry()`.
- Add `tests/test_exec_tool.py` running `run_exec` end-to-end against a `tmp_path` run-root using
  `sys.executable` (parametrise `python` + `run_root`).
Deliverable: calling `run_exec("async def main(): return 21*2")` returns a formatted result with 42.
Verify: `uv run pytest tests/test_exec_tool.py` — happy path, runner-crash synthesis, description
contains exec tool docs.

### 5. Agentic multi-turn loop in loop.py (pure)
Approach:
- Extend `run_loop` signature: add `tool_config: list[dict] | None` and
  `execute_tool: Callable[[ToolUseBlock], Awaitable[ToolResultBlock]] | None`.
- In the worker/translation: accumulate `ToolUseEvent` → `ToolUseBlock`s (track
  `input_truncated`). Pass `tool_config` to `llm.stream(...)` (replace hardcoded None, loop.py:73).
- Restructure into an OUTER loop over LLM requests. Refactor the **existing** single-request
  worker+drain (loop.py:70-166) into an inner helper (e.g. `_stream_once(working_messages)`) that
  spawns the worker thread, drains the queue, and returns a **named result dataclass**
  `_RequestResult(text_blocks: list[TextBlock], tool_use_blocks: list[ToolUseBlock], stop_reason:
  str | None, usage: TurnUsage | None, interrupted: bool)` (define it in `loop.py` — avoid a bare
  tuple in this complex milestone). It must NOT yield the terminal `TurnDone` itself. The OUTER
  loop decides terminality: it yields `TurnUsage` after **every** request, but yields `TurnDone`
  **only** on the final (non-tool_use) request.
- After an inner stream: if there are collected `ToolUseBlock`s and `stop_reason == "tool_use"`,
  yield `ToolCall` events, `await execute_tool(block)` for each, yield `ToolResult` events, build
  ONE tool-result user turn (all results batched), append to a LOCAL working message list (the
  loop must NOT mutate the caller's `messages` — copy at entry), and re-invoke. Stop when no
  tool_use / stop_reason terminal / iteration cap hit.
- Add `max_iterations` param (default 25). On cap → yield `TurnFailed(error="max tool iterations")`.
- ⚠️ Keep purity: no persistence/WebSocket/session imports. Tool execution is the injected callable.
- ⚠️ The current `finally` block (loop.py:149-159) yields `TurnUsage`+`TurnDone` on normal
  completion — in the refactor that logic moves OUT of the per-request helper: the helper returns
  its buffered usage/stop_reason, and the outer loop emits `TurnUsage` per request and `TurnDone`
  once. Preserve the `terminated`/interrupt handling and `thread.join(timeout=5.0)` per request.
- ⚠️ The `yield`-in-`finally` caveat still applies to the per-request drain; keep the normal
  `async for` consumption contract.
Wiring:
- State: local `working_messages` (copy of input + appended tool-result turns), collected
  `ToolUseBlock`s per iteration, accumulated usage.
- Producers: the stream yields text/usage/tool events; `execute_tool` yields results.
- Consumers: the harness consumes `TextChunk`/`TurnUsage`/`ToolCall`/`ToolResult`/`TurnDone`/etc.
- Call site: `run_loop(messages=..., system=..., llm=..., interrupt=..., tool_config=cfg,
  execute_tool=harness_execute)`.
Edge cases:
- `stop_reason == "max_tokens"` WITHOUT tool_use → terminal: yield `TurnDone(stop_reason="max_tokens")`
  and stop (do NOT re-invoke — the model exhausted its output budget; re-invoking would likely just
  truncate again). `max_tokens` only continues the loop when the truncated response STILL contains
  complete tool_use blocks to execute; every re-invoke counts against `max_iterations`.
- Interrupt during tool execution → stop after the current tool, then **repair history**: iterate
  the full list of `ToolUseBlock`s collected from the interrupted response and, for every one
  WITHOUT a corresponding `ToolResultBlock`, synthesise an `is_error=True` "cancelled"
  `ToolResultBlock`; emit exactly ONE batched tool-result user turn so no `ToolUseBlock` is
  orphaned (Bedrock rejects orphans; app.py:133-150), THEN yield `TurnInterrupted`.
- `execute_tool` raises → produce an error `ToolResult` (is_error=True), feed back, continue.
Tasks:
- Add new events to `events.py` (`ToolCall`, `ToolResult`) AND add them to the
  `type AgentEvent = ...` union (events.py:53) so consumers type-check.
- Define the `_RequestResult` dataclass in `loop.py`.
- Rewrite `run_loop` into the outer/inner loop with tool handling; add copy-not-mutate for messages.
- Add `tests/test_loop.py` cases: single tool round-trip, two tools batched, iteration cap,
  interrupt mid-tool history repair, execute_tool raising → error result. Use `FakeLLMClient`
  scripted with `ToolUseStart`/`ToolUseEvent`/`Done(tool_use)` then a text `Done`.
Deliverable: a pure loop that drives tool round-trips until the model stops, with a safety cap.
Verify: `uv run pytest tests/test_loop.py` — all scenarios above pass; a mutation test asserts the
caller's `messages` list is unchanged.

### 6. Harness orchestration: execute_tool, incremental persistence, broadcast
Approach:
- In `harness.py`: build the registry (`create_registry()`), compute `tool_config =
  registry.to_tool_config()`, and define `async def _execute_tool(block) -> ToolResultBlock` that
  looks up the spec and `await`s the handler directly (the `exec` handler is a native coroutine
  using `create_subprocess_exec` — do NOT use `asyncio.to_thread`). Pass an `on_start(proc)`
  callback into `run_exec` that stores `self._active_proc`; clear it in `finally`. Wrap results
  into `ToolResultBlock`.
- Pass `tool_config` + `_execute_tool` into `run_loop`.
- Consume new events: on `ToolCall` → persist a `tool_call` `MessageEntry` (content =
  JSON-serialised `{"name": "exec", "source": "..."}`, metadata = None) + broadcast
  `ToolCallEvent`. On `ToolResult` → persist a `tool_result` `MessageEntry` (content =
  the formatted model-facing result string, metadata = None) + broadcast `ToolResultEvent`.
  On `TurnDone` (final) → persist assistant text.
- Accumulate usage across iterations via the existing `record_usage` path (session.py:97): each
  `TurnUsage` call sums the `total_*` accumulators. Note `_last_input_tokens` is intentionally
  **overwritten** (last-write-wins), not accumulated — it represents the most recent request's
  input for context-window estimation (session.py:83,89); only `total_*` accumulate. Broadcast
  cumulative `UsageUpdated`.
- Add `tool_call`/`tool_result`/`assistant(tool_use)` in-memory turns via `session.add_turn` so
  `/history` and re-invocation context stay correct. ⚠️ **In-memory turn `role` uses BEDROCK roles,
  NOT the audit roles**: the assistant response containing `ToolUseBlock`s is `role="assistant"`;
  the batched `ToolResultBlock` turn is `role="user"` (bedrock.py:54 serialises `turn.role`
  verbatim, and Bedrock requires tool_result under a user message). The `tool_call`/`tool_result`
  role strings are used ONLY for the human-readable JSONL `MessageEntry` audit log, never for
  in-memory `Turn.role`.
- ⚠️ Persistence stays best-effort (warn, don't crash) — matches 006.
- ⚠️ Interrupt: `self.interrupt()` sets the event; the loop repairs history. The harness must ALSO
  cancel any live runner subprocess: `_execute_tool` passes `on_start` to `run_exec` to capture the
  `Process` into `self._active_proc` (cleared in `finally`); `interrupt()` calls
  `_cancel_active_proc()` → `SIGTERM`, wait 2s, then `kill()` (per M4). This is the chosen wiring
  — the `on_start` callback keeps `tool.py` free of harness state.
Wiring:
- State: `registry`, `tool_config`, active runner proc handle for cancellation.
- Producers: `run_loop` yields events; `_execute_tool` runs tools.
- Consumers: `_broadcast` to WS clients; `write_entry` to JSONL.
- Call site: `handle_message` builds args and consumes `run_loop`.
Edge cases:
- Tool turn crash mid-way → JSONL has all entries up to the last completed message (per-message
  writes). Client reconnect → `/history` replays in-memory turns (assistant text/tool blocks).
Tasks:
- Add `create_registry` wiring, `_execute_tool`, active-proc cancellation.
- Extend event consumption for `ToolCall`/`ToolResult`; add `_persist_tool_call`/`_persist_tool_result`.
- Add `tests/test_harness.py` cases: full exec round-trip persists ordered entries + broadcasts
  tool events; interrupt mid-tool terminates runner + writes valid history; usage accumulates.
Deliverable: an end-to-end agentic turn (user → exec → result → answer) streamed, persisted, broadcast.
Verify: `uv run pytest tests/test_harness.py` with a `FakeLLMClient` scripting a tool round-trip;
assert JSONL entry order/roles, `FakeWebSocket` received tool events, and accumulated usage/cost.

### 7. Wire into app.py + system prompt + end-to-end check
Approach:
- `app.py` lifespan already builds `AgentHarness`; ensure the registry/tool_config are constructed
  there or inside the harness (prefer inside harness to keep app.py thin). Update the system prompt
  to instruct the model on the `exec` tool + available exec tools (concise; the detailed
  exec tool docs live in the tool description, not the system prompt).
- Confirm `/history` serializes the new tool blocks (it already handles `ToolUseBlock`/
  `ToolResultBlock`, app.py:133-150) — verify against real tool turns.
- ⚠️ Real end-to-end (with Bedrock) requires a live container; gate that as a manual smoke check,
  not a unit test. Unit coverage uses `FakeLLMClient`.
Tasks:
- Update system prompt; confirm harness constructs registry; verify `/history` with tool blocks.
- Add/adjust `tests/test_ws_integration.py` for a scripted tool turn over the WS path.
- Run full suite + ruff.
Deliverable: the running agent can execute a tool round-trip end-to-end (fake in tests; Bedrock
via manual smoke).
Verify: `uv run ruff check` clean and `uv run pytest -q` green (existing 132 + new). Manual smoke:
`archie` a session, prompt "list the files in the project", observe the model call `exec` running
`glob`/`shell` and answer.

---

## Out of scope (future plans)
- Web (`web_fetch`/`web_search`) and `code` (tree-sitter) exec tools.
- Artifact store / result spillover / `retrieve_artifact`.
- `brain_*`, `recall`, `skill` tools; skills catalog in the prompt.
- In-turn context eviction; ACP subagents.
- TUI rendering of tool activity (events are emitted now; rendering is later).

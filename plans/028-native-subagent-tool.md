# 028 — Native Subagent (`task`) Tool

## Objective

Add a native subagent capability so the primary agent can delegate a well-scoped task to
a role-specialised child agent that runs to completion and returns its result as a tool
result. Subagents are defined as Markdown files in `persona/agents/`, run as async tasks
*inside the same session container*, may use a different provider/model than the parent,
and surface progress in the TUI — both an inline read-only line and a full-screen live
detail view of a selected subagent. Multiple subagents may run concurrently under a bounded
semaphore, are individually addressable (the user can stop one or all), and their messages
persist attributed in the session transcript.

## Dependency: blocked on 029 M6

**This plan is BLOCKED until plan 029 milestone M6 lands the Subagent Scope Contract.**
Subagent implementation must not start until the canonical event contract is available.
029 M6 defines how subagent activity maps onto canonical session events: child `scope` =
launching `tool_use_id`, one `llm_request` per child provider request,
`(scope, turn_iteration, request_id)` request identity, parent reconstruction via
`parent_of(S) = tool_call(tool_use_id=S).scope`, and direct/inclusive cost aggregation by
scope (`scope_direct_costs` / `scope_inclusive_costs` in
`shared/src/archie_shared/session/accounting.py`). See the "Subagent Scope Contract" section
in `plans/029-project-canonical-session-events.md` (M6).

Note: this plan predates 029 and still describes flat wire fields (`turn_index`,
`parent_tool_use_id`). Reconciling those with the canonical `scope`/`turn_iteration` model
is a separate, reviewed 028-revision step (deferred by 029 M6); do not perform that rewrite
inline.

## Context

archie-nexus currently has no subagent/delegation primitive — the primary agent does all
work in a single `run_loop()`. This plan adds the first one. It was designed after
researching eight reference implementations in `../_research/` (opencode, amcp, codex,
cline, aloop, maki, cli-agent-orchestrator, openclaw) and inspecting the Nexus codebase.

Key facts that shaped the design (verified against the codebase):

- **Architecture is single-container-per-session.** One `archie start` → one Docker
  container (`archie-{session_id}`) → one `AgentHarness` → one agent loop → one
  WebSocket. Subagents therefore run as **async tasks inside the parent's container**
  (sharing filesystem, git checkout, credentials, event loop), NOT as separate
  containers. A separate-container design was explicitly rejected as disproportionate
  infrastructure for a solo-dev tool.
- **`run_loop()` is fully injectable** (`agent/src/archie_agent/loop.py:80`): it takes
  `messages`, `system`, `llm`, `interrupt`, `tool_config`, `execute_tool`,
  `max_iterations`. A child agent is a direct reuse of `run_loop()` with a child system
  prompt, child registry, and child llm client.
- **The `skill` tool is the template** (`agent/src/archie_agent/skills.py:142`
  `create_skill_tool`): a factory that closes over harness state and returns a
  `ToolSpec`. The subagent tool follows the same shape.
- **`_execute_tool` dispatches generically** (`agent/src/archie_agent/harness.py:356`, with
  the generic branch at `:384`): `result = await spec.handler(**block.input)`. The subagent
  tool needs no special-casing there (unlike `exec`).
- **The harness does NOT currently hold the model catalog, region, or config.** `__init__`
  (`harness.py:100-140`) receives only `session, llm_client, model_name, log_dir,
  exec_python, exec_run_root`. The model-load chain and config live at module scope in
  `app.py`'s `lifespan` (`app.py:91-95`, `115-120`) and are NOT passed to the harness. This
  plan therefore MUST extend `AgentHarness.__init__` to receive what subagents need
  (model catalog, region, subagent limits) — see Milestone 3.
- **`exec` subprocess cancellation uses a SINGLE `self._active_proc` field**
  (`harness.py:137`, `_on_proc_start:399`, `_cancel_active_proc:403`). Concurrent children
  each running `exec` would clobber this single slot — the child dispatch MUST NOT reuse the
  harness's `_active_proc`; each child needs its own active-proc tracking.
- **`_build_tools()` is STATIC** (`prompt.py:70-80`): it emits the persona tools-strategy
  file + aggregated guidelines and does NOT enumerate individual tools or mention `task`.
  It is therefore safe to reuse verbatim in the child prompt (resolves the Milestone 2
  open question).
- **Tool execution in `run_loop` is strictly sequential** (`loop.py` per-tool loop).
  Parallel subagents therefore need NEW fan-out code (`asyncio.gather` + semaphore) inside
  the subagent tool handler — the loop itself stays sequential.
- **Wire protocol is flat**, events keyed by `turn_index`, no parent/child concept
  (`shared/src/archie_shared/events.py`, `PROTOCOL_VERSION = 1` at `events.py:21`). Nested
  subagent progress requires new wire events tagged with a `parent_tool_use_id`, registered
  in `_SERVER_EVENT_TYPES` (`events.py:407-419`) and dispatched in the TUI
  (`cli/.../tui/app.py`, `_handle_event` at `:304`). Wire events are **frozen dataclasses**
  with hand-written `to_json`/`from_json` (NOT msgspec — msgspec is only for config structs).
  `deserialize_event` (`events.py:433-442`) special-cases session-level (no-`turn_index`)
  classes in a hardcoded tuple `(SessionInfo, ModelSwitched, StatusUpdated)`; new subagent
  events carry `turn_index` and MUST NOT be added to that tuple.
- **`AgentConfig` is an empty placeholder** (`shared/src/archie_shared/schemas.py:30`) —
  the natural home for subagent config.
- **Cost/usage already exists**: `Session.record_usage(input_tokens, output_tokens,
  cache_read_tokens=0, cache_write_tokens=0)` (`agent/src/archie_agent/session.py:111`) +
  `calculate_cost()` (`models.py:199`).
- **Child llm client construction pattern** (`agent/src/archie_agent/app.py:91-95`, at
  module/lifespan scope): `load_models(home_dir()/"models.yaml")` → `get_model(catalog,
  key)` → `create_llm_client(model, region)`. The default max iterations constant is
  `_DEFAULT_MAX_ITERATIONS = 25` (`loop.py:54`, private — reference it explicitly).

Design decisions (the seven open questions), resolved with the user before planning:

1. **Providers** — child builds its own `create_llm_client` from its definition's
   `provider`/`model`; falls back to `global.model`. Fully decoupled from parent.
2. **UI** — read-only nested progress via new `parent_tool_use_id`-tagged wire events;
   TUI shows an indented activity line + cumulative cost. No interjection in v1.
3. **Questions** — subagents crack on; they cannot ask the user in v1. (Future: `can_ask`.)
4. **Parallelism** — `asyncio.gather` + `asyncio.Semaphore(max_concurrent)`, default 3.
5. **Cost/log** — reuse `Session.record_usage()`/`calculate_cost()`; usage tagged with the
   subagent name; cumulative cost shown on the tool header.
6. **Stop conditions** — per-subagent `max_iterations` (configurable, reuses loop cap);
   depth guard: subagents CANNOT spawn subagents; optional session budget.
7. **Prompt** — focused/role-scoped: child prompt = definition body + environment + its
   own tools/skills catalog, EXCLUDING the primary identity/persona and tone slots.

Context model: **FRESH only** — each child starts with empty message history; the parent
must pass all needed context in the task string. No history inheritance (fork) in v1.

Definition format (user-settled): `persona/agents/<name>.md`, YAML frontmatter with
`name`, `description`, `provider`, `model`, `skills` (list); Markdown body = child system
prompt. `persona/agents/` does not exist yet (greenfield).

---

## Requirements

### Agent definitions
- MUST discover agent definitions from `persona/agents/*.md` at harness construction,
  mirroring `discover_skills()`.
  - AC: a well-formed `persona/agents/researcher.md` appears in the catalog; a malformed
    file is skipped with a logged warning (not a crash).
- Frontmatter fields: `name` and `description` MUST be required; `provider`, `model`,
  `skills` MUST be optional.
  - AC: a definition omitting `provider`/`model` resolves to the session's active model.
  - AC: a definition listing `skills: [research]` makes only those skills available to the
    child (see Skills scoping).

### The `task` tool
- MUST register a `task` tool on the harness registry alongside `exec` and `skill`.
  - AC: `task` appears in `to_tool_config()` and the system prompt's tool list.
- The tool MUST accept a list of one or more sub-tasks, each with `agent` (definition
  name) and `prompt` (the task string), and run them, returning a combined result.
  - AC: calling `task` with two sub-tasks runs both and returns both results labelled by
    agent/index.
  - AC: an unknown `agent` name returns an error result naming available agents (does not
    crash the turn).
- Each subagent MUST run `run_loop()` to completion (final assistant text) and that text
  MUST become the sub-task's result content.
  - AC: a subagent that produces final text `X` yields a tool result containing `X`.

### Provider/model independence
- A subagent MUST use its definition's `provider`/`model` when present, else the session's
  active model.
  - AC: a definition specifying an Ollama model runs on Ollama while the parent is on
    Bedrock (verified via the model_id on the child client).

### Focused prompt
- The child system prompt MUST include the definition body, environment, and the child's
  own tools/skills catalog, and MUST exclude the primary identity/persona and tone
  sections.
  - AC: the child prompt string contains the definition body and does NOT contain the
    primary identity text.

### Skills scoping
- A child MUST only be offered the skills named in its definition's `skills` list; an empty
  or absent list means no skills.
  - AC: the child's skills catalog section lists only declared skills.

### Parallelism & limits
- Concurrent subagent execution MUST be bounded by a configurable `max_concurrent`
  (default 3).
  - AC: with `max_concurrent: 1`, two sub-tasks execute without exceeding one in-flight at
    a time (observable via ordering/logging).
- Each subagent MUST be bounded by a configurable `max_iterations` (default: the loop's
  existing cap).
  - AC: a runaway subagent stops at `max_iterations` and returns a truncation notice.

### Depth guard
- A subagent MUST NOT be able to spawn further subagents.
  - AC: the child registry does not contain the `task` tool.

### Observability
- Each subagent's token usage MUST be recorded via `Session.record_usage()`.
  - Child cost MUST be computed from the CHILD's `ModelEntry.cost` (a child may run on a
    different provider/model than the parent), not the parent session model. `record_usage`
    accumulates in-memory totals only and does NOT tag by agent; per-agent attribution lives
    in the session transcript (see Session persistence) and in the tagged wire events, not
    in `Session` totals.
  - AC: session totals increase by the child's usage after a `task` call, priced by the
    child's model.
- The TUI MUST display read-only nested progress for running subagents, associated with the
  spawning tool call, in the main conversation view. The default (collapsed) view shows, per
  running child, a small rolling window (~3 lines) summarising current activity/status —
  agent name, a live activity indicator, latest tool/text activity, and cumulative cost.
  - AC: running a `task` shows an indented, multi-line (~3) activity block per child under
    the tool call; each updates live and resolves when its subagent completes.
- The TUI MUST offer a full-screen detail view of a single selected subagent showing its
  LIVE output (streaming assistant text + its own tool calls/results as they happen).
  - AC: with a `task` running, the user can open a picker, select a subagent, and watch its
    text and tool activity stream in a full-screen view; closing it returns to the
    conversation with inline progress intact.
- Child streaming (text deltas + tool call/result) MUST be emitted on the wire tagged with
  the spawning `parent_tool_use_id` and the subagent index, in addition to lifecycle/usage.
  Streaming is ALWAYS emitted (not gated on a subscription and not behind a config flag);
  clients decide what to render. The collapsed inline view consumes it as a rolling ~3-line
  summary; the full-screen view renders it in full.
  - AC: the wire carries per-child text/tool events distinguishable by
    `(parent_tool_use_id, index)`, emitted regardless of whether any client has the
    full-screen view open.

### Interaction constraints
- Subagents MUST NOT prompt the user; they complete autonomously or report a blocker in
  their result.
  - AC: no user-input path is reachable from a child loop.

### Interrupt & cancellation
- A user interrupt during a `task` call MUST be able to stop EITHER all in-flight subagents
  OR one specific subagent (stopping the targeted loop(s) and any `exec` subprocesses they
  started).
  - AC (stop all): an untargeted interrupt mid-`task` stops every running child promptly;
    the `task` result reports interruption; the session returns to idle.
  - AC (stop specific): a targeted interrupt for one `(parent_tool_use_id, index)` stops
    only that child; siblings continue and the batch still returns their results.
- `InterruptCommand` MUST support an optional target `(parent_tool_use_id, subagent_index)`;
  absent target means stop-all (today's behaviour, unchanged).
  - AC: a targeted `InterruptCommand` sets only the matching child's interrupt event.

### Session persistence of subagent messages
- Subagent messages MUST be persisted to the single session transcript, attributed to their
  originating subagent, without corrupting concurrent writes.
  - AC: `MessageEntry` carries `parent_tool_use_id` and `agent_id` (and index) for
    child-originated lines; parent lines leave them null.
  - AC: with two concurrent subagents writing, no line is torn/interleaved and every child
    line is attributable by its fields. (On the single asyncio event loop `write_entry` is
    synchronous with no internal `await`, so writes cannot interleave; no explicit lock is
    required — see M10.)

### Config
- Subagent settings MUST live under the `agent` config section.
  - AC: `agent.subagents.max_concurrent` and `agent.subagents.max_iterations` load from
    `config.yaml`; absent config yields defaults.

---

## Design

### Where code lives
- **`agent/src/archie_agent/agents.py`** (NEW) — agent definition discovery + the
  `create_task_tool` factory. Mirrors `skills.py` structure (`AgentEntry` dataclass,
  `discover_agents()`, `_parse_agent_file()`, `create_task_tool(...)`).
- **`agent/src/archie_agent/prompt.py`** — add `build_subagent_prompt(...)` that omits the
  identity/tone sections. Reuses `_build_environment`, `_build_tools` (static — safe as-is),
  `_build_skills_catalog`, `_build_loaded_skills`, `_build_project_context`. Chosen over a
  boolean flag on `build_system_prompt` to keep the two prompt shapes explicit.
- **`agent/src/archie_agent/harness.py`** — (a) extend `__init__` to receive the model
  catalog, region, and subagent limits; (b) extract a reusable child tool-dispatch helper;
  (c) construct the agent catalog and register the `task` tool next to the skill tool
  (~line 133).
- **`agent/src/archie_agent/app.py`** — pass `_catalog`, `_config.global_.region`, and
  `_config.agent.subagents` into `AgentHarness(...)` at `app.py:115`.
- **`shared/src/archie_shared/events.py`** — new wire events (see Wiring): subagent
  lifecycle (start/usage/end) AND per-child streaming (text delta + tool call/result), all
  tagged with `parent_tool_use_id` + subagent index.
- **`shared/src/archie_shared/commands.py`** (wherever `InterruptCommand` lives) — extend
  `InterruptCommand` with an optional `target: (parent_tool_use_id, subagent_index)`.
- **`shared/src/archie_shared/session/log.py`** — add `parent_tool_use_id` + `agent_id`
  (+ index) to `MessageEntry`. `write_entry` (`log.py:53-63`) stays SYNCHRONOUS with no lock:
  on the single event loop it has no internal `await`, so concurrent children cannot
  interleave a write (see M10 / Session-persistence AC).
- **`shared/src/archie_shared/schemas.py`** — `SubagentsConfig` struct + `subagents` field
  on `AgentConfig`.
- **`cli/src/archie_cli/tui/`** — dispatch new wire events in `app.py` `_handle_event`
  (`:304`); render inline COLLAPSED per-child activity (~3 rolling lines) in the
  conversation/iteration block widgets; add the FIRST `ModalScreen` (full-screen subagent
  detail) + a `SubagentProvider` command-palette picker; wire targeted + stop-all interrupt
  actions.

### Reuse, don't re-decide
- Child agent execution reuses `run_loop()` verbatim; only its inputs differ.
- Child llm client uses the existing `load_models`/`get_model`/`create_llm_client` chain.
- Child registry is `create_registry()` (exec) + a scoped `skill` tool, but NOT the `task`
  tool (depth guard).
- Child usage reuses `Session.record_usage()`, but cost is computed from the CHILD's
  `ModelEntry.cost` (via `calculate_cost()`), not the parent session model — a child may run
  on a different provider. `record_usage` totals are NOT agent-tagged; per-agent attribution
  lives in the transcript + wire events.
- Child streaming/lifecycle reaches the wire ONLY via the harness `_broadcast` callable
  threaded into `create_task_tool`; the child loop is its own event consumer inside the tool
  handler (see M4/M8). Streaming is emitted ALWAYS (not gated, no flag).
- New wire events follow the existing frozen-dataclass + `to_json`/`from_json` +
  `_SERVER_EVENT_TYPES` registration pattern exactly.

### Deviations from established patterns
- **Parallel fan-out is new**: the existing loop runs tools sequentially. The `task`
  handler introduces `asyncio.gather` + `asyncio.Semaphore`. This is contained entirely
  within the tool handler; the loop is untouched.
- **Nested wire events are new**: the flat protocol gains events carrying
  `parent_tool_use_id` + subagent index, including per-child STREAMING (text/tool), not just
  lifecycle. `PROTOCOL_VERSION` MUST be bumped to 2 and both agent and cli updated in
  lockstep (they ship together).
- **Harness constructor grows**: `AgentHarness.__init__` gains the model catalog, region,
  and subagent limits (previously module-scope in `app.py`). This is a deliberate,
  contained widening of the constructor — all existing call sites (tests included) must be
  updated with the new optional/required params.
- **First TUI `ModalScreen`**: no screen/modal infrastructure exists today (flat
  single-screen app). The full-screen subagent view introduces the first `ModalScreen` and
  the first per-subagent view-state (the app's turn/tool state is currently flat
  single-agent fields).
- **`InterruptCommand` gains a target**: today interrupt is untargeted (stop everything).
  A targeted variant routes to a single child's interrupt event; untargeted stays stop-all.
- **Session log schema grows (no lock)**: `MessageEntry` gains attribution fields. Unlike an
  earlier draft, `write_entry` does NOT gain a lock — the single-event-loop sync write can't
  interleave. Existing single-agent writes are unaffected (new fields null).
- **Child dispatch owns isolated state**: unlike the harness's single `_active_proc` /
  shared `_pending_tools`, each concurrent child gets its OWN active-proc slot and pending-
  tool map so children don't clobber each other (see M3).

---

## Milestones

### 1. Agent definition discovery
Approach:
- Create `agent/src/archie_agent/agents.py` modelled on `skills.py:32-134`. Define
  `AgentEntry` (name, description, provider, model, skills, body, path) and
  `discover_agents() -> dict[str, AgentEntry]` scanning `persona_dir()/agents/*.md`.
- Parse YAML frontmatter with `yaml` (already a dep, used by skills). `name` +
  `description` required; `provider`/`model`/`skills` optional. `skills` defaults to `[]`.
- ⚠️ Malformed files MUST be skipped with a logged warning, matching `_parse_skill_file`
  behaviour — never raise during discovery.
Edge cases:
- Missing `persona/agents/` dir: return empty catalog (no error).
- Duplicate `name` across files: last-wins, log a warning.
- `skills` referencing an unknown skill: keep it in the entry; resolution/filtering happens
  at spawn time (Milestone 4), warn there.
Tasks:
- Add `AgentEntry` dataclass and discovery/parse functions.
- Create `persona/agents/` with one example definition (e.g. `researcher.md`) for testing.
- Unit tests: well-formed parse, malformed skip, missing dir, duplicate name.
Deliverable: `discover_agents()` returns a catalog from `persona/agents/*.md`.
Verify: `pytest tests/test_agents.py` (new) passes, covering the four cases above.

### 2. Focused subagent system prompt
Approach:
- In `prompt.py`, add `build_subagent_prompt(model_name, body, workspace_dir=str(WORKSPACE),
  *, catalog=None, loaded_skills=None)` assembling `[body, _build_environment(model_name,
  workspace_dir), _build_tools()]` + scoped skills catalog (`_build_skills_catalog`) +
  AGENTS.md context (`_build_project_context`) + loaded skill bodies. It omits
  `_build_identity()`. Chosen as a separate function (not a flag) to keep the two prompt
  shapes explicit.
- `_build_tools()` is STATIC (verified `prompt.py:70-80`: strategy file + guidelines, no
  per-tool enumeration, no `task` mention) — reuse verbatim, no change needed.
Tasks:
- Add `build_subagent_prompt`.
- Unit test asserting the child prompt contains the body, INCLUDES the universal sections
  (environment + tools + project-context), and excludes identity text.
Deliverable: a function producing a focused child system prompt.
Verify: `pytest` — new test asserts body present, project-context/environment/tools present,
identity absent.

### 3. Harness threading + reusable child tool-dispatch
Approach:
- Extend `AgentHarness.__init__` (`harness.py:100`) to receive `model_catalog: dict[str,
  ModelEntry]`, `region: str`, and `subagents: SubagentsConfig` (or the two ints). Default
  them to keep existing tests working, but update `app.py:115` to pass `_catalog`,
  `_config.global_.region`, and `_config.agent.subagents`. Store on self for the task
  factory closure.
- Extract child tool-dispatch into a standalone helper (module function in `agents.py` or a
  small class) that takes a registry + per-child exec config and returns an
  `execute_tool(block) -> ToolResultBlock` coroutine. It mirrors `harness._execute_tool`
  (`:356`) BUT owns its OWN state — it MUST NOT touch `harness._active_proc` (single field,
  `:137`, would be clobbered by concurrent children) NOR the harness `_pending_tools` map
  (`harness.py:243/261`, keyed by `tool_use_id` — concurrent children would collide). Each
  child dispatch owns its own active-proc slot AND its own pending-tool map.
- ⚠️ The child dispatch's `exec` branch needs its own `on_start`/`_active_proc`/cancel
  wiring so each child can cancel its own subprocess independently. Model it on
  `_on_proc_start:399` / `_cancel_active_proc:403` / `_kill_proc:420` but per-child.
- Reuse the harness's exec runner config (`_exec_python`, `_exec_run_root`) so child `exec`
  runs against the same runtime.
Wiring:
- State: `model_catalog`, `region`, `subagents` stored on harness; each child dispatch holds
  its own `_active_proc` reference AND its own `_pending_tools` map.
- Producers: `app.py` populates the new constructor args.
- Consumers: the task factory (Milestone 4) reads catalog/region/limits from the harness.
Tasks:
- Widen `AgentHarness.__init__`; update `app.py` and ALL test call sites
  (`tests/test_harness.py`, `test_native_dispatch.py`, `test_model_switch.py`).
- Extract the child dispatch helper with independent active-proc tracking + a cancel hook.
- Unit test the child dispatch: generic tool call returns a result; a child `exec` runs; a
  cancel hook terminates the child's subprocess without touching the harness's slot; two
  concurrent child dispatches with the same `tool_use_id` do not collide (isolated
  pending-tool maps).
Deliverable: harness exposes catalog/region/limits, and a reusable child dispatch exists
with isolated subprocess cancellation.
Verify: `pytest` — existing harness tests still pass with new args; new dispatch test
asserts result + isolated cancel.

### 4. `task` tool: single subagent, sequential
Approach:
- In `agents.py`, add `create_task_tool(*, agent_catalog, skill_catalog, session,
  model_catalog, region, exec_config, broadcast, max_iterations)` returning a `ToolSpec`
  named `task`. The `broadcast` param is the harness `_broadcast` callable (`harness.py:512`)
  threaded in explicitly — it is the ONLY channel by which child streaming/lifecycle events
  reach the wire, because the child loop runs INSIDE this tool handler (`spec.handler(...)`,
  `harness.py:384`), NOT inside `handle_message`'s consumer loop where the parent's
  `_broadcast` calls normally live. A `spawn_child_llm(entry)` helper resolves the child
  model: if entry has `provider`/`model`, build a `ModelEntry` / look it up in
  `model_catalog`; else use the session's active model. Then `create_llm_client(model,
  region)`. Keep a reference to the resolved child `ModelEntry` — its `cost` prices this
  child's usage.
- Handler, per sub-task: resolve agent entry; build child registry (`create_registry()` +
  scoped `skill` tool, NO `task`); build child prompt via `build_subagent_prompt`; create a
  per-child `threading.Event`; then run the child as its OWN consumer loop:
  `async for child_event in run_loop(messages=[<task as user turn>], system=...,
  llm=child_llm, interrupt=<child event>, tool_config=child_config,
  execute_tool=<child dispatch>, max_iterations=max_iterations): ...`. Inside that loop the
  handler is the per-child event consumer (mirroring `handle_message`, `harness.py:175-349`,
  but for the child): it (a) `await broadcast(...)`s the matching tagged wire event for each
  child `TextDelta`/`ToolCall`/`ToolResult`/lifecycle, and (b) on `Usage` records usage on
  `session`, pricing the delta from the CHILD `ModelEntry.cost` (do NOT reuse the parent
  session model — a child on a different provider would be mis-priced), and collects final
  text. There is no other producer path; if `broadcast` is not called here, nothing reaches
  the wire.
- Register `task` in `harness.__init__` after the skill tool (~line 133); rebuild
  `_tool_config`.
Wiring:
- State: agent catalog (built in `__init__`, like `_skill_catalog`); child usage funnels
  into the existing `Session` via `record_usage`.
- Producers: the `task` handler is itself the child-loop consumer — it records child usage
  (priced by the child `ModelEntry`) and broadcasts tagged wire events as child events arrive.
- Consumers: `Session` totals; parent turn's final result string.
- Call site: `run_loop` in `harness.handle_message` dispatches the model's `task` tool_use
  through `_execute_tool` → `spec.handler(**input)` (no special-casing needed).
Edge cases:
- Unknown `agent` name: return error content listing available agents; not an exception.
- Child loop raises: catch, return an error result for that sub-task.
- Child hits `max_iterations`: return final text with a truncation notice.
- Empty `prompt`: return an error result.
Tasks:
- Implement `create_task_tool` (single-item path first) using the Milestone 3 dispatch.
- Register the tool; rebuild `_tool_config`.
- Unit test with `FakeLLMClient`: a single sub-task returns the child's final text; unknown
  agent errors cleanly; usage recorded on the session; a fake `broadcast` receives the
  child's tagged lifecycle/streaming events (proves the producer path); child usage is
  priced from the child model's cost, not the parent's.
Deliverable: model can call `task` with one sub-task and receive the child's result.
Verify: `pytest tests/test_task_tool.py` (new) with a fake llm; assert result text, error
handling, and `session` usage delta.

### 5. Skills scoping + depth guard
Approach:
- Child registry MUST omit `task` (depth guard). Child `skill` tool is created over a
  catalog filtered to the definition's `skills` (subset of `discover_skills()`), with a
  fresh `loaded_skills` list per child.
- ⚠️ A `skills` entry naming an unknown skill: drop it and log a warning at spawn.
Edge cases:
- Definition with empty/absent `skills`: child gets no skill tool skills section.
Tasks:
- Filter the skill catalog per definition; build the scoped `skill` tool for the child.
- Assert child registry has no `task` entry.
- Tests: child registry excludes `task`; scoped catalog contains only declared skills;
  unknown declared skill is dropped + warned.
Deliverable: children are skill-scoped and cannot recurse.
Verify: `pytest` — assert `child_registry.get("task") is None` and catalog contents.

### 6. Parallel fan-out with bounded concurrency
Approach:
- Extend the `task` handler to accept N sub-tasks and run them with `asyncio.gather` under
  `asyncio.Semaphore(max_concurrent)`. Each child runs in its own coroutine; per-child
  exceptions are caught and turned into per-child error results (do not fail the batch).
- ⚠️ `run_loop` bridges the provider `stream()` via a daemon thread + `asyncio.Queue`
  (`loop.py`). Running several concurrently is fine (each has its own generator/thread — note
  thread count scales with `max_concurrent`), but each child MUST have its OWN
  `threading.Event` interrupt — do NOT share the parent's (see the Interrupt & cancellation
  requirement + M3's per-child dispatch). Each child is also its own event consumer that
  broadcasts tagged events (see M4/M8 for the producer path — `broadcast` is threaded into
  `create_task_tool`).
- Maintain a per-`task` **child registry** keyed by `(parent_tool_use_id, index)` →
  `(interrupt_event, cancel_fn)`, where `cancel_fn` cancels that child's `exec` subprocess
  (the isolated cancel hook from M3). Storing only the `threading.Event` is NOT enough: a
  targeted interrupt (M11) must both `.set()` the event AND call `cancel_fn`, mirroring
  `harness.interrupt()` (`harness.py:351-354`) which does both. Register on spawn, deregister
  on completion.
Wiring:
- State: `asyncio.Semaphore(max_concurrent)` + the child-event registry, created per `task`
  call and reachable by the interrupt handler.
- Producers/consumers: `gather` collects ordered results; combined into one labelled result
  string (by agent + index).
Edge cases:
- One child fails, others succeed: batch returns mixed results, failures labelled.
- `max_concurrent: 1`: effectively sequential; ordering preserved in output.
Tasks:
- Add the gather + semaphore logic + the keyed child-event registry; combine results
  deterministically by input order.
- Tests: two sub-tasks both complete; `max_concurrent=1` serialises; one failing child does
  not sink the others; registry holds one `(event, cancel_fn)` per in-flight child.
Deliverable: `task` runs multiple subagents concurrently under the limit, each addressable.
Verify: `pytest` — assert both results present, order stable, semaphore bound respected
(via an instrumented fake llm that records concurrent entries).

### 7. Config wiring
Approach:
- In `schemas.py`, add `SubagentsConfig(msgspec.Struct, forbid_unknown_fields=True)` with
  `max_concurrent: int = 3` and `max_iterations: int` defaulting to the loop's
  `_DEFAULT_MAX_ITERATIONS` (`loop.py:54`, currently 25 \u2014 reference the constant, do not
  hardcode a literal). Add
  `subagents: SubagentsConfig = msgspec.field(default_factory=SubagentsConfig)` to
  `AgentConfig`.
- Thread the values from `NexusConfig` into the harness (via `app.py` construction) and into
  `create_task_tool`.
Edge cases:
- Absent `agent.subagents`: defaults apply.
- `forbid_unknown_fields` rejects typos — acceptable (matches existing structs).
Tasks:
- Add the struct + field; pass limits through `app.py` → harness → task tool.
- Tests: config load with and without the section; values reach the tool.
Deliverable: subagent limits are configurable via `config.yaml`.
Verify: `pytest tests/test_config*.py` — assert defaults and overrides load.

### 8. Subagent wire events: lifecycle + live streaming
Approach:
- In `events.py`, add subagent events carrying `parent_tool_use_id` + `index`:
  - Lifecycle: `SubagentStart(turn_index, parent_tool_use_id, index, agent)`,
    `SubagentUsage(..., input_tokens, output_tokens, cost_usd)`,
    `SubagentEnd(..., is_error, summary)`.
  - Streaming (for the full-screen view): `SubagentTextDelta(..., text)`,
    `SubagentToolCall(..., tool_use_id, name, input_summary)`,
    `SubagentToolResult(..., tool_use_id, is_error, summary)`.
  Follow the existing frozen-dataclass + `to_json`/`from_json` pattern and register each in
  `_SERVER_EVENT_TYPES`. All carry `turn_index` (do NOT add to the no-`turn_index` tuple in
  `deserialize_event`).
- ⚠️ Bump `PROTOCOL_VERSION` to 2. Agent and CLI ship together; update both. NOTE: this is
  informational only — no handshake/version negotiation currently reads it (verified: no
  version check exists). Bump it for documentation; do not expect it to gate anything unless
  a check is added.
- The child event consumer (M4) forwards each child `AgentEvent` as the matching tagged wire
  event via the `broadcast` callable threaded into `create_task_tool`. Streaming events are
  emitted ALWAYS (per locked decision), NOT gated on any client subscription and NOT behind a
  config flag — clients decide what to render. `_broadcast` (`harness.py:512`) already fans
  out to all connected clients.
Wiring:
- State: none persistent; events flow child-loop → `_broadcast` → WebSocket → TUI.
- Producers: `task` handler / child event consumer.
- Consumers: TUI inline progress (M9) + full-screen view (M12).
Tasks:
- Add the six events + registration + `PROTOCOL_VERSION` bump.
- Broadcast them from the child event consumer at the right points.
- Round-trip tests: `serialize_event`/`deserialize_event` for each new event.
Deliverable: subagent lifecycle AND live text/tool activity are on the wire (always emitted),
tagged by `(parent_tool_use_id, index)`.
Verify: `pytest tests/test_events*.py` — serialize/deserialize round-trip for new events.

### 9. TUI: inline read-only nested collapsed activity view
Approach:
- In `cli/.../tui/app.py`, import and dispatch the new subagent events in `_handle_event`
  (`app.py:304`) alongside existing tool events. Associate them with the spawning tool call
  via `parent_tool_use_id` (the pending-tool tracking around the `ToolCall` branch,
  `app.py:408`; `IterationBlock._tool_entries` keyed by `tool_use_id`, `conversation.py:409`).
- Render, per child, an indented COLLAPSED activity block under the parent tool call: agent
  name + live indicator on `SubagentStart`; a rolling window of the last ~3 activity lines
  built from the streaming events (latest `SubagentToolCall`/`SubagentToolResult` summaries
  and/or condensed `SubagentTextDelta`); cumulative cost on `SubagentUsage`; final
  success/error + summary on `SubagentEnd`. This block consumes the SAME streaming events the
  full-screen view (M12) uses — it just keeps only the last ~3 lines rather than full history.
- ⚠️ Session-level dispatch at `app.py:213` only immediate-dispatches events without a
  `turn_index`; the new events HAVE `turn_index`, so they route through normal turn
  buffering. All child events share the PARENT's `turn_index`; the dedup rule
  `turn_index > last_turn_index` (`app.py:147`) drops equal-turn events once the parent turn
  is in history. This is acceptable because the inline block is LIVE-ONLY (best-effort): it
  must render correctly for the ACTIVE turn; it is NOT reconstructed on reconnect (deltas are
  not in `/history`). Verify active-turn child events survive buffering and render in the
  correct turn block.
Edge cases:
- Subagent errors: the activity block shows an error state, not a crash.
- Multiple concurrent subagents under one `task`: each gets its own ~3-line block.
- Reconnect mid-`task`: inline blocks may be empty/partial for already-buffered turns —
  acceptable (live-only).
Tasks:
- Dispatch the events; add per-child rolling ~3-line rendering to the iteration block widget.
- Widget unit test for the rolling buffer (keeps last N lines, updates on new events).
- Manual/observed verification of nesting + activity + cost.
Deliverable: running a `task` shows indented, live, read-only ~3-line activity blocks per
subagent with cost, for the active turn.
Verify: run `archie`, invoke a `task` with two subagents; observe two indented activity
blocks that update and resolve. (Rolling-buffer logic unit-tested; final check visual.)

### 10. Session-log attribution (concurrent-write safety)
Approach:
- In `shared/src/archie_shared/session/log.py`, add optional `parent_tool_use_id: str | None`,
  `agent_id: str | None`, `subagent_index: int | None` to `MessageEntry` (`log.py:35-50`).
  Parent lines leave them null; back-compat on read (default null).
- Concurrency: `write_entry` (`log.py:53-63`) is a SYNCHRONOUS `def` doing `open('a')+write()`
  with no internal `await`. On the single asyncio event loop two children cannot interleave
  mid-write, so NO lock is added (locked decision m-2). Keep it sync + append mode. Note this
  invariant in a comment so a future move to thread-pool writes revisits it.
- The harness `_persist_*` helpers (`harness.py:428-510`) assume PARENT turn context
  (`self.session.model`, parent `turn_index`) and run in the parent consumer loop — children
  run inside the `task` tool handler and can't reuse them. Add a child-specific persist path
  that takes EXPLICIT attribution (`parent_tool_use_id`, `agent_id`, `index`) AND the child's
  own model (for any model-derived fields), rather than reading `self.session.model`. It
  writes attributed `MessageEntry`s to the same `{session_id}.jsonl`.
Edge cases:
- Single-agent (no subagents): fields null — behaviour unchanged.
- Two children writing “simultaneously”: serialized by the single event loop; lines are
  well-formed and each attributable.
Tasks:
- Add the fields; add the child persist path taking explicit attribution + child model.
- Tests: round-trip `MessageEntry` with/without new fields; two children persisting produce
  well-formed, attributed lines (grep by `agent_id`).
Deliverable: subagent messages persist in the shared transcript, attributed.
Verify: `pytest tests/test_session_log*.py` — schema round-trip + child-attribution test.

### 11. Targeted interrupt (stop-specific + stop-all)
Approach:
- Extend `InterruptCommand` (in `shared/.../commands.py`) with optional
  `target: tuple[str, int] | None` = `(parent_tool_use_id, subagent_index)`. Absent =
  stop-all (unchanged). Update `serialize_command`/`deserialize_command`.
- Agent side: the interrupt handler consults the M6 child-event registry
  (`(parent_tool_use_id, index)` → `(interrupt_event, cancel_fn)`). Untargeted → set the
  parent interrupt + every child's `interrupt_event` AND call every `cancel_fn`. Targeted →
  set only the matching child's `interrupt_event` AND call its `cancel_fn` (kills its `exec`
  subprocess), mirroring `harness.interrupt()` (`harness.py:351-354`). Setting only the event
  is insufficient — `run_loop` reads `interrupt.is_set()` (`loop.py:182`) but never kills a
  running subprocess, so a child mid-`exec` would not stop without `cancel_fn`.
- ⚠️ Invariant (m-3): the registry lookup + `.set()`/`cancel_fn()` MUST run with NO
  intervening `await` (no other coroutine mutates the registry between lookup and action). A
  missing key (child already finished/deregistered) is a no-op logged at debug.
- A stopped-specific child returns an interrupted result; siblings and the parent turn
  continue; the batch still returns.
Edge cases:
- Target no longer running (already finished): no-op, log at debug.
- Untargeted during a `task`: stop-all, session returns to idle.
Tasks:
- Extend the command + (de)serialization; implement targeted vs all in the handler.
- Tests: targeted interrupt stops only that child (fake llm/long-running child) — asserts
  both its `interrupt_event` set AND its `cancel_fn` called; untargeted stops all; stale
  target is a no-op.
Deliverable: the user can stop one subagent or all of them.
Verify: `pytest` — assert only the targeted child's event is set; untargeted sets all.

### 12. TUI: full-screen subagent detail view + picker + stop-specific
Approach:
- Add the FIRST `ModalScreen` in the codebase: `SubagentScreen` showing one child's LIVE
  output — streaming assistant text + its tool calls/results — driven by the
  `SubagentTextDelta`/`SubagentToolCall`/`SubagentToolResult` events (M8) filtered to the
  selected `(parent_tool_use_id, index)`.
- Buffer per-child streaming in `ArchieApp` keyed by `(parent_tool_use_id, index)` (the app
  currently has only flat single-agent state, `app.py:87-91`) so the modal renders
  history-so-far-in-this-session on open and live thereafter. ⚠️ This buffer is LIVE-ONLY /
  best-effort (M-3): child deltas are NOT in `/history`, and all child events share the
  parent `turn_index` (dedup `turn_index > last_turn_index`, `app.py:147`). So the modal can
  fully reconstruct only for the CURRENTLY ACTIVE turn; after reconnect or once the parent
  turn is in history, an opened modal may show partial/empty history. Document this; do not
  attempt full reconstruction.
- Picker to choose a subagent: reuse the command-palette `Provider` pattern
  (`models_provider.py:30-78`) — a `SubagentProvider` listing in-flight/known children for the
  active `task` (there is NO list/table widget in the codebase, so commit to the palette
  Provider, not an in-modal list). Bind a key (e.g. `ctrl+s`) to open the picker.
- Stop-specific: from the modal (or picker), a key sends a targeted `InterruptCommand`
  (M11) for the selected child via `ws.send_command(...)` (`ws_client.py:73-77`). A separate
  binding sends untargeted stop-all (reuses existing `send_interrupt`).
Edge cases:
- Child finishes while modal open: view shows final state; picker drops it.
- Modal open across turn boundaries / after reconnect: keyed buffers are live-only, so the
  view may be partial for non-active turns — acceptable (see M-3 note above).
Tasks:
- Add `SubagentScreen(ModalScreen)` + push/pop wiring; per-child stream buffering + dispatch
  in `_handle_event`; picker; targeted-stop + stop-all key bindings.
- Widget unit tests for buffer routing; visual confirmation of live view + stop.
Deliverable: user can open a full-screen live view of a chosen subagent and stop it (or all).
Verify: run `archie`, start a `task` with two subagents, open one full-screen, watch it
stream, stop just that one; confirm the sibling continues. (Final confirmation visual.)

---

## Notes for the implementor
- Ship agent + cli together (protocol bump). Run the full `pytest` suite after each
  milestone; the loop/harness are well covered — keep them green.
- Do not touch `run_loop`'s sequential tool execution; all concurrency lives in the `task`
  handler.
- Leave `CONTRIBUTING.md`/`README.md` untracked; stage only files relevant to each commit.

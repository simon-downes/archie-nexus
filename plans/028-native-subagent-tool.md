# 028 — Native Subagent (`task`) Tool

## Objective

Add a native `task` tool so the primary agent can delegate one or more well-scoped tasks to role-specialised child agents. A child is a fresh `run_loop()` execution inside the same session container, with its own model client, prompt, skill scope, tool-dispatch state, interrupt state, canonical event scope, and final result. The parent receives the child output as a normal tool result.

The feature must support bounded concurrent delegation, independent provider/model selection, canonical scoped persistence and accounting, live nested TUI progress, full-screen child inspection, and targeted or stop-all cancellation. V1 children are autonomous, fresh-context workers and cannot spawn further children.

## Context

This plan was originally written before the canonical session-event work and before generic parallel tool dispatch. It is now revised against the completed and implemented changes in plans 029, 030, and 031.

### Dependency status

- **029 — Canonical Session Events and Usage Ledger:** complete. It replaced the old message-oriented session log and hand-written event stream with canonical `msgspec` events, append-before-broadcast persistence, canonical replay, request-backed costs, and scope-based subagent attribution.
- **030 — GPT-5.6 Luna Prompt Caching and Billable Usage:** complete and archived at `plans/done/030-spec-gpt56-luna-prompt-caching.md`. It introduced structured prompts, `history_boundary`, billable token categories, cache-aware pricing, and model-specific request accounting.
- **031 — Parallel Tool Calls:** implemented. `run_loop()` now dispatches tool calls from one provider response concurrently through an event-driven interrupt bridge. This plan must not duplicate that generic primitive.

### Current architecture

Nexus uses one container per session: `archie start` creates one container, one `AgentHarness`, one root agent loop, and one WebSocket-connected session. Children therefore run as async tasks inside the parent container and share the mounted workspace, checkout, credentials, event loop, and session log.

The root execution path is:

```text
WebSocket MessageCommand
  → AgentHarness.handle_message()
  → run_loop()
  → ToolRegistry / ToolSpec handler
  → canonical event factory + append/broadcast
```

The relevant current files are:

- `agent/src/archie_agent/loop.py` — pure tool loop; `run_loop()` and generic parallel batch dispatch
- `agent/src/archie_agent/harness.py` — root session state, event consumption, tool dispatch, process cancellation, broadcast
- `agent/src/archie_agent/app.py` — model/config loading, harness construction, WebSocket command routing
- `agent/src/archie_agent/skills.py` — discovery and closure-based `skill` tool factory
- `agent/src/archie_agent/tools.py` — `ToolSpec`, `ToolRegistry`, and provider-neutral tool configuration
- `agent/src/archie_agent/prompt.py` — structured `SystemPrompt` and prompt fragment builders
- `agent/src/archie_agent/event_log.py` — canonical `EventFactory` and persistence
- `agent/src/archie_agent/session.py` — in-memory turns and live usage/context state
- `shared/src/archie_shared/canonical_events.py` — canonical persisted/live event structs
- `shared/src/archie_shared/session/log.py` — canonical append/replay and session accounting
- `shared/src/archie_shared/session/accounting.py` — direct/inclusive scope cost aggregation
- `shared/src/archie_shared/events.py` — legacy/live command and connection events; `InterruptCommand` lives here
- `shared/src/archie_shared/schemas.py` — YAML configuration structs
- `cli/src/archie_cli/tui/app.py` — canonical replay/live reducer and TUI state
- `cli/src/archie_cli/tui/conversation.py` — conversation and iteration widgets
- `cli/src/archie_cli/tui/models_provider.py` — command-palette provider pattern

### Canonical scope contract from 029

Canonical events use `scope: str | None` rather than separate parent-agent fields.

- Root events have `scope=None`.
- When a root `task` tool call has `tool_use_id=S`, every event produced by the child has `scope=S`.
- Parentage is reconstructed from the launching tool call:

```text
parent_of(S) = tool_call(tool_use_id=S).scope
```

- A child request is uniquely identified by `(scope, turn_iteration, request_id)`.
- Each child provider request emits exactly one canonical `llm_request` event.
- `scope_direct_costs()` sums requests directly billed to each scope.
- `scope_inclusive_costs()` rolls descendant scope costs through the launching tool-call chain.

Do not reintroduce `parent_tool_use_id`, generic agent IDs, or a parallel flat `Subagent*` event protocol. The launching tool ID is represented by canonical `scope`.

### Prompt and usage contracts from 030

The prompt boundary is structured:

```text
SystemPrompt
  static_system: identity/environment/tools/constant context
  dynamic_system: loaded skills and other dynamic content
```

Child prompts must return the same `SystemPrompt` type and use the same provider interface as root prompts. `run_loop()` forwards `history_boundary` to providers that accept it. Cache metadata remains provider-owned and must not enter `Session.turns`, child task content, or canonical events.

Usage fields are billable categories:

- `input_tokens` — uncached input tokens
- `cache_read_tokens` — cache-read input tokens
- `cache_write_tokens` — cache-write input tokens
- `output_tokens` — output tokens

Total context input is derived as the sum of the three input categories. Child cost must be calculated with the child `ModelEntry.cost`, not the current parent session model. Cost is accounting data in the current agent and is not itself a termination condition.

### Generic parallel dispatch from 031

When one model response contains multiple tool calls, `run_loop()` already:

- starts all handlers concurrently;
- yields live `ToolResult` events as each completes;
- preserves provider-facing result order by input position;
- isolates handler exceptions into error results; and
- uses the event-driven `interrupt_async` bridge and structured cleanup.

The `task` handler has a separate responsibility: it bounds and tracks concurrent child `run_loop()` instances, provides child-specific scope and cancellation state, and combines child results. It must not add another generic batch executor to `loop.py` or modify plan 031's semantics.

### Existing gaps

The implementation has no child-agent discovery, child prompt builder, `task` tool, child model-client construction inside the harness, child event consumer, targeted interrupt protocol, child registry, or nested TUI state. The harness currently owns a single `_active_proc`; concurrent children must not reuse it. The harness constructor currently receives neither the model catalog nor subagent limits because those are loaded in `app.py`.

## Requirements

### 1. Agent definition discovery

- MUST discover definitions from `persona/agents/*.md` at harness construction, following the discovery and warning conventions in `skills.py`.
  - AC: a valid definition is present in the returned catalog keyed by its frontmatter `name`.
  - AC: a missing `persona/agents/` directory returns an empty catalog without raising.
- MUST require non-empty `name` and `description` frontmatter fields.
  - AC: malformed YAML, missing delimiters, non-mapping frontmatter, or missing required fields causes a warning and skips only that file.
- MUST accept optional `provider`, `model`, and `skills` fields.
  - AC: omitted provider/model are represented as no override and omitted skills becomes an empty list.
  - AC: `skills` is validated as a list of names; malformed values cause that definition to be skipped with a warning.
- MUST resolve duplicate names deterministically.
  - AC: duplicate names use sorted scan order with last-wins behavior and emit a warning naming both files.
- MUST retain the Markdown body without YAML frontmatter for child prompt construction.
  - AC: the body excludes both frontmatter delimiters and frontmatter text.

### 2. Root `task` tool contract

- MUST register a provider-neutral `task` `ToolSpec` in the root harness registry.
  - AC: `task` appears in `ToolRegistry.to_tool_config()` with a documented input schema.
  - AC: the root prompt's tool strategy remains compatible with the tool registry and no provider-specific schema is added.
- MUST accept one or more task objects with required `agent` and `prompt` strings.
  - AC: the public schema describes an array of `{agent, prompt}` objects and requires at least one item.
  - AC: an empty list, missing task list, missing agent, non-string agent, or empty prompt returns a tool error without starting a child.
- MUST return one combined, deterministic result labelled by input index and agent name.
  - AC: output order follows input order even when children complete in another order.
  - AC: each item identifies success, error, interruption, or truncation status and includes its result text.
- MUST handle unknown agent names as per-task errors.
  - AC: the error names the requested agent and lists available definitions; sibling tasks still execute.
- MUST use the child final assistant text as the task result content.
  - AC: a child that terminates with text `X` produces a parent tool result containing `X`.
- MUST catch child exceptions and isolate them to the corresponding item.
  - AC: one failing child does not cancel or remove successful sibling results.
- MUST pass the launching root `tool_use_id` to the handler as internal execution context.
  - AC: the public tool input schema does not require the model to provide a scope or tool-use ID, but child canonical events use the actual launching tool ID.

### 3. Fresh child execution

- MUST run each child through the existing `run_loop()` with a fresh message list containing the task prompt as a user turn.
  - AC: parent conversation history is not present in the child message list.
  - AC: the child final response is collected from loop events rather than by calling a second provider API directly.
- MUST provide a child tool registry containing `exec` and a scoped `skill` tool, but not `task`.
  - AC: a child model receives no `task` tool definition.
  - AC: a child can still run the existing exec tool and declared skills.
- MUST prevent children from reaching user-input paths.
  - AC: no child registry includes a tool or callback that prompts the TUI/user.
  - AC: a child blocker is returned as assistant text or an error result.

### 4. Model/provider selection

- MUST construct a separate child LLM client.
  - AC: child execution does not mutate the parent `_llm` client or active session model.
- MUST use an explicit definition provider/model when both are supplied.
  - AC: the resolved `ModelEntry` and client provider match the definition.
- MUST use the current session `ModelEntry` when the definition omits provider/model.
  - AC: a definition without overrides follows a parent model switch for children launched afterward.
- MUST resolve models through the existing catalog and client construction path.
  - AC: invalid provider/model configuration becomes a per-task error naming the unresolved model; it does not crash the parent turn.
- MUST price all child requests from the resolved child `ModelEntry.cost`.
  - AC: child requests using a different provider/model are priced with that model's normal/cache-read/cache-write/output rates.
  - AC: a later parent model switch does not reprice persisted child `llm_request` events.

### 5. Focused structured prompt

- MUST add an explicit `build_subagent_prompt()` function in `agent/src/archie_agent/prompt.py`.
  - AC: the function returns `SystemPrompt`, not a flattened string.
  - AC: root `build_system_prompt_structured()` remains unchanged in shape.
- MUST include the child definition body, environment, tool guidance, scoped skill catalog, session-snapshot project context, and dynamic loaded skill bodies.
  - AC: static and dynamic content are placed in the correct `SystemPrompt` sections.
  - AC: the child receives the `AGENTS.md` snapshot captured when the harness was constructed, not a fresh file read.
- MUST exclude the primary identity/persona and tone sections.
  - AC: the primary identity text is absent from the child static prompt.
- MUST preserve 030 provider behavior.
  - AC: child `run_loop()` passes `history_boundary` to providers whose `stream()` accepts it.
  - AC: cache breakpoints, cache keys, and other provider metadata are not stored in child turns or canonical events.

### 6. Skill scoping

- MUST offer only skills declared by the child definition and present in the discovered catalog.
  - AC: a definition with `skills: [research]` exposes only `research`.
  - AC: an absent or empty list exposes no skills.
- MUST create a fresh loaded-skills list for each child.
  - AC: one child loading a skill does not change another child's prompt or the parent's loaded skills.
- MUST warn and drop unknown skills at spawn time.
  - AC: unknown names are absent from the child catalog and a warning includes the agent and skill name.

### 7. Bounded child concurrency

- MUST bound in-flight child execution with `asyncio.Semaphore(max_concurrent)`.
  - AC: `max_concurrent=1` never has two child provider requests active simultaneously.
  - AC: the semaphore limits child jobs, not individual tool calls inside a child; child-local tool calls retain plan 031 behavior.
- MUST preserve result ordering by task input index.
  - AC: completion order may differ, but the combined result and model-facing tool result list are input ordered.
- MUST configure the limit under `agent.subagents`.
  - AC: absent configuration defaults to `max_concurrent=3`.
  - AC: invalid non-positive values fail configuration validation rather than silently disabling the limit.

### 8. Shared agent termination and protection policy

- MUST run children with the same `run_loop()` termination policy as the root agent.
  - AC: child calls use the existing loop default (`loop._DEFAULT_MAX_ITERATIONS`, currently `100`) by omitting a child-specific override or by passing the same shared agent value.
  - AC: there is no `agent.subagents.max_iterations` setting and no child-specific iteration default.
- MUST apply the same duration and subprocess timeout mechanisms to child work as to root work.
  - AC: child provider calls use the existing provider client timeout behavior, and child `exec`/shell/web calls use their existing tool timeout behavior.
  - AC: the task feature does not add a separate child duration timer or timeout policy.
- MUST treat cost consistently between root and child execution.
  - AC: child usage is recorded and priced per request using the resolved child model, while cost remains accounting data rather than an independent child termination condition.
  - AC: any future agent-wide duration, iteration, or cost guard is implemented at the shared root/child execution boundary and applies to both uniformly.
- MUST preserve normal `run_loop()` terminal event semantics.
  - AC: a child reaching the existing loop iteration cap emits a canonical scoped terminal error event and cannot leave a live child task behind.

### 9. Canonical scoped events

For every child launched from root task tool-use ID `S`:

- MUST use `scope=S` for all child canonical events.
  - AC: child `iteration_start`, `llm_request`, `tool_call`, `tool_result`, `assistant_message`, and terminal turn events decode with `scope=S`.
- MUST create one canonical `llm_request` for every child provider request.
  - AC: request ID, `turn_iteration`, model key, status, duration, billable tokens, and cost are present on exactly one record.
- MUST use a child `EventFactory` initialized with the session log path, resolved child model key, resolved child `ModelEntry`, and `scope=S`.
  - AC: the child model key and cost remain correct if the parent switches models later.
- MUST use the parent turn number and child-local request iteration when constructing `turn_iteration`.
  - AC: root and child may both have `2.1`; their scopes distinguish them.
- MUST emit child `text_delta` as a live-only canonical event with child scope.
  - AC: deltas are broadcast but never appended to the persisted event log.
- MUST append every persisted child event before broadcasting its exact serialized JSON.
  - AC: a client never receives a persisted child event that is absent from the log.
- MUST retain the root `task` `tool_call` and `tool_result` in root scope.
  - AC: the child scope is discoverable from the root tool call's `tool_use_id`.
- MUST NOT add child attribution fields to `MessageEntry`.
  - AC: canonical replay and scope accounting work without `agent_id`, `parent_tool_use_id`, or `subagent_index` fields on legacy message entries.

### 10. Accounting and replay

- MUST use canonical `llm_request` events as the authoritative child accounting source.
  - AC: `session_accounting()` includes child costs in total session cost.
  - AC: direct and inclusive scope helpers return expected child and root totals.
- MUST preserve billable usage categories from 030.
  - AC: cache-read/cache-write tokens are not added to normal input tokens before pricing.
  - AC: context-token reporting uses the derived total context input.
- MUST persist child canonical events to the same session log.
  - AC: `/events` replay returns child events in append order after reconnect.
- MUST tolerate concurrent child producers on the single event loop without torn records.
  - AC: every persisted JSONL line decodes successfully and each event has a unique ULID.

### 11. Live TUI observability

- MUST emit child scoped events to all connected clients without a detail-view subscription.
  - AC: child text/tool activity is available to both the collapsed view and detail view.
- MUST render each active child beneath its launching root `task` call.
  - AC: the collapsed block identifies agent name, running/completed/error state, latest text/tool activity, and direct cumulative cost.
  - AC: two concurrent children have separate state and rolling activity windows.
- MUST use canonical `LLMRequest` events for displayed costs.
  - AC: the TUI does not recalculate child cost from the current parent model.
- MUST provide a full-screen selected-child view.
  - AC: a picker can select an active/known child scope; the modal shows live child text and tool calls/results; closing preserves inline state.
- MUST use canonical replay for persisted child activity.
  - AC: reconnect reconstructs persisted child assistant/tool/ledger state; live-only deltas are not required to be reconstructed.

### 12. Targeted interruption

- MUST extend `InterruptCommand` in `shared/src/archie_shared/events.py` with an optional target `(scope, subagent_index)`.
  - AC: target commands serialize and deserialize without losing either field.
  - AC: no target preserves existing stop-all turn semantics.
- MUST maintain a live child registry keyed by `(scope, index)`.
  - AC: an active entry contains its threading interrupt event, async interrupt bridge, and subprocess cancellation callback.
- MUST stop only the selected child for a targeted command.
  - AC: the target event and cancellation callback are invoked; siblings and the parent task continue.
- MUST stop all children and the parent for an untargeted command.
  - AC: every child event is set, every child subprocess callback is invoked, and no child task remains after the parent returns.
- MUST perform registry lookup and cancellation without an intervening `await`.
  - AC: a targeted action cannot race with deregistration between lookup and signal.
- MUST treat stale targets as no-ops.
  - AC: a finished child target logs at debug level or remains silent and does not affect siblings.

### 13. Configuration and compatibility

- MUST add `SubagentsConfig` under `AgentConfig` without introducing a dependency from `archie_shared` to `archie_agent`.
  - AC: `agent.subagents.max_concurrent` loads from YAML with a shared-owned default of `3`.
  - AC: there is no subagent-specific iteration, duration, or cost limit in the config schema.
  - AC: absent `agent.subagents` is accepted and children inherit the same agent-wide protection behavior as root execution.
- MUST preserve root single-agent behavior.
  - AC: existing root tool dispatch, prompt caching, canonical replay, model switching, and session accounting tests remain green.
- MUST ship shared, agent, and CLI command/event changes in lockstep.
  - AC: targeted commands and scoped events are understood by the current agent and TUI clients.

## Technical Design

### Overview

Implement the feature as a child-execution subsystem behind the root `task` `ToolSpec`. Agent discovery and child execution helpers live in a new `agents.py`; prompt construction stays in `prompt.py`; the harness owns session-wide dependencies, child lifecycle state, cancellation routing, and broadcast; `EventFactory` remains the canonical persistence boundary. The child loop is consumed inside the task handler because the handler must translate each child event into scoped canonical events while the parent tool call is still active.

The root loop remains generic. It sees `task` exactly like any other `ToolSpec`; plan 031 handles concurrency among sibling root tool calls, while the task implementation controls how many child loops may run simultaneously.

### Technical Stack

- Reuse Python `asyncio`, `threading.Event`, `asyncio.Event`, and `asyncio.Semaphore`; no new runtime dependency.
- Reuse `yaml` and the parser conventions in `skills.py` for agent definitions.
- Reuse `ToolSpec` and `ToolRegistry` from `tools.py`.
- Reuse `run_loop()` and the provider-neutral `LLMClient` interface.
- Reuse `create_llm_client()`, `get_model()`, and the loaded model catalog from `app.py`.
- Reuse `SystemPrompt` and structured prompt fragments from 030.
- Reuse `EventFactory`, canonical `msgspec` events, `append_event()`, and scope accounting from 029.
- Reuse existing pytest/pytest-asyncio tests and fake LLM client. No new test framework or serialization library.
- Keep `archie-shared` standalone: it may define the configuration shape and nullable override, but it must not import agent loop constants or any package-specific implementation module.

### Architecture

**Root harness**

Owns the active parent session, root model/client, model catalog, region, subagent limits, root registry, connected clients, parent interrupt state, and live child registry. It creates the `task` factory closure and supplies the broadcast and canonical-log callbacks.

**Agent catalog**

`agents.py` discovers immutable `AgentEntry` values at harness construction. The catalog is session-constant, like the skill catalog. Agent body text is immutable; child loaded-skill state is per invocation.

**Task handler**

`create_task_tool()` validates the public input, resolves the launching tool-use ID from internal execution context, creates one child coroutine per task, applies the semaphore, gathers ordered results, and cleans up registry entries. It does not know provider-specific request formats.

**Child runner**

A child runner resolves a model, creates a scoped prompt and registry, creates isolated cancellation state, runs `run_loop()`, consumes child events, persists/broadcasts canonical events, records live context usage, and returns a normalized result. Each runner has an isolated child dispatch object for tool execution and subprocess cancellation.

**Canonical event path**

The child runner uses `EventFactory(scope=S)`. Persisted events are appended to the same session log before `_broadcast_raw(serialized)`. Live-only text deltas are constructed with the same scope and broadcast without append. The TUI consumes the resulting canonical events; no separate subagent wire representation is introduced.

**TUI**

The TUI maintains child state keyed by `(scope, index)` for live presentation and links it to the root task by `scope`. Persisted canonical events remain keyed by scope and canonical IDs. The collapsed view retains only a rolling activity window; the detail view can retain the full live buffer for currently known children.

### Components

**`agent/src/archie_agent/agents.py` — new**

- `AgentEntry`: name, description, provider override, model override, skills, body, path.
- `discover_agents()`: scan `persona_dir() / "agents" / "*.md"` in deterministic order.
- `_parse_agent_file()`: frontmatter/body extraction and validation.
- `ChildDispatch`: isolated registry, pending state, active process, and cancellation callback.
- `create_task_tool()`: root task `ToolSpec` factory and bounded child execution.
- Child event-consumption helpers only where they are reusable and independent of harness state.

**`agent/src/archie_agent/prompt.py` — modified**

Add `build_subagent_prompt()` returning `SystemPrompt`. It reuses `_build_environment`, `_build_tools`, `_build_skills_catalog`, `_build_loaded_skills`, and project-context formatting, but intentionally omits `_build_identity()`.

**`agent/src/archie_agent/harness.py` — modified**

- Accept and store model catalog, region, and `SubagentsConfig`.
- Discover agents and create the root task registry entry.
- Expose the launching tool-use ID and parent turn context to the task handler without adding those fields to the public schema.
- Own the live child registry and route targeted/untargeted interrupts.
- Supply canonical log/broadcast callbacks.

**`agent/src/archie_agent/app.py` — modified**

Pass `_catalog`, `_config.global_.region`, and `_config.agent.subagents` into `AgentHarness`. Route `InterruptCommand.target` to targeted harness interruption while preserving no-target stop-all behavior.

**`agent/src/archie_agent/event_log.py` — modified minimally**

Reuse `EventFactory` as-is where possible. Add only child-loop construction helpers needed to emit scoped request, iteration, tool, assistant, and terminal events without duplicating cost calculation or serialization.

**`shared/src/archie_shared/schemas.py` — modified**

Add `SubagentsConfig` with defaults and `AgentConfig.subagents` using `msgspec.field(default_factory=...)`.

**`shared/src/archie_shared/events.py` — modified**

Extend `InterruptCommand`; retain canonical persisted event definitions in `canonical_events.py` as the source of child event schema. A new event type is allowed only if an actual lifecycle fact cannot be represented by existing scoped events, and must be explicitly classified persisted/live-only.

**CLI/TUI — modified**

Extend canonical live-event dispatch, child state, rolling activity rendering, picker, modal screen, and targeted interrupt command creation. Follow the command-palette `ModelProvider` pattern for child selection.

### Data Model

**`AgentEntry`** — in-memory, session lifetime

- `name: str`, required and unique within catalog
- `description: str`, required
- `provider: str | None`, optional
- `model: str | None`, optional
- `skills: list[str]`, required in memory, defaults empty
- `body: str`, required, frontmatter-free
- `path: Path`, required source path

**`SubagentsConfig`** — YAML/config lifetime

- `max_concurrent: int = 3`, required after shared-schema defaulting, must be positive
- No `max_iterations` field; children inherit the root `run_loop()` default and any future agent-wide protection policy

**`ChildState`** — one active task invocation, ephemeral

- `scope: str`, the launching root `tool_use_id`
- `index: int`, input task index
- `agent_name: str`
- `interrupt: threading.Event`
- `interrupt_async: asyncio.Event`
- `cancel_process: Callable[[], None]`
- child task reference and completion status

The registry exists only while the child is running and is removed in a `finally` block. Canonical event records are the durable state; no separate child database or message log is created.

**Canonical child events** — session-log lifetime

Existing canonical types carry `scope` and are used without flat subagent fields: `IterationStart`, `TextDelta` live-only, `LLMRequest`, `ToolCall`, `ToolResult`, `AssistantMessage`, `TurnComplete`, `TurnError`, and `TurnInterrupted`.

### Data Flow

**Single child — success**

1. Root provider requests `task`; root loop emits root `ToolCall` with `tool_use_id=S`.
2. Harness dispatch invokes the task handler with internal launch context `S`.
3. Handler resolves the definition/model, creates child state, scoped registry, structured prompt, and `EventFactory(scope=S)`.
4. Child `run_loop()` begins with one fresh user turn containing the task prompt.
5. For each child event, the child consumer broadcasts live text/tool events and persists canonical replayable events before broadcasting them.
6. On child `Usage`/`RequestFinished`, the consumer records live usage and creates the child-model-priced `LLMRequest`.
7. On child terminal completion, the consumer persists scoped assistant/turn events and returns final text.
8. The task handler returns the labelled result to the root loop; root handling persists/broadcasts the root `ToolResult`.

**Multiple children**

1. Handler validates all inputs and creates one coroutine per valid task.
2. Each coroutine waits on the task semaphore.
3. Each child gets independent model, prompt, registry, interrupt, process, event factory, and result accumulator.
4. Child events may arrive in completion order; canonical append order is the actual event-loop append order.
5. `gather()` returns result slots in input order; the handler formats a deterministic combined result.

**Targeted interruption**

1. TUI sends `InterruptCommand(target=(scope, index))`.
2. WebSocket command deserializes the target and calls harness targeted interruption.
3. Harness performs an atomic synchronous registry lookup/action: set child threading event, set child async event on the owning loop, and invoke its process cancellation callback.
4. Child loop repairs its tool history and emits scoped interruption/terminal events.
5. Sibling child coroutines and the root task continue.

**Stop-all interruption**

1. TUI sends no-target `InterruptCommand`.
2. Harness sets root interrupt and async bridge, then signals every live child and cancels every child process.
3. Plan 031's root batch cleanup and each child runner await all pending tasks.
4. Root `run_loop()` emits the existing interruption behavior; the task result contains interrupted child items; the harness returns idle with no live child registry entries.

**Failure paths**

- Unknown definition → labelled task error; no child state created.
- Model resolution/client construction failure → labelled task error; siblings continue.
- Child provider error → scoped canonical request error and labelled task error; siblings continue.
- Canonical append failure → do not broadcast the failed persisted event; child becomes an error and the parent receives a labelled failure. Do not emit a noncanonical substitute.
- Child subprocess failure → child tool returns its normal error content; child may continue if the loop does so; targeted cancellation kills its process group.
- Client disconnect → child execution continues because canonical events are session-owned; later clients recover persisted events through replay.

### Error Handling and Edge Cases

- Missing agent directory → empty catalog; root task reports no available agents.
- Malformed definition → warning and skip only the malformed file.
- Duplicate definition name → deterministic last-wins catalog entry and warning.
- Unknown skill → warning, drop that skill, continue spawning the child.
- Empty task prompt → per-task validation error; do not call the provider.
- Unknown agent → per-task error listing available names; siblings continue.
- Explicit model not in catalog → per-task error; parent turn continues.
- Child raises before first provider request → no partial child result; return labelled error and clean registry.
- Child reaches the existing root loop iteration cap → emit scoped terminal error and return truncation notice.
- Child returns malformed tool result ID → child dispatch normalizes it to the input tool-use ID and reports an error, following plan 031's correlation invariant.
- Two children use equal-looking tool blocks or provider IDs → isolated dispatch state keeps them distinct by child and tool-use ID.
- Two children append at the same event-loop point → synchronous append operations serialize complete JSONL writes; each event receives a unique ULID.
- Target finishes before command arrives → no-op.
- Targeted child interrupted while in `exec` → set loop interrupt and kill only that child's process group.
- Parent stop-all while child is waiting on provider stream → set both interrupt mechanisms and await child cleanup.
- Client reconnects during a child → persisted canonical events replay; already-emitted live-only text deltas are not required to replay.
- Parent model switches after child launch → child event factory retains its resolved model key/cost; no historical repricing.
- Provider supports no `history_boundary` parameter → `run_loop()`'s existing signature inspection omits that argument.

### External Integrations

**Model catalog/provider clients**

- Purpose: construct independent child LLM clients.
- Pattern: synchronous model lookup followed by provider client construction; child requests run through existing threaded provider streaming bridge.
- Constraints: use the catalog and region already loaded by `app.py`; no child-specific credentials or container.
- Failure: invalid model/provider is a per-task error; provider stream errors are scoped child errors; no retry policy is added beyond existing provider behavior.

**Session canonical event log**

- Purpose: durable child event persistence, replay, and accounting.
- Pattern: synchronous append of canonical serialized JSONL on the agent event loop, append before broadcast.
- Constraints: same session path, unique ULIDs, canonical decoding, no legacy `MessageEntry` records.
- Failure: failed append prevents broadcasting that persisted event and terminates the affected child/turn according to 029's canonical error rules.

**WebSocket clients**

- Purpose: live canonical event delivery and interrupt commands.
- Pattern: existing `_broadcast_raw()`/`_broadcast()` fan-out and `InterruptCommand` serialization.
- Constraints: agent and CLI changes ship together; clients may ignore unrecognized future event types but current scoped types must be understood.
- Failure: disconnected clients are removed from broadcast; child work continues and replay recovers persisted state.

### Code Structure

Use the following locations and existing patterns:

- New `agent/src/archie_agent/agents.py`, following `skills.py`'s dataclass/discovery/factory structure.
- New `persona/agents/` directory with a small example definition used by tests and manual verification.
- Extend `agent/src/archie_agent/prompt.py`, following the separate public prompt-builder functions and `SystemPrompt` structure from 030.
- Extend `agent/src/archie_agent/harness.py`, following the existing root `_execute_tool`, `_on_proc_start`, `_cancel_active_proc`, and `_broadcast` boundaries without sharing their mutable process slot.
- Extend `agent/src/archie_agent/app.py` at lifespan harness construction and WebSocket interrupt routing.
- Extend `shared/src/archie_shared/schemas.py` using existing `msgspec.Struct` config conventions.
- Extend `shared/src/archie_shared/events.py` using its existing frozen dataclass command serialization conventions.
- Extend `cli/src/archie_cli/tui/app.py` and `conversation.py`; use `models_provider.py` as the command-palette picker pattern and Textual `ModalScreen` for detail view.
- Add tests under the established `tests/` directory: `test_agents.py`, `test_task_tool.py`, `test_subagent_events.py` or canonical event tests, `test_subagent_interrupt.py`, and focused TUI tests. Extend `test_harness.py`, `test_loop.py`, `test_config*.py`, and canonical accounting/replay tests where the behavior crosses existing seams.

### Patterns and Conventions

- Use `logging.getLogger(__name__)` and warnings matching `skills.py` for malformed definitions and unknown skills.
- Use immutable dataclasses for in-memory definition/state values where mutation is not required; use explicit mutable per-child state only for interrupt/process/result accumulation.
- Use provider-neutral `ToolSpec` schemas; do not put provider-specific payload shapes into the task tool.
- Use canonical `EventFactory` and `append_event()` rather than hand-building JSON or adding a second persistence path.
- Keep public schemas explicit; internal launch context may be carried by a closure or execution-context object, but must not be exposed as a model-required field.
- Use `asyncio.create_task()`/`gather()` with `return_exceptions` or equivalent per-child normalization; ensure all tasks are awaited in `finally`.
- Use `threading.Event` for provider stream cancellation and `asyncio.Event` for async batch wake-up, matching plan 031.
- Keep child subprocess cancellation process-group based, matching the root `_cancel_active_proc()` behavior, but store it per child.
- Unit-test public seams: discovery result, prompt object, task handler result, canonical event log, interrupt command, and TUI reducer state. Do not test private implementation details when an observable seam exists.

### Infrastructure and Deployment

- No new container, service, database, environment variable, secret, or runtime dependency.
- The shared schema does not own agent termination defaults. Children call the same root loop policy; no sentinel or agent import is needed.
- No database migration is needed.
- No session-log migration is implemented; 029 canonical-session startup rules continue to apply.
- Agent/shared/CLI packages ship together when command or canonical event interfaces change.
- Configuration is read from the existing `<ARCHIE_HOME_DIR>/config.yaml` under `agent.subagents`.

### Non-Functional Concerns

- **Isolation:** child subprocesses and pending-tool maps must be independently cancellable and must not overwrite root or sibling state.
- **Reliability:** all child tasks and provider worker threads must be awaited or terminated before task completion; no orphaned asyncio tasks or child subprocesses may remain.
- **Accounting correctness:** every provider request emits one child-model-priced canonical request record; no current-model repricing is allowed.
- **Observability:** warnings identify malformed definitions/skills; canonical scoped events expose child request, tool, terminal, and cost state; targeted cancellation is visible through child terminal events.
- **Performance:** child concurrency is bounded by `max_concurrent`; no additional global semaphore or provider request limit is introduced beyond existing configuration. Child duration and iteration behavior remain the existing root/provider/tool behavior.
- **Protection policy:** the feature introduces no divergent child duration, iteration, or cost guard. Existing controls and any future agent-wide controls apply uniformly to root and child loops.
- **Data handling:** task prompts and child outputs are session data and follow existing session-log/provider handling. Cache metadata remains private to providers and is never persisted.

### Key Decisions

- Canonical scope over flat parent/index wire fields: 029 already defines scope as the stable relationship between a launching tool call and child requests, and it enables nested cost aggregation without another protocol hierarchy.
- Existing canonical event types over new `Subagent*` types: child activity already maps to iteration, text, request, tool, assistant, and terminal events; adding parallel types would force the replay/TUI/metrics stack to maintain duplicate reducers.
- Child `EventFactory` over `Session.record_usage()` as the source of truth: 029 persists immutable request model/cost data, while session totals are only live convenience state. The child uses the same termination policy as the root loop rather than introducing a second limit surface.
- Structured child prompts over flattened strings: 030's provider boundary must remain usable for cache-aware Responses requests and ordinary Converse/Ollama requests.
- Child-local semaphore over a loop-level limit: 031 intentionally leaves tool-specific concurrency to the tool implementation, and a child task is the unit that must be bounded.
- Same-container async children over child containers: the current session architecture provides the required shared workspace and event loop without new orchestration infrastructure.
- No child recursion in v1 despite canonical nested-scope support: it bounds lifecycle complexity while preserving a compatible scope model for a future version.
- Provider/model overrides from the existing catalog over arbitrary dynamic provider construction: this reuses validated model definitions, credentials, region, cache capabilities, and cost configuration.

### Risks and Open Questions

**Risks**

- Concurrent child event volume can increase synchronous session-log append latency. This is acceptable for v1 because 029 deliberately chose synchronous append ordering; buffering requires a separate plan.
- A provider stream can remain blocked despite an interrupt. The existing daemon-thread behavior remains; subprocesses are explicitly killed, and the child cleanup path must not leak asyncio tasks.
- Live-only child deltas are unavailable after reconnect. This is acceptable because persisted canonical assistant/tool/request events are authoritative for replay; the UI must show partial live state rather than inventing missing deltas.
- A client older than the revised CLI may not render child scopes. Agent and CLI are shipped together for this feature; no compatibility negotiation is added.

**Open questions resolved for implementation**

- Scope identifier: launching root `task` tool-use ID.
- Child persistence: canonical scoped events in the existing session log, not `MessageEntry`.
- Cost source: resolved child `ModelEntry.cost` on canonical request creation.
- Fan-out owner: task handler semaphore; generic root tool batching remains in `run_loop()`.
- Child context: fresh task prompt only.
- Child interaction: no user questions and no recursive task tool.

## Milestones

### 1. Agent definition discovery and catalog

Approach:

- Create `agent/src/archie_agent/agents.py` following the immutable `SkillEntry` and parser conventions in `agent/src/archie_agent/skills.py`.
- Scan `persona_dir() / "agents"` for `*.md` files in sorted path order; do not recursively scan.
- Parse YAML using the existing `yaml` dependency. Split on the first two `---` delimiters so the body can contain later horizontal rules.
- Store `AgentEntry` values with frontmatter-free body text and source path.
- Test seam: `discover_agents()` and `_parse_agent_file()` outputs/logging, not filesystem implementation details.
- ⚠️ Discovery occurs at harness construction, so changes to agent files affect new sessions only.

Edge Cases:

- Missing directory → return empty catalog.
- Unreadable file → warning and skip.
- Malformed YAML/frontmatter/body → warning and skip.
- Duplicate name → sorted last-wins entry and warning.
- Empty body → valid definition; the role may rely on the task prompt.

Tasks:

- Add `AgentEntry` and parsing/discovery functions.
- Add `persona/agents/researcher.md` example with valid frontmatter.
- Add tests for valid parse, body extraction, missing directory, malformed files, duplicate names, optional fields, and warnings.

Deliverable: `discover_agents()` returns a validated catalog of usable agent definitions from `persona/agents/*.md`.

Verify: `uv run pytest tests/test_agents.py -q`; inspect a test-created catalog and assert malformed files are absent and warnings are recorded.

### 2. Structured focused child prompt

Approach:

- Add `build_subagent_prompt()` to `agent/src/archie_agent/prompt.py`; follow `build_system_prompt_structured()` and return `SystemPrompt` with static and optional dynamic sections.
- Static content must be assembled from the definition body, `_build_environment()`, `_build_tools()`, filtered `_build_skills_catalog()`, and `_format_project_context(agents_context)`.
- Dynamic content must use `_build_loaded_skills()` for the child's fresh loaded state.
- Do not call `_build_identity()`. Do not add a boolean to the root builder because the root and child prompt shapes are intentionally distinct.
- Test seam: returned `SystemPrompt` section text and a fake provider's received `system`/`history_boundary` arguments.
- ⚠️ The harness's `AGENTS.md` snapshot is passed explicitly; the child builder must not reread the workspace.

Edge Cases:

- Empty catalog → omit the skills catalog or render no available skills according to existing prompt convention.
- Empty loaded skills → `dynamic_system=None` or empty dynamic section, matching `SystemPrompt` semantics.
- Empty agent body → prompt still contains environment/tools/project context.
- Provider lacking `history_boundary` → loop signature inspection omits the argument.

Tasks:

- Implement `build_subagent_prompt()`.
- Add focused prompt tests for body inclusion, identity exclusion, static/dynamic placement, project snapshot use, and scoped skill catalog.
- Add a child fake-provider test proving structured prompt and optional `history_boundary` forwarding.

Deliverable: a child can receive a focused `SystemPrompt` that preserves the 030 provider contract while excluding root identity.

Verify: `uv run pytest tests/test_prompt.py tests/test_prompt_caching.py -q`; assert the fake provider sees `SystemPrompt` and the expected history-boundary behavior.

### 3. Configuration, harness dependency threading, and child dispatch prefactor

Approach:

- This is an explicit **prefactor**: it establishes isolated child execution infrastructure before the user-visible task slice.
- Add `SubagentsConfig` to `shared/src/archie_shared/schemas.py` with only `max_concurrent: int = 3`. `archie_shared` remains standalone and MUST NOT import `archie_agent`, `archie_cli`, or `archie_orchestrator`.
- Do not add a child-specific `max_iterations`, duration, or cost override. Child runners call `run_loop()` through the same agent boundary and inherit `_DEFAULT_MAX_ITERATIONS` and the existing provider/tool timeout behavior automatically.
- Extend `AgentHarness.__init__` with model catalog, region, and subagent configuration. Preserve test compatibility with explicit defaults only where existing direct harness tests require them; update all production/test call sites.
- Create a `ChildDispatch` abstraction in `agents.py` or a narrowly scoped module. It owns a child `ToolRegistry`, independent pending tool state, independent `_active_proc`, `on_start` callback, and process-group cancellation methods.
- Reuse root exec configuration (`_exec_python`, `_exec_run_root`) but never root `_active_proc` or root pending state.
- Test seam: child dispatch public coroutine and cancellation callback.
- ⚠️ A child dispatch must isolate equal `tool_use_id` values across children; keying only on the global ID is insufficient.

Wiring:

- State: harness stores `model_catalog: dict[str, ModelEntry]`, `region: str`, and `subagents: SubagentsConfig`; each `ChildDispatch` stores its own process and pending state.
- Producers: `app.py.lifespan()` loads catalog/config and passes them into `AgentHarness`; child runner creates one dispatch per child.
- Consumers: task factory resolves model/limits from harness; child loop sends tool blocks to its dispatch.
- Call site: `AgentHarness(..., model_catalog=_catalog, region=_config.global_.region, subagents=_config.agent.subagents)`.

Edge Cases:

- Missing config section → defaults.
- Invalid non-positive limit → configuration validation error.
- Existing test harness construction without new optional values → compatible defaults or updated fixture values, never module-global lookup.
- Child process cancellation with no active process → no-op.
- Child dispatch handler exception → normalized `ToolResultBlock` with the input tool-use ID.

Tasks:

- Add config structs and schema tests for `max_concurrent` and defaults.
- Thread catalog/region/config through `app.py` and harness construction.
- Verify child runners inherit the existing root loop default and do not accept a subagent-specific iteration/duration/cost override.
- Implement isolated child dispatch and process cancellation.
- Update all harness fixtures/call sites.
- Test generic child tool, child exec, same tool-use ID isolation, and process cancellation without changing root process state.

Deliverable: the harness can construct an independently cancellable child tool-dispatch context with configured model and concurrency dependencies.

Verify: `uv run pytest tests/test_config*.py tests/test_harness.py tests/test_child_dispatch.py -q`; instrument two dispatches with equal tool IDs and confirm independent results/process slots.

### 4. Single child task execution and scoped canonical events

Approach:

- Implement `create_task_tool()` in `agents.py` for one task before adding fan-out.
- Resolve the definition, model, client, scoped skill catalog, fresh loaded-skill list, child prompt, child interrupt state, child dispatch, and `EventFactory(scope=launching_tool_use_id)`.
- Run `run_loop(messages=[Turn(role="user", content=prompt)], system=child_prompt, llm=child_llm, interrupt=child_interrupt, interrupt_async=child_interrupt_async, tool_config=child_config, execute_tool=child_dispatch)` without a subagent-specific `max_iterations`; this uses the same default as root execution. If a future agent-wide policy is added, it must enter through this shared execution boundary for both root and child loops.
- Consume child events in the task handler because that is where child scope, index, parent turn, event factory, and broadcast callback are available.
- Follow root harness ordering: `IterationStart`; request finish/usage; child text delta; child tool calls/results; terminal assistant/turn event. Persist canonical replayable events before broadcast. Child `TextDelta` is live-only.
- Create `llm_request` with the child `ModelEntry`, including 030 billable usage categories and cache-aware cost.
- Test seam: `create_task_tool()` handler with a fake provider and temporary canonical session log.
- ⚠️ Do not emit old flat wire events or append `MessageEntry` child records.

Wiring:

- State: handler captures agent catalog, skill catalog, session, model catalog, region, exec config, broadcast callback, parent turn, launch scope, and max iterations.
- Producers: child loop produces semantic events; child consumer creates canonical events and appends/broadcasts them.
- Consumers: canonical session log, live WebSocket clients, parent tool result, session live usage state.
- Call site: root dispatch supplies internal context such as `execute_task(..., launch_scope=block.tool_use_id, parent_turn=turn_index)` while the model-facing schema remains only `{tasks}`.

Edge Cases:

- Unknown agent → error result with available names.
- Model resolution failure → error result and no child provider call.
- Provider failure → scoped error request/terminal events when appendable, labelled task error.
- Child reaches cap → truncation result and scoped terminal error.
- Canonical append failure → no broadcast for failed event; terminate affected child.
- Empty final text → return a valid labelled empty/truncation result rather than hanging.

Tasks:

- Implement model resolution and child client construction.
- Implement single child runner and event consumer.
- Create scoped `EventFactory` and emit child canonical events.
- Register root `task` and pass launch context from root dispatch.
- Add fake provider tests for final text, model override/fallback, scope, request identity, canonical log contents, append-before-broadcast, child pricing, and depth guard.

Deliverable: the root model can call `task` once and receive a final child result while the child request and terminal activity are represented by scoped canonical events.

Verify: `uv run pytest tests/test_task_tool.py tests/test_canonical_session_events.py tests/test_subagent_accounting.py -q`; inspect the temporary JSONL log and broadcast capture to confirm identical persisted/broadcast canonical records and `scope=S`.

### 5. Skills scoping and provider/model regression coverage

Approach:

- Complete child registry construction with only `exec` and the scoped `skill` tool.
- Filter the session-discovered skill catalog by the definition's declared names; do not rediscover skills per child.
- Keep loaded skills mutable only within that child invocation.
- Verify child prompt construction and provider adapters using existing fake/provider test seams. Converse and Ollama continue to flatten structured prompts as before; Responses continues to own cache metadata.
- Test seam: child tool configuration and provider fake's received prompt/usage arguments.

Wiring:

- State: root skill catalog is immutable session state; each child has `child_skill_catalog` and `loaded_skills=[]`.
- Producers: child `skill` handler appends only to its list.
- Consumers: child prompt builder reads that list on subsequent child requests.
- Call site: `create_skill_tool(child_skill_catalog, child_loaded_skills)`.

Edge Cases:

- Empty skills → no usable skill names and no loaded-skill mutation.
- Unknown declared skill → warning/drop, child remains usable.
- Child loads same skill twice → existing skill-tool no-op behavior.
- Child skill body changes on disk during execution → current parsed entry/body behavior remains session/child scoped; no live rediscovery.

Tasks:

- Add scoped skill catalog filtering and warning behavior.
- Ensure child registry omits `task`.
- Add tests for declared/unknown/empty skills, fresh loaded state across siblings, and recursion absence.
- Add provider request-shape regression tests for structured child prompts and 030 usage categories.

Deliverable: child agents can use exactly their declared known skills without recursion or cross-child loaded-state leakage.

Verify: `uv run pytest tests/test_task_tool.py tests/test_skills.py tests/test_prompt.py tests/test_bedrock.py tests/test_ollama.py tests/test_bedrock_openai.py -q`; inspect child tool config and fake-provider prompt state.

### 6. Bounded multi-child fan-out

Approach:

- Extend the single-child handler to accept N tasks using `asyncio.gather()` under one per-`task` `asyncio.Semaphore(max_concurrent)`.
- Plan 031's `run_loop()` remains responsible for parallel tools inside each child. This milestone only schedules child runner coroutines and limits child count.
- Keep one indexed result slot per input task. Child completion events may arrive in any order; combined parent result must remain input ordered.
- Catch per-child exceptions and normalize them into labelled results. Do not use `list.index(block)`; child index is assigned at creation and remains stable.
- Test seam: task handler's returned combined string plus an instrumented fake provider tracking active child count.
- ⚠️ The semaphore must cover the full child execution lifetime, including provider requests and child tool loops, not just client construction.

Wiring:

- State: per-task `asyncio.Semaphore`, result list, child registry, and child index.
- Producers: task handler creates runner coroutines; runners register/deregister child state.
- Consumers: `gather()` collects result slots; root loop receives one combined `ToolResultBlock`.
- Call site: root `task` handler receives `tasks=[...]`, `launch_scope=S`, `parent_turn=T`, and configured `max_concurrent`.

Edge Cases:

- `max_concurrent=1` → strictly one child at a time, input order preserved.
- One child fails → siblings continue and result slot is an error.
- One child returns early → semaphore releases and next queued child starts.
- Empty task list → validation error without semaphore work.
- Duplicate agent names or prompts → independent indexed children.
- Child result arrives after sibling → result formatting still follows input order.

Tasks:

- Add semaphore and indexed gather logic.
- Register/deregister child state around the complete runner lifetime.
- Combine results deterministically with agent/index labels.
- Add instrumented fake-provider tests for overlap, bound, order, failure isolation, and cleanup.

Deliverable: one `task` call runs multiple children with bounded concurrency and deterministic combined results.

Verify: `uv run pytest tests/test_task_tool.py -q`; assert observed maximum active children equals the configured limit, both results are present in input order, and the registry is empty after completion.

### 7. Targeted and stop-all cancellation

Approach:

- Extend `InterruptCommand` in `shared/src/archie_shared/events.py` with optional target data. Keep no-target JSON shape compatible with existing clients.
- Add harness methods for targeted child interruption and stop-all propagation. The live registry is keyed by `(scope, index)` and stores both loop signals and process cancellation.
- Follow plan 031's event-driven model: set child threading event for provider worker cancellation and schedule child async event on its owning loop. Do not poll from the event loop.
- Targeted cancellation must not set the parent interrupt or sibling events. Stop-all must set parent and all children and invoke all process callbacks.
- Test seam: command round-trip and harness interruption behavior with long-running fake children and cancellable exec.
- ⚠️ Setting an interrupt event alone does not stop a child waiting on a subprocess; process-group cancellation is mandatory.

Wiring:

- State: `live_children: dict[tuple[str,int], ChildState]` owned by harness/task handler.
- Producers: TUI sends target command; app routes it; harness mutates registry synchronously.
- Consumers: child run loops, child dispatch process cancellation, root batch interrupt bridge.
- Call site: `AgentHarness.interrupt(target: tuple[str, int] | None = None)`; `app.py` passes `command.target`.

Edge Cases:

- Target already finished → no-op.
- Targeted child in provider stream → set both interrupt mechanisms; child returns interrupted result.
- Targeted child in exec → set event and kill only its process group.
- Stop-all during child fan-out → signal every child and await all before root idle.
- Child deregisters concurrently with target lookup → synchronous lookup/action prevents an await race.
- Sibling failure during targeted stop → sibling result is preserved.

Tasks:

- Extend command type, serializer/deserializer, union, exports, and `WSClient` helpers.
- Implement child registry signal/cancel operations.
- Route target through `app.py` and harness.
- Add targeted/untargeted tests with fake long-running clients and process callbacks.
- Add cleanup assertions for no pending child tasks and no active child process slots.

Deliverable: users can stop one selected child or all active children without leaving orphaned work.

Verify: `uv run pytest tests/test_subagent_interrupt.py tests/test_ws_integration.py -q`; assert only the targeted registry entry is signalled in targeted tests and all entries/process callbacks are signalled for stop-all.

### 8. Canonical replay and accounting hardening

Approach:

- Validate the complete child event lifecycle against 029's canonical structs and append-before-broadcast rule.
- Use `EventFactory` rather than direct JSON construction. Ensure request IDs, event IDs, scope, turn iteration, model key, status, usage, and cost are generated at the child boundary.
- Exercise `scope_direct_costs()` and `scope_inclusive_costs()` with root, child, and nested fixture events. V1 does not spawn nested children, but the event/accounting contract must remain compatible with nested scopes.
- Verify 030 billable pricing with child models that have distinct normal/cache-read/cache-write rates.
- Test seam: persisted JSONL replay and accounting helpers, not in-memory session totals.
- ⚠️ Canonical `TextDelta` is live-only; replay tests must use persisted `AssistantMessage`, tools, requests, and terminal events.

Wiring:

- State: one session canonical JSONL path; child event factory holds immutable child model/scope.
- Producers: child consumer appends canonical events before broadcasting.
- Consumers: `/events`, `session_accounting()`, TUI canonical reducer, orchestrator metrics.
- Call site: `EventFactory(path, child_model_key, child_model, scope=S)` and `scope_*_costs(decoded_events)`.

Edge Cases:

- Child model differs from parent → child `LLMRequest.model_key` and price remain child-specific.
- Parent switches model after child completion → existing events unchanged.
- Duplicate event ID → canonical append rejects conflicting data.
- Append failure → failed event is not broadcast and affected child reports an error.
- Concurrent child appends → each complete line decodes and all IDs are unique.
- Reconnect after live deltas → persisted child state replays without requiring deltas.

Tasks:

- Add/extend canonical child event fixtures and round-trip tests.
- Add append-before-broadcast capture test.
- Add child direct/inclusive accounting tests, including model-specific cache pricing.
- Add replay test through `/events` after a completed child task.
- Add orchestrator metrics regression coverage for scoped child request events where the existing API exposes them.

Deliverable: child execution is durable, replayable, and accurately accounted through the canonical event stream.

Verify: `uv run pytest tests/test_subagent_accounting.py tests/test_session_events.py tests/test_canonical_session_events.py tests/test_orchestrator_metrics.py -q`; decode every persisted line and compare persisted/broadcast serialized records.

### 9. TUI collapsed scoped activity

Approach:

- Extend `cli/src/archie_cli/tui/app.py` canonical live dispatch and state reduction for scoped child events.
- Associate child scope with the root task tool call by its `tool_use_id`; child index identifies siblings under one task call.
- Add a per-child rolling buffer of approximately three latest activity lines. Derive summaries from canonical raw tool input/result and text events using shared `tool_summaries.py`; do not require agent-generated summary fields.
- Display agent name from `Subagent` start metadata only if needed. Prefer deriving known agent/index from the root task input and child scope. If the existing canonical event set cannot carry an agent name in live state, add the smallest justified live-only canonical event and document why it cannot be inferred.
- Test seam: TUI reducer/widget state after a sequence of canonical scoped events.
- ⚠️ Live scoped events share the parent turn but must not be dropped by root-turn buffering/dedup logic while the turn is active.

Wiring:

- State: `child_views: dict[(scope,index), ChildViewState]` in `ArchieApp` or the conversation model; each state owns status, rolling lines, cost, and final summary.
- Producers: `_handle_event()` consumes scoped canonical events.
- Consumers: nested conversation widget renders child blocks under the parent task tool entry; status/cost reads canonical `LLMRequest`.
- Call site: canonical event reducer receives the same event objects used by replay/live handling; no second wire reducer.

Edge Cases:

- Two children under one task → separate blocks keyed by index.
- Child error/interruption → block resolves to error/interrupted state.
- Child finishes while live → final state remains visible until parent tool result resolves.
- Reconnect during active task → persisted state renders; missing deltas produce partial text, not fabricated text.
- Scope with no known parent tool call → render as an unattached scoped activity or log/drop according to existing reducer policy; never attach to the wrong root.

Tasks:

- Add scoped child state and event routing.
- Add rolling activity buffer and nested rendering.
- Fold canonical request costs into child direct cost display.
- Add reducer/widget tests for routing, rolling-window size, completion, error, and replay.
- Manually verify two-child live nesting.

Deliverable: the active conversation view shows independent collapsed live activity blocks for all running children.

Verify: `uv run pytest tests/test_tui_canonical.py tests/test_tui_subagents.py -q`; run the CLI against a two-child fake session and observe each block updating and resolving beneath its root task.

### 10. Full-screen child detail, picker, and targeted stop UI

Approach:

- Add the first `ModalScreen` for child detail, following Textual's existing app/widget conventions and keeping modal state separate from conversation state.
- Maintain live per-child detail buffers keyed by `(scope,index)`. The modal consumes the same scoped canonical events as the collapsed view.
- Add a `SubagentProvider` using the command-palette `ModelProvider` pattern to list active/known children. Bind a documented key to open it; do not invent a separate table/list infrastructure.
- From the picker/modal, send `InterruptCommand(target=(scope,index))` through `WSClient`. Preserve the existing no-target interrupt action for stop-all.
- Test seam: child buffer routing and command creation; final visual behavior is manual because Textual layout is not fully captured by unit tests.
- ⚠️ A modal opened after reconnect may contain only persisted canonical state and no old live-only deltas; this is acceptable and must be documented in the UI behavior.

Wiring:

- State: app owns selected child key and per-child detail buffers; modal reads a selected immutable snapshot plus live updates.
- Producers: canonical event handler updates buffers; picker selects keys; key binding sends command.
- Consumers: modal renders text/tool activity; WS client sends targeted command; inline view remains active underneath.
- Call site: `self.push_screen(SubagentScreen(child_key=...))`; `ws.send_command(InterruptCommand(target=child_key))`.

Edge Cases:

- Child finishes while modal is open → show final state and retain output.
- Selected child disappears from active registry → picker removes it, modal can still show final state.
- Modal closes → inline buffers/state remain intact.
- Targeted stop command fails to send → show existing client error path; do not mutate local state as if cancellation succeeded.
- Reconnect while modal open → preserve selection where possible and render replayed state.

Tasks:

- Implement child detail `ModalScreen`.
- Add per-child full/live buffer routing.
- Implement picker and command binding.
- Add targeted-stop and stop-all actions.
- Add widget tests for routing/selection and manual visual validation.

Deliverable: a user can select one active child, watch its live detail view, close it, and stop that child without stopping siblings.

Verify: run `archie`, invoke a task with two long-running children, open the picker, select one, observe text/tool activity, target-stop it, and confirm the sibling continues and inline activity remains.

### 11. End-to-end validation and documentation

Approach:

- Run the full suite after all agent/shared/CLI changes.
- Run changed-file lint/format and inspect canonical event protocol diffs.
- Verify root regressions from plan 029/030/031: canonical replay, prompt caching, billable usage, generic parallel tool calls, root interruption, model switch, and metrics.
- Document the v1 constraints and no-recursion/no-user-question behavior in the plan and any relevant feature documentation. Do not modify unrelated README/CONTRIBUTING files unless implementation requires it.
- Test seam: complete fake-provider integration path plus one manual live TUI path.

Edge Cases:

- No agent definitions installed → root task returns available-agent error without crash.
- Provider credentials unavailable → child error is labelled and parent remains usable.
- Client disconnects → child completes/persists; reconnect replays canonical state.
- Stop-all during multi-child execution → no pending child tasks/processes after turn returns.
- Existing root-only session → all existing behavior and canonical logs remain unchanged.

Tasks:

- Add/update end-to-end tests covering discovery → task → child provider → scoped log → replay/TUI reducer.
- Run full pytest suite.
- Run Ruff and format checks.
- Run `git diff --check`.
- Update any implementation progress marker only after all milestones are complete.

Deliverable: the native subagent feature is validated end-to-end without regressions to canonical events, prompt caching, billable usage, or generic parallel tool execution.

Verify:

```bash
cd /workspace/archie-nexus
uv run pytest
uv run ruff check .
uv run ruff format --check .
git diff --check
```

Then perform a manual two-child run: confirm separate models/skills where configured, nested live activity, canonical replay after reconnect, targeted stop of one child, and stop-all cleanup.

## Acceptance summary

The plan is complete when:

1. A root model can call `task` with one or more definitions from `persona/agents/`.
2. Each child runs fresh, autonomously, and with the configured focused `SystemPrompt`.
3. Explicit child model/provider overrides work without mutating the parent client.
4. Child skills are scoped and child registries cannot recurse into `task`.
5. Child execution is bounded by `agent.subagents.max_concurrent` and inherits the root agent's iteration, duration, subprocess-timeout, and cost-accounting behavior.
6. Results are deterministic and input ordered while live child events may arrive in completion order.
7. Every child provider request creates one child-model-priced canonical `llm_request` with correct billable usage categories.
8. Child tool, assistant, terminal, and live text events use canonical `scope` and replay/account correctly.
9. The TUI renders collapsed scoped activity and a full-screen selected-child view.
10. Targeted interruption stops only the selected child and subprocess; no-target interruption stops parent and all children.
11. No child task, provider worker, or subprocess remains orphaned after completion/interruption.
12. Existing 029 canonical replay/accounting, 030 prompt-cache/usage, and 031 generic parallel-tool behavior remain green.

## Implementation notes

- Never add `parent_tool_use_id`, `agent_id`, or `subagent_index` to `MessageEntry`.
- Never create a second flat subagent event protocol merely for UI convenience.
- Use `scope` for canonical relationship and `(scope,index)` only for ephemeral sibling UI/cancellation addressing.
- Keep root task tool call/result in root scope; only child-produced events use child scope.
- Keep provider cache metadata private to provider adapters.
- Keep generic multi-tool execution in `run_loop()` under plan 031; task-specific fan-out belongs in `agents.py`.
- Keep canonical append-before-broadcast ordering and child-model request pricing from 029/030.
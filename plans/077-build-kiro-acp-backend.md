# 077 — Kiro ACP backend

Status: planning
Branch: build-it/077-kiro-acp-backend (base: main @ 4ee641d)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/077-kiro-acp-backend

## Objective

Let a session run on Kiro CLI instead of Archie's own agent loop. `archie start --kiro`
starts a container whose agent drives `kiro-cli acp --agent-engine=v3` over ACP, gives Kiro
Archie's tools through an in-process MCP server, enables Kiro skills (enabled plugins only)
and one `archie-worker` subagent, and writes the same canonical session log the TUI,
viewer and metrics already read. Usage is recorded as Kiro credits with a USD cost derived
from the subscription rates.

## Context

The user asked to build the Kiro backend researched and spiked in
`~/.nexus/brain/projects/archie/kiro-acp-backend-research.md` (kiro-cli 2.34.2, KAS 0.66.62,
spike artefacts in `/tmp/kiro-spike/`). User decisions recorded there:

- latest stable Kiro CLI, ACP V3 engine; auth from `credentials.yaml` `kiro.api_key` passed as
  `KIRO_API_KEY` to the Kiro process only;
- Kiro's layered base prompt and its forced tool search (`tool_load` / `tool_call`) are fine;
- a `kiro` config section: model allowlist `claude-opus-5.5`, `claude-sonnet-5.5`,
  `claude-haiku-5.5`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`; default
  `gpt-5.6-luna`; optional `effort` (unset = model default); credits: 2,000 included
  (Pro+, $40/month), resets on the 1st, $0.02 per credit, $0.04 overage;
- `credits` is a new `llm_request` field and `cost_usd` is derived from it;
- one generated `archie-worker` subagent; skills from enabled plugins only;
- `contentCollection` must be `disabled` and verified on every new session;
- `autopilot` on; local sessions only; no Kiro session resume (`~/.kiro` stays ephemeral);
- ACP is a native Kiro interface, so the terms-of-use concern is resolved.

Spike facts this plan relies on (verified 2026-10-27 unless marked):

- Launch `kiro-cli acp --agent-engine=v3`; newline-delimited JSON-RPC on stdio. Closing stdin
  does not stop it within 5 s; the process group must be killed. stderr has a Node SQLite
  `ExperimentalWarning` line at `[ERROR]` on every start.
- `session/new {cwd, mcpServers, _meta.kiro.modeId}` selects a custom agent from
  `~/.kiro/agents/<name>.json`. The prompt must be a relative `file://` URI to a file outside
  `~/.kiro/agents/` (every `.md` there is parsed as an agent).
- Invalid agent config silently falls back to the default Kiro agent with every built-in; the
  only signal is a `_kiro/customAgent/config_error` notification.
- With `tools: ["@archie", "subagent", "disclose_context"]` and the permission rules
  `mcp archie/*`, `skill *`, `subagent archie-worker`, no permission prompts occur.
  `_kiro/tools/didChange` then listed builtin tag `read` plus the `@archie/*` MCP tags.
- `session/new` returns `configOptions`: `mode`, `model` (options carry
  `_meta.kiro.rateMultiplier`, `effortLevels`, `defaultEffortLevel`), `memoryReflection`,
  `autopilot` (on), `contentCollection` (enabled). Documented: `session/set_config_option
  {configId, value}` changes them, `configId: "effortLevel"` sets effort.
- Updates: `agent_message_chunk` (text), `tool_call` / `tool_call_update` (`toolCallId`,
  `status` pending/in_progress/completed/failed, `title`, `rawInput`, `rawOutput`, `content`),
  `session_info_update` with `_meta.kiro.kind` (`turn_start`, `context_usage`,
  `turn_completion` with `promptTurnSummaries[{unit:"credit", usage}]`, `elapsedTime`, `status`
  success|aborted, `requestIds`; `turn_end`). The turn settles from the `session/prompt`
  response `stopReason` (`end_turn`, `cancelled`, …).
- MCP tools arrive as `title "@archie/<name>"`, `rawInput {tool_id: "archie::<name>",
  arguments}`. `tool_load` arrives only as a completed `tool_call_update` (no `tool_call`).
  Skill loads: `title "Load skill: <name>"`, `_meta.kiro.toolId "disclose_context"`,
  `rawInput {name}`. Subagents: `toolCallId invoke_subagent_…`, `_meta.kiro.kind
  "agent-subtask"`, `_meta.kiro.agentSubtaskId`, `rawInput {name, prompt, explanation}`;
  child updates carry the same `agentSubtaskId`; the child ends with a "Subagent Response"
  tool call and the dispatch completes with `rawOutput` = the child's answer.
- Results over 30,000 characters are replaced by a 500-char head/tail preview and offloaded
  to a file. Parallel MCP calls overlap. MCP image results reach the model.
- `session/cancel` returns `cancelled` immediately but the running MCP tool keeps running.

Codebase facts:

- `agent/src/archie_agent/app.py` `lifespan` builds one `AgentHarness`; routes use
  `_agent.session.{model_id,session_id,turn_index}`, `event_bus`, `turn_active`,
  `try_begin_turn`, `handle_message`, `run_manual_compaction`, `interrupt`, `switch_model`.
- `harness.py` `_execute_tool` dispatches `exec` (via `run_exec` with `on_start` and
  `runner_context`) and native tools from `exec/tool.py` `create_registry()`; `_on_proc_start`
  / `_cancel_active_proc` kill a tool's process group.
- `EventFactory` (`event_factory.py`) builds canonical events; `SessionEventBus` persists and
  broadcasts. TUI and metrics recognise a `task` tool call (`input.tasks[i].agent`) as a
  subagent launch and `skill` (`input.name`) as a skill load.
- `orchestrator/lifecycle.py` `start_session(workspace, config)` builds the `docker run` command
  and passes session env with `-e`. `POST /sessions` takes `{"workspace"}`. `cli/cli.py`
  `start` posts it.
- `plugins/openai/` is a credential-only plugin (static `api_key`, `set_env: false`); its
  credential type is generated into `CREDENTIAL_TYPES`, which makes `get_credential("openai")`
  and `archie auth login openai` work.
- `mcp` 1.30 is already an agent dependency (low-level `Server` and
  `StreamableHTTPSessionManager` available); `uvicorn` and `starlette` too.
- Tool results are capped at 32,000 characters (`exec/tool.py` `_MAX_RESULT_CHARS`,
  `truncate_result`).

## Baseline

- Check commands: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check <files>`
- Setup: `uv sync`
- Setup leftovers: none (`.venv` is gitignored)
- Failing before this work: none (2,449 passed; ruff clean)
- Not included: `plans/060-spec-container-aws-profiles.md`, `plans/070-build-nexus-plugins.md`
  (untracked in the user's checkout, unrelated)

## Decisions

| #   | Decision | Chosen | Rejected | Why | Source |
|-----|----------|--------|----------|-----|--------|
| D1  | How a session selects the backend | `archie start --kiro` → `POST /sessions {"backend": "kiro"}` → container env `ARCHIE_BACKEND=kiro` | `agent.backend` config switch; Kiro keys in the model catalog | user wants a per-session choice with exactly `--kiro` | user Q1 |
| D2  | Granularity of `llm_request` | one per Kiro turn: credits, derived cost, `duration_ms` = `elapsedTime`, tokens 0 | one per inferred model request | per-turn is what Kiro reports; inferred request boundaries are unreliable | user Q2 |
| D3  | Where Kiro settings live | top-level `kiro:` section in `config.yaml` (`shared/schemas.py` `KiroConfig`) | under `agent:`; a plugin `settings` block | agreed config shape in the research doc; it configures a backend, not a tool plugin | self |
| D4  | How the API key is declared | credential-only plugin `plugins/kiro/` (static `api_key`, env `KIRO_API_KEY`, `set_env: false`), like `plugins/openai/` | hand-written core credential type in `shared/credentials/models.py` | reuses the generated credential type, `archie auth login kiro` and auth status for free | self |
| D5  | Kiro model keys | `kiro/<kiro model id>` (e.g. `kiro/gpt-5.6-luna`), outside `DEFAULT_MODELS` | `ModelEntry`s with a new `KiroProvider` | Kiro bills credits, not tokens; catalog entries need token rates and a context size that Kiro doesn't give | self |
| D6  | ACP client | hand-written asyncio JSON-RPC client in `agent/src/archie_agent/kiro/acp.py` (spike pattern) | `agent-client-protocol` SDK | ~150 lines, no new dependency, and Kiro's `_meta` extensions and extra update kinds pass through untouched | self |
| D7  | MCP server | `mcp` low-level `Server` + `StreamableHTTPSessionManager` in the agent process, uvicorn on `127.0.0.1:<free port>`, no auth token | FastMCP; mounting on port 8080; bearer token | tools already have JSON schemas (`ToolSpec`); 8080 is published to the host; any process able to reach container loopback can already run shell as the same user | self |
| D8  | Source of canonical tool events | the ACP stream (`tool_call` / `tool_call_update`); the MCP server only executes | MCP server emits events | ACP carries `toolCallId`, scope (`agentSubtaskId`) and the content the model actually saw; MCP calls can't be correlated to ACP IDs | self |
| D9  | Kiro-internal tool calls | `tool_load` and "Subagent Response" are dropped from the canonical log; skill loads become `skill`; subagent dispatches become `task` with `{"tasks":[{"agent","prompt"}]}` | logging `tool_load`; new event kinds | existing TUI, viewer and metrics already understand `skill` and `task`; `tool_load` is Kiro overhead | self |
| D10 | Subagent scope | child events use `scope` = the dispatch `toolCallId`, `subagent_index` 0; child terminal from the dispatch's final status | one scope per Kiro execution ID | matches the native contract (scope = launching tool call ID) | self |
| D11 | Iterations and request IDs | one `iteration_start` (iteration 0) per turn; one ULID request ID per turn used by every event of the turn and by the turn's `llm_request` | an iteration per inferred model request | consistent with D2; request identity stays unique | self |
| D12 | Tool result size | MCP layer re-caps results at 29,000 characters with Archie's truncation marker | keep 32,000 | Kiro replaces anything over 30,000 with a 500-char preview | self |
| D13 | Monthly credit total for overage pricing | at harness start, sum `credits` from `llm_request` lines in `~/.nexus/sessions/*.jsonl` modified since the current period start; then keep a running total in memory | read `metrics.db`; a new state file | the session logs are the accounting source of truth (CONTRIBUTING); metrics.db is orchestrator-owned and lags 15 s | self |
| D14 | Startup failure handling | any failed check (missing key, Kiro exit, `config_error`, wrong mode, builtin tool tag other than `read`, `contentCollection` not `disabled`, default model not offered, MCP not connected in 20 s) logs the reason and exits the agent process (code 1), so the session never becomes ready | start degraded; fall back to native | fail closed: a silent fallback would run Kiro's built-in tools or collect content | self |
| D15 | Kiro dies mid-session | the active turn ends with `turn_error`; later messages get `turn_error` "Kiro backend stopped; start a new session" | auto-restart | restart loses Kiro's conversation state anyway; no resume (user) | self |
| D16 | `/compact` on Kiro | live `error_notice` (kind `unsupported`): Kiro compacts automatically | `_kiro/session/compact` | untested method; Kiro auto-compacts at 80 % | self |
| D17 | Interrupt targeting one subagent | cancels the whole turn (`session/cancel`) and kills every running MCP tool process | ignore | Kiro has no per-subagent cancel; the user pressed stop | self |
| D18 | Permission requests | answer `session/request_permission` with outcome `cancelled` and log a warning | auto-allow | the rules should prevent prompts; anything else is unexpected and must not be granted | self |
| D19 | Raw ACP frame log | `~/.nexus/logs/kiro/<session-id>.acp.jsonl`, both directions, timestamped | none | the spike's main debugging tool; same sensitivity as the session log in the same home | self |
| D20 | TUI for Kiro sessions | status bar shows `<kiro id> (Kiro)`; model palette lists `kiro/<id>` for the host config's `kiro.models` when the session model key starts with `kiro/`, catalog models otherwise | a new wire event listing models | no protocol change; the CLI already reads `config.yaml` | self |

## Requirements

Selection and startup
- MUST start the Kiro backend only when `archie start --kiro` is used; without the flag the
  session runs the native loop exactly as today.
  - AC: `POST /sessions {"workspace": w, "backend": "kiro"}` adds `-e ARCHIE_BACKEND=kiro` to
    the `docker run` command; omitting `backend` adds nothing; `"backend": "x"` → 400
    `{"error": "Invalid backend 'x': expected 'native' or 'kiro'"}`.
  - AC: `archie start --kiro` posts `"backend": "kiro"`; plain `archie start` posts no `backend`.
- MUST launch `kiro-cli acp --agent-engine=v3` in its own process group with `KIRO_API_KEY`
  from `credentials.yaml` `kiro.api_key` (fallback: `KIRO_API_KEY` in the agent env) set only
  in that process's environment.
  - AC: fake-Kiro test records its env: `KIRO_API_KEY` present; the agent's `os.environ`
    unchanged; no key value in the ACP frame log or Python log output.
- MUST generate `~/.kiro/agents/archie.json` and `archie-worker.json` (prompts under
  `~/.kiro/archie/`, relative `file://` URIs), with tools `@archie`, `subagent`,
  `disclose_context` (worker: `@archie`), the three permission rules, `availableAgents` /
  `trustedAgents` = `["archie-worker"]`, `includeMcpJson: false`, `includePowers: false`.
  - AC: generator unit test asserts the JSON; the prompt URIs are relative.
- MUST fail startup (agent exits 1, reason logged) on each D14 condition.
  - AC: one fake-Kiro test per condition asserts the startup error names the condition.
- MUST set and verify `contentCollection` = `disabled`, `autopilot` = `on` and the model on
  every new session before the first turn.
  - AC: fake-Kiro log shows the three `session/set_config_option` calls; a fake that keeps
    `contentCollection` `enabled` fails startup.
- MUST set `telemetry.enabled: false` in `~/.kiro/settings/cli.json`, keeping other keys.
  - AC: unit test on an existing settings file.

Prompt, tools, skills, subagents
- MUST use Archie's prompt (identity, soul, environment, tools, brain) plus a Kiro section
  (`persona/prompts/kiro.md`), without the skills catalog, `<agents.md>` or `task`
  delegation guidance; the worker prompt is the default-agent body with environment, tools,
  brain and the Kiro section.
  - AC: prompt test: `kiro.md` text present; `<skills>`, `<agents.md>`, "Delegating with
    task" absent.
- MUST serve Archie's `exec` and native tools (not `skill`, `task`) over MCP with their
  existing schemas, live tool policy and runner context; results capped at 29,000
  characters; images returned as MCP image content; shell non-zero exit and exceptions
  returned with `isError: true`.
  - AC: MCP client test lists exactly the registry tools; calls `read` on a PNG (image
    content), `shell "exit 3"` (isError), and a 40,000-char result (≤ 29,000 + marker).
- MUST list skills from persona, `~/.agents/skills` and enabled plugins only, as explicit
  `skill://` paths.
  - AC: generator test with a disabled plugin: its skill paths are absent.
- MUST kill running MCP tool processes on interrupt.
  - AC: test: interrupt during a `shell "sleep 30"` MCP call; the process group is gone
    within 3 s.

Event mapping
- MUST write, per turn: `user_message`, `iteration_start`, live `text_delta`s,
  `assistant_message` (flushed before the next root tool call and at turn end),
  `tool_call` / `tool_result` per Archie tool (name from `tool_id`, input = `arguments`,
  `duration_ms` from ACP timestamps, `is_error` for `failed`), one `llm_request`, and one
  terminal event: `turn_complete(stop_reason)`, `turn_interrupted` for `cancelled`, or
  `turn_error` for a JSON-RPC error / Kiro exit.
  - AC: translator tests on frames copied from the spike runs.
- MUST map skill loads to `tool_call name "skill" input {"name"}` and subagents to
  `tool_call name "task" input {"tasks":[{"agent","prompt"}]}`, with child text and tool
  events scoped to the dispatch `toolCallId` (`subagent_index` 0), a child terminal event,
  and a parent `tool_result` with the child's answer.
  - AC: translator test on the spike's subagent frames.
- MUST drop `tool_load` and "Subagent Response" tool calls.

Accounting
- MUST add optional `credits: float | None = None` to `llm_request` (native requests
  `null`), documented in `docs/event-spec.md`; older logs without the field still decode.
  - AC: decode test for an old line; round-trip with credits.
- MUST record per turn `credits` = sum of `promptTurnSummaries[].usage` with unit `credit`,
  `cost_usd` = credits priced at `rate_usd` until the period's cumulative credits reach
  `included`, `overage_rate_usd` beyond (split when a turn crosses), status `completed` for
  `success` and `interrupted` for `aborted` (cost still charged), `model_key` `kiro/<model>`.
  - AC: pricing tests: under, crossing, over; period start for `resets_on` 1 and 31 in a
    30-day month; log-scan test sums only lines with `sent_at` in the current period.

Session control
- MUST switch models through `switch_model` with `kiro/<id>` keys from the allowlist (and
  offered by Kiro), re-applying `effort`; other keys → `error_notice unknown_model`.
  - AC: fake-Kiro test sees `set_config_option model`; `bedrock-…` key rejected.
- MUST apply `kiro.effort` when set and the selected model's `effortLevels` contain it, via
  `set_config_option effortLevel`; otherwise log a warning and leave the model default.
  Unset → no effort call.
  - AC: fake-Kiro tests for set/unsupported/unset.
- MUST validate the `kiro` config at load: `default_model` in `models`, every model a
  non-empty string, `effort` in `none|low|medium|high|xhigh|max` when set, `included ≥ 0`,
  `1 ≤ resets_on ≤ 31`, rates ≥ 0.
  - AC: config tests for each invalid case.
- MUST log raw ACP frames to `~/.nexus/logs/kiro/<session-id>.acp.jsonl`, filter the SQLite
  `ExperimentalWarning` from stderr and log other stderr lines at warning level.
- MUST kill the Kiro process group and stop the MCP server on agent shutdown.
  - AC: test: after lifespan exit the fake's pid no longer exists.

TUI
- MUST show `<id> (Kiro)` as the model name for `kiro/` keys and list allowlisted Kiro
  models (and no catalog models) in the palette for Kiro sessions.
  - AC: TUI provider test with a `kiro/` session key.

Out of scope: Kiro session resume; remote/cloud sessions; manual compaction on Kiro;
per-request latency; token counts; credit display in insights/`archie stats`; context %
for Kiro models in the status bar; turn cost limit enforcement on Kiro; copying Kiro's own
`~/.kiro/logs` out of the container.

## Design

### Overview

A `KiroHarness` with the same surface `app.py` uses replaces `AgentHarness` when
`ARCHIE_BACKEND=kiro`. It writes Kiro agent files, starts an in-process MCP server that
executes Archie tools, spawns Kiro over ACP, verifies the session configuration, and turns
each `session/prompt` into canonical events through a pure translator. Usage is one
`llm_request` per turn priced from credits.

### Code structure

New package `agent/src/archie_agent/kiro/`:
- `acp.py` — `AcpClient`: spawn (process group, env), JSON-RPC request/notify, response
  futures, agent→client request handlers (`session/request_permission` → cancelled; other
  methods → `-32601`), notification callback, frame log, stderr pump with the SQLite filter,
  `close()` (SIGTERM group, SIGKILL after 2 s). Follow `/tmp/kiro-spike/acp_client.py`.
- `agent_files.py` — render prompts and agent JSON, settings merge, skill resource list from
  `discover_skills(enabled)`.
- `mcp_server.py` — `ArchieMcpServer(registry, runner_context, on_proc_start)`: low-level
  `Server` `list_tools` / `call_tool`, `StreamableHTTPSessionManager`, uvicorn on
  `127.0.0.1:0`, `start()` → URL, `stop()`, `cancel_all()` over a set of active processes.
  Tool dispatch mirrors `AgentHarness._execute_tool` for `exec` and generic tools.
- `translate.py` — `TurnTranslator(factory, turn, request_id)`: `feed(update) -> list[Event]`,
  `finish(stop_reason | error) -> list[Event]`. Pure; holds text buffers, tool start times,
  `agentSubtaskId` → scope map, per-scope factories.
- `harness.py` — `KiroHarness`: `start()` (startup sequence and D14 checks), `close()`,
  `session` (a small `KiroSessionState(model_id, session_id, turn_index)`), `event_bus`,
  `log_path`, `turn_active`, `try_begin_turn`, `handle_message`, `interrupt`,
  `switch_model(key) -> str | None` (error text), `run_manual_compaction`.

Shared:
- `shared/src/archie_shared/schemas.py` — `KiroCreditsConfig`, `KiroConfig`, `NexusConfig.kiro`.
- `shared/src/archie_shared/kiro_credits.py` (new) — `period_start(now, resets_on)`,
  `credit_cost(credits, used_before, cfg)`, `credits_used_since(sessions_dir, start)`.
- `shared/src/archie_shared/events.py` — `LLMRequest.credits`.

Other:
- `plugins/kiro/plugin.yaml`, `plugins/kiro/README.md` (new; follow `plugins/openai/`).
- `persona/prompts/kiro.md` (new); `agent/src/archie_agent/prompt.py` —
  `build_kiro_prompt()` / `build_kiro_worker_prompt()`.
- `agent/src/archie_agent/exec/tool.py` — `truncate_result(text, limit=_MAX_RESULT_CHARS)`.
- `agent/src/archie_agent/app.py` — backend selection, Kiro model switch, `/compact`
  routing, shutdown.
- `orchestrator/src/archie_orchestrator/{app.py,lifecycle.py}` — `backend` field and env.
- `cli/src/archie_cli/cli.py` — `--kiro`; `cli/src/archie_cli/tui/{app.py,models_provider.py}`.
- Docs: `docs/kiro.md` (new), `docs/architecture.md`, `docs/event-spec.md`, `docs/config.md`.

### Data model

```yaml
kiro:
  default_model: gpt-5.6-luna          # str, required in models
  models: [claude-opus-5.5, claude-sonnet-5.5, claude-haiku-5.5,
           gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna]   # list[str], non-empty
  effort: null                          # str | None
  credits:
    included: 2000                      # float ≥ 0
    resets_on: 1                        # int 1..31; clamp to month length
    rate_usd: 0.02                      # float ≥ 0
    overage_rate_usd: 0.04              # float ≥ 0
```

These values are the defaults. `LLMRequest.credits: float | None = None`. Period start =
the latest `resets_on` day (clamped to the month's length) at 00:00 UTC that is ≤ now.

### Data flow

Startup: key → settings merge → agent files → MCP server start → spawn → `initialize` →
`session/new {cwd: "/workspace", mcpServers: [], _meta.kiro.modeId: "archie"}` → checks (no
`config_error`, mode `archie`, builtin tags ⊆ {`read`}) → set `contentCollection`,
`autopilot`, `model` (+ `effortLevel`) and verify each → wait for `_kiro/mcp/status` archie
`connected` → ready. Credits used this period are scanned once here.

Turn: `user_message` → `iteration_start` → `session/prompt` → each `session/update` → translator
→ bus; `turn_completion` captured; prompt response → translator `finish` → `llm_request` →
terminal → `session_status`. A JSON-RPC error or EOF → `turn_error`.

Failure paths: Kiro exit mid-turn → pending futures fail with EOF → `turn_error`, harness marked
dead (D15). A `config_error` or a disallowed tool tag after startup → harness marked dead, Kiro
killed, active turn `turn_error`.

### Error handling

- `session/new` / config calls: 60 s timeout each → startup failure.
- `session/prompt`: no timeout (turns can be long); interrupt is the escape.
- Missing `turn_completion` before the prompt response → `llm_request` with credits 0 and
  status `no_usage`, `error` "no turn_completion from Kiro".
- Malformed JSON from Kiro stdout → logged to the frame log as `in-raw`, ignored.

### Non-functional

- MCP server bound to `127.0.0.1` only; DNS-rebinding protection with allowed host
  `127.0.0.1:<port>`.
- The API key exists only in the Kiro child env; frame log and logs never contain it.
- Credit scan reads only logs with mtime ≥ period start and only lines containing
  `"credits":` followed by a number.

## Milestones

### [ ] M1 — prefactor: config, credential, credits contract

- **Approach:** `KiroConfig` / `KiroCreditsConfig` in `schemas.py` with a `__post_init__`
  validator (msgspec structs) raising `ValueError` mapped to `ConfigError` like existing
  validation; `plugins/kiro/` copying `plugins/openai/` shape; `LLMRequest.credits`; the
  `kiro_credits` module. Update `docs/event-spec.md` and `docs/config.md`. Fix tests that
  compare encoded `llm_request` JSON.
- **Test seam:** `load_nexus_config`, `decode_event` / `encode_event`, the `kiro_credits`
  functions, `get_credential("kiro")`.
- **Deliverable:** config, credential and event contract in place; native behaviour unchanged.
- **Verify:** `uv run pytest -q tests/test_nexus_config.py tests/test_session_events.py
  tests/test_kiro_credits.py tests/test_plugin_layout.py` passes, then full `uv run pytest`.
- **Outcome:**

### [ ] M2 — tracer: a Kiro turn with an Archie tool, end to end

- **Approach:** `kiro/acp.py`, `kiro/agent_files.py` (without skills), `kiro/mcp_server.py`,
  `kiro/translate.py` (text, Archie tools, terminal, `llm_request`), `kiro/harness.py` with
  the full startup sequence and D14 checks; `prompt.py` Kiro builders and `kiro.md`;
  `truncate_result` limit; `app.py` selects `KiroHarness` on `ARCHIE_BACKEND=kiro` (unknown
  value → exit 1) and closes it on shutdown. Tests use `tests/fake_kiro.py`, a scripted ACP
  agent run with `sys.executable` whose scenario is chosen by an env var; KiroHarness
  takes a `command` override. ⚠️ The MCP server must be reachable before `session/new`.
- **Test seam:** `KiroHarness.start()` / `handle_message()` / `close()` with the fake, reading
  the session log; `TurnTranslator.feed/finish` on spike frames; an MCP client
  (`mcp.client.streamable_http`) against `ArchieMcpServer`.
- **Edge cases:** fake exits during `initialize` → startup error; `config_error` → startup
  error; prompt JSON-RPC error → `turn_error`; aborted turn → `turn_interrupted` with charged
  `llm_request`.
- **Deliverable:** with `ARCHIE_BACKEND=kiro`, a message produces text, an Archie tool call and
  result, and a credit-priced `llm_request` in the session log.
- **Verify:** `uv run pytest -q tests/test_kiro_*.py` passes; live smoke: run the agent app in
  the worktree with a temporary `ARCHIE_HOME_DIR` (copied `credentials.yaml`), send "run
  `echo KIRO-OK` with the shell tool" over `/stream`; the log has `tool_call name shell`,
  a `tool_result` containing `KIRO-OK`, and `llm_request` with `credits > 0` and
  `model_key kiro/gpt-5.6-luna`.
- **Outcome:**

### [ ] M3 — skills and the archie-worker subagent

- **Approach:** skill resources for enabled plugins in `agent_files.py`; translator handling
  of `disclose_context` → `skill`, dispatch → `task`, scoped child events and child
  terminal, dropping `tool_load` / "Subagent Response".
- **Test seam:** translator on the spike's run2 frames; generator test with a disabled plugin.
- **Deliverable:** skill loads and subagent runs appear in the log as `skill` and scoped `task`
  children.
- **Verify:** `uv run pytest -q tests/test_kiro_*.py`; live: ask Archie-on-Kiro to load the
  `research` skill and to delegate `echo SUB-OK` to archie-worker; the log has a `skill` call
  and a `task` call with scoped child `tool_call shell`.
- **Outcome:**

### [ ] M4 — session control: interrupt, model switch, effort, compaction notice, death

- **Approach:** `interrupt()` (cancel + `cancel_all`), `switch_model`, effort application,
  `/compact` notice, Kiro-death handling (D15), shutdown kill; `app.py` routing for
  `SwitchModelCommand` and `/compact` on Kiro.
- **Test seam:** `KiroHarness` with the fake; `app._handle_model_switch` with a Kiro harness.
- **Deliverable:** stop, model switch and effort work; a dead Kiro gives clear errors.
- **Verify:** `uv run pytest -q tests/test_kiro_*.py tests/test_model_switch.py`; live:
  interrupt a `sleep 60` shell call, see `turn_interrupted` and no `sleep` process.
- **Outcome:**

### [ ] M5 — per-session selection and TUI

- **Approach:** `--kiro` in `cli.py start`; `backend` in `sessions_post` and
  `start_session(..., backend="native")`; TUI status name and palette (D20); docs
  (`docs/kiro.md`, `architecture.md`).
- **Test seam:** orchestrator `POST /sessions` with mocked `start_session`; `start_session`
  docker command; CLI `start` with mocked httpx; TUI `ModelProvider`.
- **Deliverable:** `archie start --kiro` starts a Kiro-backed session; the TUI shows Kiro
  models.
- **Verify:** `uv run pytest -q tests/test_orchestrator_lifecycle.py tests/test_cli_start_stop.py
  tests/test_orchestrator_app.py` and the TUI test pass; full `uv run pytest` and
  `uv run ruff check .` pass.
- **Outcome:**

## Deviations

## Risks

- `effortLevel` was not in the spike's advertised `configOptions` (the default model `auto`
  has no effort). Assumed it is advertised once an effort-capable model is selected; M4 live
  check confirms, otherwise effort is sent as `_meta.kiro.effortLevel` on `session/new`.
- Response shape of `session/set_config_option` is undocumented; verification falls back to
  the latest `config_option_update`.
- Single `skill://` file paths (not globs) are assumed to work like the spike's globs.
- Builtin tag `read` is assumed to be the `disclose_context` category; the spike's model saw
  no read built-in.
- Concurrent Kiro sessions don't see each other's credits until restart, so overage pricing
  can lag by the other session's usage.
- Kiro or its service changes (preview models withdrawn, new tool tags) fail startup closed.

## Review

## Outcome

PR: pending
CI: pending

## Follow-ups

- Show credits in insights and `archie stats`.
- Context % for Kiro sessions from `context_usage.usagePercentage`.
- Copy `~/.kiro/logs` into `~/.nexus/logs/kiro/` at shutdown.

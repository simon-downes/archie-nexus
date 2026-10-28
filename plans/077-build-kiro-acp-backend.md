# 077 — Kiro ACP backend

Status: building
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
- `autopilot` on; local Kiro sessions only (no Kiro remote session sources or cloud sandbox;
  this is unrelated to Archie's orchestrator profiles, which work with `--kiro` as with native);
  no Kiro session resume (`~/.kiro` stays ephemeral);
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
| D2  | Granularity of `llm_request` | one per Kiro turn (root scope): credits, derived cost, `sent_at` = time `session/prompt` was sent, `duration_ms` = `turn_completion.elapsedTime` (wall clock if absent), tokens 0, `iteration` 0 | one per inferred model request | per-turn is what Kiro reports; inferred request boundaries are unreliable | user Q2 |
| D3  | Where Kiro settings live | top-level `kiro:` section in `config.yaml` (`shared/schemas.py` `KiroConfig`) | under `agent:`; a plugin `settings` block | config shape agreed with the user in the research doc; it configures a backend, not a tool plugin | user (research doc) |
| D4  | How the API key is declared | credential-only plugin `plugins/kiro/` (static `api_key`, env `KIRO_API_KEY`, `set_env: false`), like `plugins/openai/` | hand-written core credential type in `shared/credentials/models.py` | reuses the generated credential type, `archie auth login kiro` and auth status for free | self |
| D5  | Kiro model keys | `kiro/<kiro model id>` (e.g. `kiro/gpt-5.6-luna`), outside `DEFAULT_MODELS` | `ModelEntry`s with a new `KiroProvider` | Kiro bills credits, not tokens; catalog entries need token rates and a context size that Kiro doesn't give | self |
| D6  | ACP client | hand-written asyncio JSON-RPC client in `agent/src/archie_agent/kiro/acp.py` (spike pattern) | `agent-client-protocol` SDK | ~150 lines, no new dependency, and Kiro's `_meta` extensions and extra update kinds pass through untouched | self |
| D7  | MCP server | `mcp` low-level `Server` + `StreamableHTTPSessionManager` in the agent process, uvicorn on `127.0.0.1:<free port>`, no auth token | FastMCP; mounting on port 8080; bearer token | tools already have JSON schemas (`ToolSpec`); 8080 is published to the host; any process able to reach container loopback can already run shell as the same user | self |
| D8  | How Kiro finds the MCP server | `mcpServers.archie {type: http, url: http://127.0.0.1:<port>/mcp, timeout 10000, requestTimeout 600000}` in both generated agent JSONs; `session/new` sends `mcpServers: []`; the files are written after the MCP server has bound its port | URL in `session/new` `mcpServers` | the spike's verified config; `session/new` servers are not proven to reach subagents | self |
| D9  | Source of canonical tool events | the ACP stream (`tool_call` / `tool_call_update`); the MCP server only executes | MCP server emits events | ACP carries `toolCallId`, scope (`agentSubtaskId`) and the content the model actually saw; MCP calls can't be correlated to ACP IDs | self |
| D10 | Kiro-internal tool calls | `tool_load` and "Subagent Response" are dropped from the canonical log; skill loads become `skill`; subagent dispatches become `task` with `{"tasks":[{"agent","prompt"}]}` | logging `tool_load`; new event kinds | existing TUI, viewer and metrics already understand `skill` and `task`; `tool_load` is Kiro overhead | self |
| D11 | Subagent scope | child events use `scope` = the dispatch `toolCallId`, `subagent_index` 0; child terminal from the dispatch's final status | one scope per Kiro execution ID | matches the native contract (scope = launching tool call ID) | self |
| D12 | Iterations and request IDs | per scope, a segment = one `iteration` with its own ULID `request_id`. A scope's first update opens iteration 0 with an `iteration_start`. Once a `tool_result` has been written in the current iteration, or a dropped internal call (D10) followed text, the scope's next text or tool call opens the next iteration (`iteration_start`, new `request_id`). `assistant_message` is flushed for the scope before its next tool call, before a new iteration, and at scope end. The turn's `llm_request` has its own ULID | one request ID per turn | the TUI drops `text_delta`s for a request key after its `assistant_message` (`tui/app.py` lines 437, 732), so text after a tool call needs a new key; segments mirror the native text-then-tools iteration shape | self |
| D13 | Tool-call timestamps when `llm_request` comes last | metrics `_apply_request` (root, conversation purpose) backfills `tool_calls.timestamp` and `model_key` for every row of the same session and turn, any scope, whose `timestamp IS NULL` | emit `llm_request` before tools; give `tool_call` a timestamp field; match by `request_id` | Kiro tool calls carry segment request IDs (D12) that never equal the turn's `llm_request` id, so `_request_info` returns nothing; Kiro reports credits only at turn end and events are immutable; native rows resolve at insert, so the backfill touches nothing for them | self |
| D14 | How Kiro events are built | `EventFactory(model: ModelEntry | None)` builds all events except `llm_request`; `request()` raises if `model` is None. The translator builds `LLMRequest` directly with credits and the precomputed cost | dummy `ModelEntry`; a cost override on `request()` | `request()` zeroes cost for non-completed status, but aborted Kiro turns are charged; a dummy entry would fake token rates | self |
| D15 | Tool result size | MCP layer re-caps results at 29,000 characters with Archie's truncation marker; the Kiro prompt says 29,000 | keep 32,000 | Kiro replaces anything over 30,000 with a 500-char preview | self |
| D16 | Monthly credit total for overage pricing | at harness start, sum `credits` from `llm_request` lines in `~/.nexus/sessions/*.jsonl` with mtime ≥ period start and `sent_at` ≥ period start; then keep a running total in memory | read `metrics.db`; a new state file | the session logs are the accounting source of truth (CONTRIBUTING); metrics.db is orchestrator-owned and lags 15 s | self |
| D17 | Startup failure handling | any failed check (missing key, Kiro exit, step timeout, `config_error`, wrong mode, builtin tool tag other than `read`, `contentCollection` not `disabled`, `autopilot` not on, default model not offered, MCP not `connected`) writes the reason to `~/.nexus/logs/kiro/<session-id>.startup-error`, logs it, and exits the agent process (code 1). In `start_session`, a new `except DockerError` around `wait_for_ready` (for `backend=kiro`, both the exited and the timeout paths) stops the container as the `RuntimeError` branch does and re-raises a `DockerError` whose `stderr` appends the file's text when it exists, so `archie start --kiro` prints it | start degraded; fall back to native; reason only in container logs | fail closed: a silent fallback would run Kiro's built-in tools or collect content; the container runs with `--rm`, so its logs are gone | self |
| D18 | Startup time budget | Kiro startup has a 30 s overall deadline; each step waits at most the remaining time. The orchestrator's `wait_for_ready` uses 45 s for `backend=kiro` (30 s otherwise); the CLI's 60 s POST timeout stays | 60 s per step | the spike's startup took under 3 s; 45 s leaves room for `uv sync` and the deadline, and stays under the CLI's 60 s | self |
| D19 | What the agent's `lifespan` does on Kiro | loads config and the catalog; skips `get_model(global.model)`, the compaction-model check and `create_llm_client`; runs `startup_runner_state` and `run_startup_hooks` as today; builds `LiveToolPolicy` as today; `SessionStarted.model_key` = `kiro/<default_model>` | run the native checks too; skip plugin hooks | a bad `global.model` must not block a Kiro session; plugin hooks prepare state the Archie tools need | self |
| D20 | Kiro dies mid-session | stdout EOF marks the harness dead; the active turn ends with a persisted `turn_error` "Kiro backend stopped: <exit code>"; later messages write no `user_message` and get a live `error_notice` kind `turn_error` "Kiro backend stopped; start a new session" (the TUI ends its turn on that kind) | auto-restart | restart loses Kiro's conversation state anyway; no resume (user) | self |
| D21 | `/compact` on Kiro | `KiroHarness.run_manual_compaction()` releases its turn flag (taken by `try_begin_turn`) and emits a live `error_notice` kind `manual_compaction_failed`, message "Kiro compacts automatically; /compact is not available" (a kind the TUI already ends the turn on) | `_kiro/session/compact` | untested method; Kiro auto-compacts at 80 % | self |
| D22 | Interrupt targeting one subagent | cancels the whole turn (`session/cancel`) and kills every running MCP tool process | ignore | Kiro has no per-subagent cancel; the user pressed stop | self |
| D23 | Permission requests | answer `session/request_permission` with outcome `cancelled` and log a warning naming the tool | auto-allow | the rules should prevent prompts; anything else is unexpected and must not be granted | self |
| D24 | Raw ACP frame log | `~/.nexus/logs/kiro/<session-id>.acp.jsonl`, `{t, dir, msg}` per frame, both directions | none | the spike's main debugging tool; same sensitivity as the session log in the same home | self |
| D25 | TUI for Kiro sessions | status bar shows `<kiro id> (Kiro)`; model palette lists `kiro/<id>` for the CLI host config's `kiro.models` when the session model key starts with `kiro/`, catalog models otherwise. With a remote profile whose config differs, the agent rejects unknown keys with `error_notice unknown_model` | a new wire event listing models | no protocol change; the CLI already reads `config.yaml` | self |
| D27 | Encoding `credits` | `LLMRequest.credits` is omitted from the encoding when `None` (`credits: float | msgspec.UnsetType = msgspec.UNSET`; `omit_defaults` would also drop native `stop_reason`/`error` nulls), so native request lines are byte-identical to today | always encode `"credits": null` | older CLIs decode with unknown fields forbidden and would drop every native `llm_request`; omitting keeps that hazard to Kiro lines only | self |
| D28 | Unmapped Kiro tool calls | a `tool_call` that is not `@archie/*`, a skill load, a subagent dispatch, `tool_load` or "Subagent Response" is logged canonically under `name` = `_meta.kiro.toolId` or the title, input = `rawInput`, plus a warning in the agent log | drop it; fail the session | the user should see every action the model took; the startup tag check already blocks real built-ins | self |
| D26 | Tool result images | translator decodes each `rawOutput.imageBase64Urls` data URL: format from the MIME type, `bytes` = decoded length, width/height from Pillow, `source` "" | no image metadata | Pillow is already an agent dependency; the TUI and viewer show image metadata | self |

## Requirements

Selection and startup
- MUST start the Kiro backend only when `archie start --kiro` is used; without the flag the
  session runs the native loop exactly as today.
  - AC: `POST /sessions {"workspace": w, "backend": "kiro"}` adds `-e ARCHIE_BACKEND=kiro` to
    the `docker run` command and uses a 45 s ready timeout; omitting `backend` adds nothing
    and keeps 30 s; `"backend": "x"` → 400
    `{"error": "Invalid backend 'x': expected 'native' or 'kiro'"}`.
  - AC: `archie start --kiro` posts `"backend": "kiro"`; plain `archie start` posts no `backend`.
- MUST launch `kiro-cli acp --agent-engine=v3` in its own process group with `KIRO_API_KEY`
  from `credentials.yaml` `kiro.api_key` (fallback: `KIRO_API_KEY` in the agent env) set only
  in that process's environment.
  - AC: fake-Kiro test records its env: `KIRO_API_KEY` present; the agent's `os.environ`
    unchanged; no key value in the ACP frame log or captured log output.
- MUST generate `<kiro home>/agents/archie.json` and `archie-worker.json` (prompts under
  `<kiro home>/archie/`, relative `file://` URIs), each with `mcpServers.archie` pointing at
  the running MCP server URL, tools `@archie`, `subagent`, `disclose_context` (worker:
  `@archie`), the permission rules (`mcp archie/*`, `skill *`, `subagent archie-worker`;
  worker: `mcp archie/*`), `availableAgents` / `trustedAgents` = `["archie-worker"]`,
  `includeMcpJson: false`, `includePowers: false`.
  - AC: generator unit test asserts the JSON for a given URL; the prompt URIs are relative.
- MUST fail startup per D17 on each listed condition, within the D18 deadline.
  - AC: one fake-Kiro test per condition asserts exit code 1 and that the startup-error file
    names the condition.
  - AC: orchestrator test: a `DockerError` from `wait_for_ready` for `backend=kiro` with the
    startup-error file present → the 500 error text contains the file's text.
- MUST set and verify `contentCollection` = `disabled`, `autopilot` = `on` and the model on
  every new session before the first turn.
  - AC: the fake's request log shows the three `session/set_config_option` calls; a fake that
    keeps `contentCollection` `enabled` fails startup.
- MUST set `telemetry.enabled: false` in `<kiro home>/settings/cli.json`, keeping other keys.
  - AC: unit test on an existing settings file.
- MUST on Kiro skip the native model, compaction-model and LLM-client setup, run plugin
  startup hooks, and write `SessionStarted.model_key` = `kiro/<default_model>` (D19).
  - AC: lifespan test with `ARCHIE_BACKEND=kiro`, an invalid `global.model` and the fake:
    the app starts; the log's first event has `model_key` `kiro/gpt-5.6-luna`; a recorded
    startup hook ran. An unknown `ARCHIE_BACKEND` value exits 1.

Prompt, tools, skills, subagents
- MUST use Archie's prompt (identity, soul, environment, tools, brain) plus a Kiro section
  (`persona/prompts/kiro.md`: tools are reached through `tool_load` / `tool_call` as
  `archie::<name>`, results cap at 29,000 characters), without the skills catalog,
  `<agents.md>` or `task` delegation guidance; the tools section says 29,000 where the
  native one says 32,000. The worker prompt is the default-agent body with environment,
  tools, brain and the Kiro section.
  - AC: prompt test: `kiro.md` text and "29,000" present; `<skills>`, `<agents.md>`,
    "Delegating with task" and "32,000" absent.
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
- MUST write, per turn: `user_message`, segments per D12 (`iteration_start`, live
  `text_delta`s, `assistant_message`), `tool_call` / `tool_result` per Archie tool (name from
  `tool_id`, input = `arguments`, `duration_ms` from receive times, `is_error` for `failed`,
  `images` per D26), one root `llm_request` after the last segment, and one terminal event:
  `turn_complete(stop_reason)`, `turn_interrupted` for `cancelled`, or `turn_error` for a
  JSON-RPC error / Kiro exit.
  - AC: translator tests on frames copied from the spike runs, fed with explicit receive
    times: text → tool → text yields `iteration_start` 0 and 1 with distinct `request_id`s,
    and iteration 0's `assistant_message` precedes its `tool_call`.
  - AC: TUI test (`tests/test_tui_canonical.py`): the translated events stream both text
    segments live.
- MUST map skill loads to `tool_call name "skill" input {"name"}` and subagents to
  `tool_call name "task" input {"tasks":[{"agent","prompt"}]}`, with child segments per D12
  scoped to the dispatch `toolCallId` (`subagent_index` 0), a child terminal event, and a
  parent `tool_result` with the child's answer.
  - AC: translator test on the spike's run2 subagent frames.
- MUST drop `tool_load` and "Subagent Response" tool calls.
  - AC: translator test on run1 frames (`text | tool_load | text | @archie/shell`): no event
    for `tool_load`; the two texts land in iterations 0 and 1.
- MUST log unmapped Kiro tool calls per D28.
  - AC: translator test: a `tool_call` with `_meta.kiro.toolId` "web_search" yields
    `tool_call name web_search` and its `tool_result`.
- MUST let metrics give Kiro tool calls the turn's request timestamp and model key (D13).
  - AC: `tests/test_orchestrator_metrics.py`: fold `tool_call` then `llm_request` of the
    same turn; the `tool_calls` row has the request's `sent_at` and `model_key`. A native
    sequence (request first) is unchanged.

Accounting
- MUST add optional `credits: float` to `llm_request`, absent (`UNSET`) for native requests and
  omitted from the encoding (D27), documented in `docs/event-spec.md` with the older-client note;
  older logs without the field still decode.
  - AC: decode test for an old line; round-trip with credits; a native request encodes with
    no `credits` key.
- MUST record per turn `credits` = sum of `promptTurnSummaries[].usage` with unit `credit`,
  `cost_usd` = credits priced at `rate_usd` until the period's cumulative credits reach
  `included`, `overage_rate_usd` beyond (split when a turn crosses), status `completed` for
  `success` and `interrupted` for `aborted` (cost still charged), `model_key` `kiro/<model>`.
  No `turn_completion` before the prompt response → credits 0, status `no_usage`, `error`
  "no turn_completion from Kiro".
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
- MUST answer `/compact` on Kiro with D21's `error_notice` and start no turn.
  - AC: app test: `/compact` on a Kiro harness yields `error_notice` kind
    `manual_compaction_failed`, writes no `user_message`, and `turn_active` is false after.
- MUST answer `session/request_permission` with outcome `cancelled` and a warning (D23).
  - AC: fake sends a permission request mid-turn; its recorded response has outcome
    `cancelled`; the warning names the tool.
- MUST handle Kiro death per D20.
  - AC: fake exits mid-turn → persisted `turn_error` containing "Kiro backend stopped"; the
    next message → live `error_notice` kind `turn_error` with "start a new session", no new
    persisted event, and `turn_active` false.
- MUST log raw ACP frames per D24, filter the SQLite `ExperimentalWarning` from stderr and
  log other stderr lines at warning level.
  - AC: fake-Kiro test: the frame log has an `out` `initialize` and an `in` `session/new`
    result; the fake's warning line is absent from captured logs and another stderr line is
    present at WARNING.
- MUST kill the Kiro process group and stop the MCP server on agent shutdown.
  - AC: test: after `close()` the fake's pid no longer exists and the MCP port refuses
    connections.

TUI
- MUST show `<id> (Kiro)` as the model name for `kiro/` keys and list allowlisted Kiro
  models (and no catalog models) in the palette for Kiro sessions.
  - AC: `tests/test_tui_models_provider.py` (new) with a `kiro/` session key and a native one.

Out of scope: Kiro session resume; remote/cloud sessions; manual compaction on Kiro;
per-request latency; token counts; credit display in insights/`archie stats`; context %
for Kiro models in the status bar; turn cost limit enforcement on Kiro; copying Kiro's own
`~/.kiro/logs` out of the container.

## Design

### Overview

A `KiroHarness` with the same surface `app.py` uses replaces `AgentHarness` when
`ARCHIE_BACKEND=kiro`. It starts an in-process MCP server that executes Archie tools, writes
Kiro agent files pointing at it, spawns Kiro over ACP, verifies the session configuration,
and turns each `session/prompt` into canonical events through a pure translator. Usage is one
`llm_request` per turn priced from credits.

### Code structure

New package `agent/src/archie_agent/kiro/`:
- `acp.py` — `AcpClient`: spawn (process group, env), JSON-RPC request/notify, response
  futures, agent→client request handlers (`session/request_permission` → cancelled; other
  methods → `-32601`), notification callback with receive time, frame log, stderr pump with
  the SQLite filter, EOF callback, `close()` (SIGTERM group, SIGKILL after 2 s). Follow
  `/tmp/kiro-spike/acp_client.py`.
- `agent_files.py` — render prompts and agent JSON (given the MCP URL), settings merge, skill
  resource list from `discover_skills(enabled)`.
- `mcp_server.py` — `ArchieMcpServer(registry, runner_context, tool_policy)`: low-level
  `Server` `list_tools` / `call_tool`, `StreamableHTTPSessionManager`, uvicorn on
  `127.0.0.1:0`, `start()` → URL, `stop()`, `cancel_all()` over a set of active process
  groups. Tool dispatch follows `AgentHarness._execute_tool` in `harness.py` for `exec` and
  generic tools.
- `translate.py` — `TurnTranslator(factory_for_scope, turn)`: `feed(update, at: float) ->
  list[Event]`, `finish(stop_reason | error, at) -> list[Event]`, `llm_request(...)`. Pure:
  receive times come from the caller. Holds per-scope segment state (D12), tool start
  times, the `agentSubtaskId` → scope map.
- `harness.py` — `KiroHarness(config, kiro_home=Path.home()/".kiro", command=None, ...)`:
  `start()` (startup sequence, D17 checks, D18 deadline), `close()`, `session` (a small
  `KiroSessionState(model_id, session_id, turn_index)`), `event_bus`, `log_path`,
  `turn_active`, `try_begin_turn`, `handle_message`, `interrupt`, `switch_model(key) -> str |
  None` (error text), `run_manual_compaction`.

Shared:
- `shared/src/archie_shared/schemas.py` — `KiroCreditsConfig`, `KiroConfig`, `NexusConfig.kiro`.
- `shared/src/archie_shared/kiro.py` (new) — `period_start(now, resets_on)`,
  `credit_cost(credits, used_before, cfg)`, `credits_used_since(sessions_dir, start)`,
  `startup_error_path(home, session_id)`, `frame_log_path(home, session_id)`.
- `shared/src/archie_shared/events.py` — `LLMRequest.credits`.

Other:
- `agent/src/archie_agent/event_factory.py` — `model: ModelEntry | None` (D14).
- `plugins/kiro/plugin.yaml`, `plugins/kiro/README.md` (new; follow `plugins/openai/`).
- `persona/prompts/kiro.md` (new); `agent/src/archie_agent/prompt.py` —
  `build_kiro_prompt()` / `build_kiro_worker_prompt()`.
- `agent/src/archie_agent/exec/tool.py` — `truncate_result(text, limit=_MAX_RESULT_CHARS)`.
- `agent/src/archie_agent/app.py` — backend selection (D19), Kiro model switch, `/compact`
  routing, shutdown.
- `orchestrator/src/archie_orchestrator/{app.py,lifecycle.py}` — `backend` field, env,
  ready timeout, startup-error text (D17, D18).
- `orchestrator/src/archie_orchestrator/metrics.py` — D13 backfill in `_apply_request`.
- `cli/src/archie_cli/cli.py` — `--kiro`; `cli/src/archie_cli/tui/{app.py,models_provider.py}`.
- Tests (new): `tests/fake_kiro.py`, `tests/test_kiro_credits.py`,
  `tests/test_kiro_translate.py`, `tests/test_kiro_mcp.py`, `tests/test_kiro_harness.py`,
  `tests/test_kiro_agent_files.py`, `tests/test_tui_models_provider.py`.
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

These values are the defaults. `LLMRequest.credits: float | UnsetType = UNSET`. Period start =
the latest `resets_on` day (clamped to the month's length) at 00:00 UTC that is ≤ now.
Files under `~/.nexus/logs/kiro/`: `<session-id>.acp.jsonl` (appended for the session's
life) and `<session-id>.startup-error` (written only on startup failure); neither is
rotated or deleted by Archie.

### Data flow

Startup (30 s deadline): key → settings merge → MCP server start (URL known) → agent files
with that URL → spawn → `initialize` → `session/new {cwd: "/workspace", mcpServers: [],
_meta.kiro.modeId: "archie"}` → checks (no `config_error`, mode `archie`, builtin tags ⊆
{`read`}) → set `contentCollection`, `autopilot`, `model` (+ `effortLevel`) and verify each →
wait for `_kiro/mcp/status` archie `connected` → ready. Credits used this period are scanned
once here. Any failure → D17.

Turn: `user_message` → `session/prompt` (time recorded as `sent_at`) → each `session/update`
→ translator (with receive time) → bus; `turn_completion` captured; prompt response →
translator `finish` → final `assistant_message`s → `llm_request` → terminal →
`session_status`. A JSON-RPC error or EOF → `turn_error`.

Failure paths: Kiro exit mid-turn → pending futures fail with EOF → `turn_error`, harness
marked dead (D20). A `config_error` or a disallowed tool tag after startup → harness marked
dead, Kiro killed, active turn `turn_error`.

### Error handling

- Startup steps: bounded by the remaining D18 deadline → D17 failure naming the step.
- `session/prompt`: no timeout (turns can be long); interrupt is the escape.
- Missing `turn_completion` → `no_usage` `llm_request` (Accounting requirement).
- Malformed JSON from Kiro stdout → logged to the frame log as `in-raw`, ignored.
- Startup-error file write fails → logged; exit 1 still happens.

### Non-functional

- MCP server bound to `127.0.0.1` only; DNS-rebinding protection with allowed host
  `127.0.0.1:<port>`.
- The API key exists only in the Kiro child env; frame log and logs never contain it.
- Credit scan reads only logs with mtime ≥ period start and only lines containing
  `"credits":` followed by a number.

## Milestones

### [x] M1 — prefactor: config, credential, credits contract

- **Approach:** `KiroConfig` / `KiroCreditsConfig` in `schemas.py` with a `__post_init__`
  validator (msgspec structs) raising `ValueError` mapped to `ConfigError` like existing
  validation; `plugins/kiro/` copying `plugins/openai/`; `LLMRequest.credits`; the new
  `archie_shared/kiro.py`; `EventFactory` optional model (D14); metrics backfill (D13).
  Update `docs/event-spec.md` and `docs/config.md`. Fix tests that compare encoded
  `llm_request` JSON.
- **Test seam:** `load_nexus_config`, `decode_event` / `encode_event`, the `archie_shared.kiro`
  functions, `get_credential("kiro")`, metrics `_Folder.apply`.
- **Deliverable:** config, credential, event and metrics contract in place; native behaviour
  unchanged.
- **Verify:** `uv run pytest -q tests/test_nexus_config.py tests/test_session_events.py
  tests/test_kiro_credits.py tests/test_plugin_layout.py tests/test_orchestrator_metrics.py`
  passes, then full `uv run pytest`.
- **Outcome:** Built `KiroConfig`/`KiroCreditsConfig` (`schemas.py`), `plugins/kiro/`,
  `LLMRequest.credits` (UNSET, omitted for native), `archie_shared/kiro.py` (pricing,
  `CreditLedger`, log scan, paths, model keys), optional `EventFactory` model, and the
  metrics backfill. The plan named this module `kiro_credits`; it is `archie_shared/kiro.py` as
  in Code structure. Evidence: full `uv run pytest` 2,473 passed; `ruff check .` clean;
  provider registry tests updated for the new `kiro` credential.

### [ ] M2 — tracer: a Kiro turn with an Archie tool, end to end

- **Approach:** `kiro/acp.py`, `kiro/agent_files.py` (without skills), `kiro/mcp_server.py`,
  `kiro/translate.py` (segments, Archie tools, terminal, `llm_request`), `kiro/harness.py`
  with the startup sequence including the telemetry merge and the three
  `set_config_option` calls (so the live smoke never runs with content collection on), but
  without the D17 verification checks beyond "Kiro exited"; `prompt.py`
  Kiro builders and `kiro.md`; `truncate_result` limit; `app.py` selects `KiroHarness` on
  `ARCHIE_BACKEND=kiro` per D19 and closes it on shutdown; frame log and stderr filter.
  Tests use `tests/fake_kiro.py`, a scripted ACP agent run with `sys.executable` whose
  scenario is chosen by an env var and which records requests and env to a file;
  `KiroHarness` takes `command` and `kiro_home` overrides.
- **Test seam:** `KiroHarness.start()` / `handle_message()` / `close()` with the fake, reading
  the session log; `TurnTranslator.feed/finish` on spike frames; an MCP client
  (`mcp.client.streamable_http`) against `ArchieMcpServer`; the TUI canonical test.
- **Edge cases:** prompt JSON-RPC error → `turn_error`; aborted turn → `turn_interrupted` with
  charged `llm_request`; text → tool → text → two iterations.
- **Deliverable:** with `ARCHIE_BACKEND=kiro`, a message produces text, an Archie tool call and
  result, and a credit-priced `llm_request` in the session log.
- **Verify:** `uv run pytest -q tests/test_kiro_*.py tests/test_tui_canonical.py` passes. Live
  smoke: (1) `mkdir -p /tmp/kiro-smoke/home && cp ~/.nexus/credentials.yaml
  ~/.nexus/config.yaml /tmp/kiro-smoke/home/`; (2) in the worktree run
  `ARCHIE_BACKEND=kiro ARCHIE_SESSION_ID=kirosmoke-01 ARCHIE_HOME_DIR=/tmp/kiro-smoke/home
  ARCHIE_PERSONA_DIR=$PWD/persona ARCHIE_PLUGINS_DIR=$PWD/plugins uv run archie-agent --host
  127.0.0.1 --port 18080` in the background; (3) run `/tmp/kiro-smoke/drive.py` (a
  `websockets` client from the agent venv: connect `ws://127.0.0.1:18080/stream`, send
  `{"type":"message","content":<arg>}`, print events until a terminal event, optional
  `--interrupt-after <s>`) with "run `echo KIRO-OK` with the shell tool". Pass: the session
  log has `tool_call name shell`, a `tool_result` containing `KIRO-OK`, and `llm_request` with
  `credits > 0` and `model_key kiro/gpt-5.6-luna`.
- **Outcome:** Built `kiro/acp.py`, `agent_files.py`, `mcp_server.py`, `translate.py`,
  `harness.py`, the Kiro prompt builders, `persona/prompts/kiro.md`, the `truncate_result`
  limit, and `app.py` backend selection (`_kiro_lifespan`, Kiro model switch). Some M3/M5
  surface landed early because it was small and the fake made it cheap to test: the
  missing-key check and startup-error file, `switch_model` and effort, `/compact` notice,
  permission answer, interrupt, Kiro-death handling and shutdown kill. M3/M5 still own the
  remaining D17 checks and the live interrupt check. Tests: `tests/fake_kiro.py` (real MCP
  calls back to the harness), spike frames in `tests/fixtures/kiro/`,
  `test_kiro_{translate,agent_files,mcp,harness}.py`. Evidence: full `uv run pytest`
  2,511 passed; `ruff check .` clean. Live smoke (real `kiro-cli` 2.34.2): set-config
  responses showed `contentCollection disabled`, `autopilot on`, `model gpt-5.6-luna`;
  advertised tags were `read` plus `@archie/*` only; the log had `tool_call shell`,
  `tool_result` `[exit: 0]\nKIRO-OK`, and `llm_request` `credits 0.12071`, `cost_usd 0.002414`,
  `model_key kiro/gpt-5.6-luna`. On SIGINT the agent killed Kiro (exit -15).

### [ ] M3 — startup checks and failure reporting

- **Approach:** in `KiroHarness.start()`: the D17 checks, the D18 deadline, the
  startup-error file; `app.py` exits 1 on `KiroStartupError`.
- **Test seam:** `KiroHarness.start()` with fake scenarios, one per D17 condition.
- **Deliverable:** every misconfiguration stops the session with a named reason on disk.
- **Verify:** `uv run pytest -q tests/test_kiro_harness.py -k startup` passes; live: with
  `kiro.default_model: no-such-model` in `/tmp/kiro-smoke/home/config.yaml`, the agent exits 1
  and `/tmp/kiro-smoke/home/logs/kiro/kirosmoke-01.startup-error` names the model.
- **Outcome:** `KiroHarness.start()` checks each D17 condition: config error, mode not
  `archie`, built-in tags other than `read`, set-config values verified from each response
  (`contentCollection`, `autopilot`, `model`), default model not offered, MCP not connected
  or no `@archie/*` tags, Kiro exit, and the 30 s deadline. After startup, a config error or
  a new disallowed tag marks the backend dead and kills Kiro, and the active turn ends in
  `turn_error`. Evidence: `tests/test_kiro_harness.py` (one parametrised case per
  condition, a post-startup case, and the lifespan error-file case); full suite and Ruff
  pass. Live: with `kiro.default_model: no-such-model` the agent stopped, and
  `logs/kiro/kirosmoke-01.startup-error` read "Kiro does not offer kiro.default_model
  'no-such-model'".

### [ ] M4 — skills and the archie-worker subagent

- **Approach:** skill resources for enabled plugins in `agent_files.py`; translator handling
  of `disclose_context` → `skill`, dispatch → `task`, scoped child segments and child
  terminal, dropping `tool_load` / "Subagent Response".
- **Test seam:** translator on the spike's run2 frames; generator test with a disabled plugin.
- **Deliverable:** skill loads and subagent runs appear in the log as `skill` and scoped `task`
  children.
- **Verify:** `uv run pytest -q tests/test_kiro_*.py`; live with the M2 driver: "load the
  `research` skill, then delegate `echo SUB-OK` to archie-worker"; the log has a `skill` call
  and a `task` call with a scoped child `tool_call shell`.
- **Outcome:** Skill resources and the skill/subagent translation were built in M2 (the
  translator was easier to write whole). M4 added the disabled-plugin generator test
  (`test_agent_file_lists_skills_of_enabled_plugins_only`, which also covers the
  `KIRO_API_KEY` env fallback) and the live check. Live: the log had `tool_call skill
  {"name": "research"}`, `tool_call task` (agent `archie-worker`), and a child `tool_call
  shell` scoped to the dispatch ID with `subagent_index` 0. The child `turn_complete`
  came before the parent `tool_result`, which reported `SUB-OK`.

### [ ] M5 — session control: interrupt, model switch, effort, compaction, permissions, death

- **Approach:** `interrupt()` (cancel + `cancel_all`), `switch_model`, effort application,
  `/compact` notice, permission answer, Kiro-death handling (D20), shutdown kill; `app.py`
  routing for `SwitchModelCommand` and `/compact` on Kiro.
- **Test seam:** `KiroHarness` with the fake; `app._handle_model_switch` and the `/compact`
  route with a Kiro harness.
- **Deliverable:** stop, model switch and effort work; a dead Kiro gives the D20 errors.
- **Verify:** `uv run pytest -q tests/test_kiro_*.py tests/test_model_switch.py`; live with the
  M2 driver: "run `sleep 60` with the shell tool" with `--interrupt-after 8` → a
  `turn_interrupted` event, and `pgrep -f "sleep 60"` finds nothing.
- **Outcome:** Most of M5 landed in M2. M5 added tests for effort set, unsupported and
  unset; for effort re-applied on model switch; for rejecting a model outside the
  allowlist; and for `app._handle_model_switch` routing to Kiro. Live: interrupt after 8 s
  gave `turn_interrupted` with an `interrupted` `llm_request`, and no `sleep 60` process
  was left. A switch to `bedrock-…` gave `error_notice unknown_model`, and a switch to
  `kiro/claude-haiku-5.5` gave `session_status` with that key. With `kiro.effort: low` on
  `claude-haiku-5.5`, Kiro advertised `effortLevel` after the model was set (options
  `low…max`); `set_config_option effortLevel low` returned `effortLevel low`. This closes
  the effort risk. Full suite and Ruff pass.

### [ ] M6 — per-session selection and TUI

- **Approach:** `--kiro` in `cli.py start`; `backend` in `sessions_post` and
  `start_session(..., backend="native")` with env, ready timeout and startup-error text; TUI
  status name and palette (D25); docs (`docs/kiro.md`, `architecture.md`).
- **Test seam:** orchestrator `POST /sessions` with mocked `start_session`; `start_session`
  docker command and error path with mocked docker helpers; CLI `start` with mocked httpx;
  TUI `ModelProvider`.
- **Deliverable:** `archie start --kiro` starts a Kiro-backed session; the TUI shows Kiro
  models.
- **Verify:** `uv run pytest -q tests/test_orchestrator_lifecycle.py tests/test_cli_start_stop.py
  tests/test_orchestrator_app.py tests/test_tui_models_provider.py` passes; full
  `uv run pytest` and `uv run ruff check .` pass. A real `archie start --kiro` needs the host
  orchestrator and Docker, which this container lacks: not verified here.
- **Outcome:** `archie start --kiro` posts `"backend": "kiro"`. `sessions_post` validates
  `backend`, with 400 for an unknown value. `start_session(..., backend=)` adds
  `-e ARCHIE_BACKEND=kiro`, waits 45 s, and on a Kiro `DockerError` stops the container and
  appends the startup-error file's text. The TUI status bar shows `<id> (Kiro)`, and
  `ModelProvider` lists the host's `kiro.models` for `kiro/` sessions. Added `docs/kiro.md`
  and updated `architecture.md`. The `Dockerfile` installs the latest stable `kiro-cli`
  headless build, checked against the manifest SHA-256. Kiro 2.35.0 was released during the
  build, and its live check found that the `model` option now arrives after `session/new`.
  The harness now waits for it (see Deviations). Evidence: full `uv run pytest` 2,543 passed;
  `ruff check .` clean. Live with 2.35.0 alone on `PATH` (the two binaries the Dockerfile
  installs): startup verified all settings, and a shell turn logged `credits 0.119739`. Not
  verified: `archie build` and a real `archie start --kiro`, because this container has no
  Docker.

## Deviations

- M2: when Kiro reports one parallel call's result before the next call's `tool_call`, the
  next call opens a new iteration (D12 as written). The spike's parallel shell turn yields
  four iterations, not three; the TUI streams every segment.
- M2: Kiro writes routine `[INFO]`/`[DEBUG]` lines to stderr (dozens per turn). These go to
  DEBUG; other stderr lines stay at WARNING as planned.
- M2: `ArchieMcpServer.cancel_all()` also kills tool processes that start before the next
  turn's `reset()`; without it a call reaching the MCP server just after an interrupt ran
  to completion and blocked Kiro's cancel.
- M6: Kiro 2.35.0 no longer includes the `model` option in the `session/new` result; it
  arrives in a later `config_option_update`. The harness waits for it (within the startup
  deadline) before checking `default_model`, and `tests/fake_kiro.py` now behaves the same way.
- M6: added a Kiro install step to the `Dockerfile` (the plan only noted the image gap);
  `kiro-cli` and `kiro-cli-chat` are the only binaries needed for ACP.
- M3: the agent process exits with code 3, not 1: uvicorn turns a `SystemExit` raised in
  the lifespan into exit 3, as it already does for native startup failures. The
  orchestrator only needs the container to exit and reads the reason from the file.
- M2: Kiro logs `acp.content_collection.resolved {"enabled":true}` at process start, before
  the session exists; the per-session set and its verified response are what apply.

## Risks

- ~~`effortLevel` not advertised before a model is chosen~~ — resolved in M5: Kiro
  advertises it after `model` is set, and setting it works.
- Response shape of `session/set_config_option` is undocumented; verification falls back to
  the latest `config_option_update`.
- Single `skill://` file paths (not globs) are assumed to work like the spike's globs.
- Builtin tag `read` is assumed to be the `disclose_context` category; the spike's model saw
  no read built-in.
- Concurrent Kiro sessions don't see each other's credits until restart, so overage pricing
  can lag by the other session's usage.
- Kiro or its service changes (preview models withdrawn, new tool tags) fail startup closed.
- Kiro `llm_request` lines carry `credits`, so older CLIs drop them (`forbid_unknown_fields`);
  native lines are unchanged (D27). Documented: update the CLI with the agent.
- Live smoke runs overwrite `~/.kiro/agents/archie*.json` in this dev container.

## Review

- Plan review pass 1 (build-it-reviewer): 6 ❌, 9 ⚠️, 8 ℹ️. Resolutions:
  - ❌ no AC for dropping internal tools / frame log → ACs added (Event mapping, Session control).
  - ❌ D15/D16/D18 behaviour untested → now D20/D21/D23 with requirements and ACs.
  - ❌ one request ID per turn breaks TUI streaming → D12 segments with distinct request IDs;
    translator and TUI ACs.
  - ❌ `llm_request` after tool calls leaves metrics rows without timestamp → D13 backfill and
    metrics AC.
  - ❌ MCP URL not configured → D8; generator AC includes the URL.
  - ❌ startup order vs port → Data flow starts MCP before writing agent files.
  - ⚠️ M2 too large → D17 checks split into M3.
  - ⚠️ live verify steps undriveable → M2 names the env, the command and the `websockets`
    driver; M4/M5 reuse it.
  - ⚠️ unnamed TUI test → `tests/test_tui_models_provider.py` (new).
  - ⚠️ event construction undecided → D14.
  - ⚠️ lifespan steps on Kiro unspecified → D19 with requirement and AC.
  - ⚠️ timeouts exceed readiness window → D18.
  - ⚠️ failure reason lost with `--rm` → D17 startup-error file surfaced by the orchestrator.
  - ⚠️ child flush timing → D12 covers every scope.
  - ⚠️ M1 is not the tracer → rejected: M1 is a labelled prefactor of about an hour; the
    tracer depends on its config, credential and `credits` field, and the spike already
    removed the ACP/MCP uncertainty.
  - ℹ️ D3 source → user (research doc). ℹ️ D20 remote config → D25 records the behaviour.
    ℹ️ new test file marked → Code structure lists new tests. ℹ️ translator purity → `feed`
    takes receive time. ℹ️ images → D26. ℹ️ `sent_at` → D2. ℹ️ older-client note → Accounting
    requirement and Risks. ℹ️ 32,000 vs 29,000 prompt text → prompt requirement and AC.

- Plan review pass 2: 1 ❌, 6 ⚠️, 3 ℹ️, all accepted. ❌ `/compact` notice kind left the TUI
  stuck → D21 uses `manual_compaction_failed` and releases the turn flag. ⚠️ DockerError not
  caught → D17 names the handler. ⚠️ `credits: null` on native lines → D27 omits it. ⚠️ remote
  profiles → Context clarifies "local" means Kiro sessions, not Archie profiles. ⚠️ M2 smoke
  without content-collection off → M2 does the settings calls. ⚠️ unmapped tool calls → D28.
  ⚠️ D20 event type → live `error_notice`. ℹ️ D13 reason, iteration 0 `iteration_start`, and
  `tool_load` segment split → D13, D12 and ACs updated. Two passes done; no review left.

## Outcome

PR: pending
CI: pending

## Follow-ups

- Show credits in insights and `archie stats`.
- Context % for Kiro sessions from `context_usage.usagePercentage`.
- Copy `~/.kiro/logs` into `~/.nexus/logs/kiro/` at shutdown.

# Kiro backend

`archie start --kiro` runs a session on [Kiro CLI](https://kiro.dev/docs/cli/acp/) instead of
Archie's own agent loop. Kiro chooses and calls the model. Archie keeps the prompt, the tools,
the session log, the TUI and accounting. Without `--kiro`, sessions run the native loop
unchanged. The backend is chosen per session.

## Requirements

- The session image includes `kiro-cli`. The `Dockerfile` installs the latest stable build
  into `~/.local/bin` and checks it against the SHA-256 in Kiro's release manifest. Rebuild
  with `archie build` to pick up a newer Kiro.
- An orchestrator as new as the CLI (restart `archie serve` after updating). An older
  orchestrator answers `POST /api/sessions` with 200 once it has started a session, perhaps
  a native one; `archie start` takes that 200 as the sign of an older orchestrator, stops the
  session it names and fails, asking you to restart `archie serve`.
- A Kiro API key at `kiro.api_key` in `credentials.yaml`. Store it with
  `KIRO_API_KEY=... archie auth login kiro` (see `plugins/kiro/README.md`). The agent passes the
  key only to the `kiro-cli` process, never into the agent environment, tool commands or logs.
- The `kiro:` section of `config.yaml` (see [config.md](config.md#kiro-backend)), which
  holds the model allowlist, the default model, effort and credit pricing.

## How a session starts

1. `archie start --kiro` posts `{"workspace": …, "backend": "kiro"}` to `POST /api/sessions`. The request is not a blocking start: it returns 202 with a `starting` session record, and the CLI polls `GET /api/sessions/{id}` behind a terminal-aware activity indicator until the status leaves `starting`; it does not stream raw Kiro stderr or ACP frames. A failed start ends as `failed`, and the CLI prints the record's `error`.
2. In the background, the orchestrator adds `-e ARCHIE_BACKEND=kiro` to `docker run` and waits up to 45 s for
   the agent's `/status`. Native sessions wait 30 s.
3. The agent's lifespan loads the config and runs plugin startup hooks. It skips the native
   model, compaction-model and LLM-client setup, so a bad `global.model` doesn't block a
   Kiro session. It then starts `KiroHarness` (`agent/src/archie_agent/kiro/harness.py`).
4. The harness completes these steps within a 30 s deadline:
   1. It sets `telemetry.enabled: false` in `~/.kiro/settings/cli.json`.
   2. It starts the Archie MCP server on `127.0.0.1:<free port>`.
   3. It writes `~/.kiro/agents/archie.json` and `archie-worker.json`, which point at that
      server. Their prompts go under `~/.kiro/archie/`, because Kiro parses every `.md` file
      in `agents/` as an agent.
   4. It spawns `kiro-cli acp --agent-engine=v3` in its own process group.
   5. It sends `initialize` and `session/new` with `_meta.kiro.modeId: "archie"`.
   6. It sets and verifies `contentCollection: disabled`, `autopilot: on`, the model, and
      `effortLevel` when `kiro.effort` is set and the model offers that level.
   7. It waits until the `archie` MCP server is `connected` and Archie tools are advertised.
5. The session log's `session_started.model_key` is `kiro/<default_model>`.

Startup fails closed. A Kiro agent config error, a mode other than `archie`, or an advertised
built-in tool other than `read` (the skill category) stops the session. So do a config value
Kiro doesn't confirm, a default model Kiro doesn't offer, an MCP server that doesn't connect,
Kiro exiting, or the deadline passing. The agent then writes the reason to
`~/.nexus/logs/kiro/<session-id>.startup-error` and exits. The orchestrator stops the
container, marks the session `failed` and appends that reason to the record's `error`, which
`archie start --kiro` prints. If a config error or a disallowed tool appears after startup,
the agent kills Kiro and ends the active turn with `turn_error`.

## Prompt, tools, skills and subagents

- **Prompt:** Archie's identity, soul, environment, tools and brain sections, plus
  `persona/prompts/kiro.md`. Kiro injects `AGENTS.md` and lists skills itself, so the Archie
  prompt leaves out `<agents.md>`, the skills catalog and the `task` guidance.
- **Tools:** Archie's `exec`, native tools and `question` (not `skill` or `task`) come from the in-process
  MCP server (`kiro/mcp_server.py`) with their usual schemas, live tool policy and plugin
  runner context. Kiro defers MCP tools: the model loads them with `tool_load` and calls them
  as `archie::<name>`. Results are capped at 29,000 characters, because Kiro replaces
  anything over 30,000 with a preview.
- **Skills:** the root agent lists skills from persona, `~/.agents/skills` and enabled
  plugins as `skill://` resources. Kiro loads them with its own skill tool.
- **Subagents:** one subagent, `archie-worker`, which has the default-agent prompt and every
  Archie tool except `question`: its agent file lists them as `@archie/<name>`, because
  both agents share one MCP server and questions are root-only, as on native sessions.

## Event mapping

`kiro/translate.py` turns ACP `session/update` notifications into canonical events. See
[event-spec.md](event-spec.md) for the event contract.

| Kiro | Canonical |
| --- | --- |
| `agent_message_chunk` | `text_delta`, then `assistant_message` per segment |
| `tool_call` / `tool_call_update` for `@archie/<name>` | `tool_call` / `tool_result` named `<name>` |
| skill load (`toolId` `disclose_context`) | `tool_call skill {"name"}` |
| subagent dispatch (`kind` `agent-subtask`) | `tool_call task {"tasks": [{"agent", "prompt"}]}`; child events scoped to its `toolCallId`, `subagent_index` 0 |
| `tool_load`, "Subagent Response" | dropped |
| any other tool | logged under its `toolId` or title, with a warning |
| `turn_completion` + prompt result | one root `llm_request`, then `turn_complete` / `turn_interrupted` / `turn_error` |
| `session_info_update` kind `context_usage` (root) | `session_status.context_pct` when the value changes; the turn's last, max and distinct count on its `llm_request` |
| `session_info_update` kinds `summarization_started` / `summarization_completed` / `summarization_failed` during a turn | `compaction_start` / `compaction_complete` (reason `threshold`; `success` with Kiro's summary, `failure`, or `interrupted` for `canceled` or a turn that ends first), scoped like the other events |

For current Kiro ACP frames, an Archie tool call uses `title: "@archie/<name>"` and places
its arguments directly in `rawInput`, for example `{"path": "foo.py"}`. The translator treats
that object as the canonical tool input; the older `{"tool_id": ..., "arguments": ...}` wrapper
is not supported.

Each text-then-tools segment is an `iteration` with its own `request_id`. The TUI therefore
streams text that follows a tool call. Kiro reports credits, not tokens, so each turn has one
`llm_request`. Its `credits` field holds the turn's credits, and `cost_usd` prices them per
`kiro.credits`. Its tokens are 0, and its `duration_ms` is Kiro's `elapsedTime`. Aborted
turns are still charged. The period's credits so far come from the session logs when the
harness starts. The TUI shows each turn's credits after its cost in the turn summary, and the
session's total after the cost in the status bar.

Kiro reports context usage as `usagePercentage` in `context_usage` frames, about once per
model request and in identical pairs. The harness keeps the latest root figure, puts it in
every `session_status` and sends a new status when it changes, so the TUI status bar shows
`Ctx: N%` live; Kiro gives no token count. The turn's `llm_request` stores the last figure
(`context_pct`), the highest (`context_pct_max`), the number of distinct figures
(`context_samples`) and `len(requestIds)` (`kiro_requests`), so replay, insights and
`archie stats` show it. Straight after an automatic compaction Kiro reports a placeholder of
exactly 20 % before the real figure; the agent drops those frames from both the live status
and the turn's figures. After the compaction Kiro carries on with one or two more model
requests in the same turn, so a short extra reply can follow the row. The turn's fields cover frames up to the `session/prompt` result; Kiro usually repeats the last figure just after it, and a changed figure there reaches the next `session_status` (and the next turn) only. Archie keeps one `llm_request` per turn: in the logs checked, the
figures matched Kiro's request IDs for only 39 % of requests, so the two counts are recorded
to re-check that later.

## Session control

- **Interrupt** sends `session/cancel` and kills every running Archie tool process group.
  Kiro has no per-subagent cancel, so stopping one subagent stops the turn.
- **Model switch** accepts `kiro/<id>` keys from `kiro.models` that Kiro also offers, and
  re-applies effort. Other keys get `error_notice unknown_model`. The TUI palette lists the
  allowlist for Kiro sessions, and the status bar shows `<id> (Kiro)`.
- **`/compact`** (typed, or Compact context in the TUI palette) calls Kiro's
  `_kiro/session/compact`. The log gets a `compaction_start` (reason `manual`) and a
  `compaction_complete` carrying Kiro's summary from its `summarization_completed` frame.
  Kiro reports no tokens, so both carry `tokens_before` 0 and `tokens_after` null, and the
  TUI and web viewer show `Compaction manual (Kiro, <ms>)`. Kiro answers `{success: false}`
  while a turn or another compaction runs, which ends as a `failure` completion; so do a
  JSON-RPC error and the 120 s timeout. Before the first turn the agent answers
  `manual_compaction_failed` "Nothing to compact" without calling Kiro. Kiro can't cancel a
  compaction, so an interrupt during one sends only an `error_notice`
  `compaction_not_cancellable`. Kiro sends no context figure after a manual compaction, so
  the status bar keeps the old percentage until the next turn.
- **`question`** blocks its MCP call until the user answers, like the native tool: the
  agent publishes the pending question in `session_status` (`state: blocked`) and `/status`,
  and routes `answer` commands to it. Its id is generated by the agent, because the MCP call
  never sees ACP's `toolCallId`. Kiro doesn't cancel the MCP call when a turn is cancelled
  or when it fails the tool call itself (for example after the MCP `requestTimeout`, 600 s),
  so the agent clears the question on interrupt, when Kiro ends the `question` tool call,
  and at the end of every turn; the abandoned call returns an `Error:` text that Kiro
  discards.
- **Permission requests** are answered `cancelled`, with a warning. The agent rules allow
  every Archie tool, skill and `archie-worker`, so Kiro shouldn't ask.
- **Kiro exits mid-session:** the harness restarts it (094). The active turn ends with a
  `turn_error` that names the exit code, and the out-of-memory killer with a count when the
  container's `oom_kill` counter rose during the turn. The harness kills the turn's Archie
  tool processes, relaunches `kiro-cli`, and reloads the conversation with ACP `session/load`,
  re-applying `contentCollection: disabled`, the model and effort. It then sends a live
  `error_notice` kind `backend_restarted`, and prefixes the next prompt with a note that the
  previous turn was cut off (the logged `user_message` stays as typed). It restarts at most 3
  times in 30 minutes. If a restart can't reload the session, or the limit is reached, the
  backend is dead: it reports `session_status` state `error`, sends `error_notice` kind
  `backend_stopped`, and later messages get a `turn_error` telling you to start a new session.
  A fail-closed stop (a config error or a disallowed built-in tool) is never restarted.
- **Out-of-memory resilience:** every process a tool starts (`shell`, `exec`, and the exec
  runner) raises its own `oom_score_adj` to 1000 before it runs, so when the VM or container
  runs low on memory the kernel kills a tool process before the agent or Kiro. A `shell`
  result or an exec `RunnerCrash` whose run overlapped an OOM kill carries an
  `[archie warning: ...]` line. See `agent/src/archie_agent/oom.py`.
- **Shutdown** kills Kiro's process group and stops the MCP server.

## Debugging

- `~/.nexus/logs/kiro/<session-id>.acp.jsonl` holds every ACP frame, both directions, with
  the API key redacted. `kiro-cli` stderr lines are stored as `err` entries, and each exit as
  an `exit` entry (`{"code": N, "oom_kills": K}`), so the reason Kiro stopped outlives the
  container.
- `~/.nexus/logs/kiro/<session-id>.startup-error` holds the reason a session failed to start.
- Kiro's stderr goes to the agent log. Routine `[INFO]`/`[DEBUG]` lines go to DEBUG, the
  Node SQLite `ExperimentalWarning` is dropped, and everything else goes to WARNING.

## Limits

Kiro sessions run locally only, with no remote Kiro sessions or cloud sandboxes. The backend
restarts and reloads its conversation after an unexpected `kiro-cli` exit (094), but a session
still ends when its container goes away, because Kiro's session store lives inside the
container. There are no per-request latency or token counts. Context is a
percentage only, for the main agent only (Kiro reports none for subagents), and turn cost
limits are not enforced. Insights and `archie stats` don't
show credits yet. The `tests/fake_kiro.py` scripted agent covers the ACP flow without a
network.

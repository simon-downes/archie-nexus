# Kiro backend

`archie start --kiro` runs a session on [Kiro CLI](https://kiro.dev/docs/cli/acp/) instead of
Archie's own agent loop. Kiro chooses and calls the model. Archie keeps the prompt, the tools,
the session log, the TUI and accounting. Without `--kiro`, sessions run the native loop
unchanged. The backend is chosen per session.

## Requirements

- The session image includes `kiro-cli`. The `Dockerfile` installs the latest stable build
  into `~/.local/bin` and checks it against the SHA-256 in Kiro's release manifest. Rebuild
  with `archie build` to pick up a newer Kiro.
- An orchestrator that supports `backend` (restart `archie serve` after updating). If an
  older orchestrator starts a native session instead, `archie start --kiro` stops it and
  fails.
- A Kiro API key at `kiro.api_key` in `credentials.yaml`. Store it with
  `KIRO_API_KEY=... archie auth login kiro` (see `plugins/kiro/README.md`). The agent passes the
  key only to the `kiro-cli` process, never into the agent environment, tool commands or logs.
- The `kiro:` section of `config.yaml` (see [config.md](config.md#kiro-backend)), which
  holds the model allowlist, the default model, effort and credit pricing.

## How a session starts

1. `archie start --kiro` posts `{"workspace": …, "backend": "kiro"}` to `POST /sessions`.
2. The orchestrator adds `-e ARCHIE_BACKEND=kiro` to `docker run` and waits up to 45 s for
   `/status`. Native sessions wait 30 s.
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
container and appends that reason to the error `archie start --kiro` prints. If a config
error or a disallowed tool appears after startup, the agent kills Kiro and ends the active
turn with `turn_error`.

## Prompt, tools, skills and subagents

- **Prompt:** Archie's identity, soul, environment, tools and brain sections, plus
  `persona/prompts/kiro.md`. Kiro injects `AGENTS.md` and lists skills itself, so the Archie
  prompt leaves out `<agents.md>`, the skills catalog and the `task` guidance.
- **Tools:** Archie's `exec` and native tools (not `skill` or `task`) come from the in-process
  MCP server (`kiro/mcp_server.py`) with their usual schemas, live tool policy and plugin
  runner context. Kiro defers MCP tools: the model loads them with `tool_load` and calls them
  as `archie::<name>`. Results are capped at 29,000 characters, because Kiro replaces
  anything over 30,000 with a preview.
- **Skills:** the root agent lists skills from persona, `~/.agents/skills` and enabled
  plugins as `skill://` resources. Kiro loads them with its own skill tool.
- **Subagents:** one subagent, `archie-worker`, which has Archie's tools and the default-agent
  prompt.

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

Each text-then-tools segment is an `iteration` with its own `request_id`. The TUI therefore
streams text that follows a tool call. Kiro reports credits, not tokens, so each turn has one
`llm_request`. Its `credits` field holds the turn's credits, and `cost_usd` prices them per
`kiro.credits`. Its tokens are 0, and its `duration_ms` is Kiro's `elapsedTime`. Aborted
turns are still charged. The period's credits so far come from the session logs when the
harness starts.

## Session control

- **Interrupt** sends `session/cancel` and kills every running Archie tool process group.
  Kiro has no per-subagent cancel, so stopping one subagent stops the turn.
- **Model switch** accepts `kiro/<id>` keys from `kiro.models` that Kiro also offers, and
  re-applies effort. Other keys get `error_notice unknown_model`. The TUI palette lists the
  allowlist for Kiro sessions, and the status bar shows `<id> (Kiro)`.
- **`/compact`** is not available. Kiro compacts automatically at 80 % of the context, and
  the agent answers with `error_notice manual_compaction_failed`.
- **Permission requests** are answered `cancelled`, with a warning. The agent rules allow
  every Archie tool, skill and `archie-worker`, so Kiro shouldn't ask.
- **Kiro exits mid-session:** the active turn ends with `turn_error` "Kiro backend stopped".
  Later messages get a live `error_notice` saying to start a new session. Kiro sessions are
  not resumed.
- **Shutdown** kills Kiro's process group and stops the MCP server.

## Debugging

- `~/.nexus/logs/kiro/<session-id>.acp.jsonl` holds every ACP frame, both directions, with
  the API key redacted.
- `~/.nexus/logs/kiro/<session-id>.startup-error` holds the reason a session failed to start.
- Kiro's stderr goes to the agent log. Routine `[INFO]`/`[DEBUG]` lines go to DEBUG, the
  Node SQLite `ExperimentalWarning` is dropped, and everything else goes to WARNING.

## Limits

Kiro sessions run locally only, with no remote Kiro sessions or cloud sandboxes. The backend
has no resume, no manual compaction and no per-request latency or token counts. It shows no
context percentage, and turn cost limits are not enforced. Insights and `archie stats` don't
show credits yet. The `tests/fake_kiro.py` scripted agent covers the ACP flow without a
network.

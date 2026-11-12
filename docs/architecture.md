# archie-nexus architecture

This document describes the implementation currently in the repository. Code and tests are authoritative when this document and the implementation disagree. Update this file when a component boundary, route, mount, event schema, persisted format, or accounting rule changes.

## Contents

- [System shape](#system-shape)
- [Runtime topology and lifecycle](#runtime-topology-and-lifecycle)
- [Host HTTP API](#host-http-api)
- [Turn and data flow](#turn-and-data-flow)
- [Event layers](#event-layers)
- [Event specification](event-spec.md) — canonical event catalog, delivery class, and replay rules
- [Logs, accounting, and metrics](#logs-accounting-and-metrics)
- [Configuration, security, and ownership](#configuration-security-and-ownership)
- [Change impact guide](#change-impact-guide)

## System shape

Nexus has four Python workspace packages:

- **CLI (`cli/`)** — host-side `archie` command, Textual TUI, authentication commands, and clients for the orchestrator.
- **Orchestrator (`orchestrator/`)** — host-side Starlette control plane. It starts/stops Docker containers, keeps a record of every session, proxies session HTTP/WebSocket traffic, serves the session page, log viewer and usage insights pages, and keeps the metrics index in step with the session logs.
- **Agent (`agent/`)** — container-side Starlette application. It owns one session, the agent loop, model client, tools, skills, subagents, and session event log.
- **Shared (`shared/`)** — cross-boundary types and contracts: configuration, model catalog, credentials, client commands, public events, session-log storage, accounting, and the transcript model (`archie_shared.transcript`) that both the TUI and the web viewer render from.

`persona/` is content rather than a package: prompts, agents, and procedure skills are mounted into the container and loaded by the agent.

`plugins/` holds one directory per integration (Datadog, incident.io, Jira, Notion, Slack, Google, AWS, GitHub, Scalr, Cloudflare) with its `plugin.yaml`, code, skills, and tests. The host reads manifests through `shared/src/archie_shared/plugins/` for credentials and config validation; inside the container the exec runner imports plugin code as `archie_plugins.<id>` and exposes its tools only inside `exec`, under the plugin's namespaces. Core tools (`exec/tools/`) are flat and native. See [Plugins](plugins.md).

## Runtime topology and lifecycle

```text
archie CLI / TUI
       │ HTTP + WebSocket
       ▼
host orchestrator :7600
       │ Docker API / localhost port mapping
       ▼
agent container :8080
       │ model provider, tools, workspace
       ▼
AWS Bedrock / OpenAI API / Ollama / local workspace
```

1. `archie build` builds `archie:latest` from `Dockerfile`.
2. `archie serve` runs the host orchestrator; the default profile is `127.0.0.1:7600`.
3. `archie start <workspace>` asks `POST /api/sessions` for a workspace directory below `global.workspace_root` (default `~/dev`). `archie start --kiro` adds `"backend": "kiro"`, which runs the session on Kiro CLI (see [kiro.md](kiro.md)). The request returns 202 with a `starting` session record; the CLI then polls `GET /api/sessions/{id}` every 0.5 s, up to 60 s, behind a terminal-aware activity status until the status leaves `starting`. A `failed` start prints the stored `error`, another ended status prints `Session ended during startup (<status>)`, and a timeout leaves the session running; all three exit 1.
4. Before responding, the orchestrator validates the single directory name and the image, creates a session ID and inserts the `starting` record, so bad input (400) and a missing image (500) are still reported synchronously. In the background it runs a container, publishes container port 8080 on an ephemeral localhost port, and waits for the agent's `/status` to succeed (up to 30 seconds; 45 for Kiro sessions, which also get `ARCHIE_BACKEND=kiro`). The session tracker then follows the agent's `/stream`, and the agent's first `session_status` makes the record `idle`. A start that fails or times out makes it `failed`, with the message in `error`.
5. The CLI connects through the orchestrator’s session routes. The orchestrator looks up the session's agent port in the tracker's memory, without calling Docker, and proxies to it.
6. `archie stop` calls `DELETE /api/sessions/{session_id}`. The record becomes `stopping`, the container is stopped by name, and the response is the `stopped` record. The attached TUI closes its receive/reconnect tasks and WebSocket before requesting deletion; backend disconnects during this ordered teardown are handled as expected transport loss. Containers use `--rm`; stopping/removing a container does not remove the host session JSONL log or the session record, except that a session that never received a prompt leaves neither. A container that goes away without a `DELETE` makes the record `exited`. After an orchestrator restart, the tracker reconciles the stored records with the running containers (see [Session records and the tracker](#session-records-and-the-tracker)).

### Session records and the tracker

The orchestrator keeps one record per session in the `sessions` table of `archie.db` (see [Orchestrator datastore](#orchestrator-datastore)). On the wire it is `SessionDescriptor` (`shared/src/archie_shared/session/descriptor.py`), served by every `/api/sessions` route and the feed:

| Field | Meaning |
| --- | --- |
| `session_id`, `workspace` | the ID (`{workspace}-{ulid_prefix}`) and its workspace directory name |
| `backend` | `native` or `kiro` |
| `name`, `name_source` | display name and where it came from: `user` (set with `POST`/`PATCH`; never overwritten), `model` (a title from the title model, see [Session titles](#session-titles)) or `id` (the session ID) |
| `status` | one of the statuses below |
| `error` | the start failure message of a `failed` session |
| `question` | the pending question while `blocked` |
| `unseen` | set when a root turn completes or fails; cleared by a client with `PATCH` |
| `model_key` | the model the agent last reported; null until the first `session_status` |
| `started_at`, `last_activity_at`, `ended_at` | ISO 8601 UTC times; `ended_at` is set with an ended status |
| `container_name` | `archie-{session_id}`, or null once the session has ended; computed, not stored |

`status` merges the orchestrator's lifecycle with the agent's turn state: `starting` (container starting), `idle`, `working`, `blocked` (waiting on a `question`), `error` (the last root turn failed; cleared by the next root message), `stopping`, and the ended statuses `stopped` (by `DELETE`), `exited` (the container went away by itself) and `failed` (the start failed). An ended record changes only through `PATCH` (`name`, `unseen`).

`SessionTracker` (`orchestrator/src/archie_orchestrator/tracker.py`) owns every write to `sessions`. It keeps every record that hasn't ended in memory and is authoritative for them; reads of ended records go to the store. Container operations go through a small `Runtime` interface (`DockerRuntime` in production, a fake in tests). For each running session the tracker holds one read-only WebSocket to the agent's `/stream`, as the live viewer does, and folds the events into the record:

- A root `user_message` sets `working`; `session_status` sets `idle`, `working` or `blocked`, the model and the question, and a `kiro/` model sets `backend` to `kiro`.
- A root `turn_complete` or `turn_error` sets `unseen`. After a root `turn_error` the next `idle` is stored as `error`; a root `turn_interrupted` drops that pending error.
- Other events only move `last_activity_at`. A change to `last_activity_at` alone is written and published at most every 10 s per session, with a trailing write at the end of the window; every other change is written and published at once.
- When the stream closes, the tracker asks Docker whether the container still runs: gone makes the record `exited`; running reconnects after 1, 2, then 5 s. A `stopping` record is left to the stop.

A failed write is logged and retried with the next change; a record leaves memory only once its ended state is written (retried every 5 s). On startup the orchestrator migrates `archie.db`, loads the live records and reconciles them with one `docker ps`: a running container makes its record `idle` again (a container with no record is adopted, from its log if there is one), and a record with no container ends as `stopped` (was `stopping`), `failed` (was `starting`, error `orchestrator restarted during startup`) or `exited`, with `ended_at` the log's mtime (or now). If `docker ps` fails, nothing is ended and reconcile is retried every 30 s. Reconcile also starts following each running session; the metrics loop starts after this first attempt. On shutdown the tracker closes its streams and writes pending activity without changing any status, since sessions keep running without the orchestrator.

No read or proxy path calls Docker: the session list and record routes, the session pages, and the port lookups for `/events`, `/stream` and the live viewer all read the tracker. The tracker also publishes every change to the feed's subscribers, each with a 256-record queue; a subscriber that falls behind is dropped and reconnects to a fresh snapshot.

### Session titles

Sessions the user hasn't named are titled by `orchestrator.title_model` (default `bedrock-openai-gpt-5-6-luna`; `null` turns titles off), so clients can tell them apart without asking for a name. The tracker requests a title on the root `user_message` of turn 1, at the end of root turns 1, 3, 8 and 15 and every 10th turn after that, and at every root turn end while `name_source` is still `id`. Opening a session that still has only its ID also requests one: `GET /api/sessions/{id}/events` without `after` (how the TUI loads a session) and the web transcript `/sessions/{id}`, for live and ended sessions alike, at most once per 5 minutes after a failed call. Sessions nobody opens are never backfilled. `PATCH name: null` requests a title straight away. A session whose `name_source` is `user` is never titled.

A title job reads the session log (root prompts and each turn's last uninterrupted reply only, never tool calls or results) and sends one request with no tools or reasoning (`orchestrator/src/archie_orchestrator/titles.py`). The request carries the workspace, the current model title (when there is one), the first prompt and reply, the last three prompts and the latest reply, each cut to 400–600 characters. The model answers with a title of at most 48 characters or `KEEP`. The reply is cleaned (first line, no quotes or trailing punctuation, no control characters, no refusals, cut on a word boundary) and written as `name` with `name_source: model` unless the user renamed the session during the call; the change is published on the feed. A failure, a rejected reply or `KEEP` leave the record as it was, so a session keeps its ID until the first title succeeds and its last title after that. One job runs per session at a time (later requests coalesce into one rerun), and at most two calls run at once, each with a 15 s timeout and one retry.

Each call writes one orchestrator log line with the session ID, model, input and output tokens, cost in USD and `ok`, `keep` or the error; no conversation content is logged, and the calls are not counted in insights. The orchestrator calls the model itself (not through the agent's LLM clients) with the `bedrock` credential, or the `openai` key for `openai-*` models.

### Container mounts

The complete configuration reference, including additional host bind mounts and wildcard
examples, is in [Configuration](config.md). The lifecycle code mounts the following fixed
paths; configured mounts are appended separately.

| Host content | Container path | Purpose |
| --- | --- | --- |
| `agent/` | `/opt/archie/agent` | live agent source |
| `shared/` | `/opt/archie/shared` | shared source (read-only) |
| `persona/` | `/opt/archie/persona` | prompts, agents, procedure skills |
| `plugins/` | `/opt/archie/plugins` | integration manifests, code, skills (read-only; `ARCHIE_PLUGINS_DIR`) |
| `container/bin/gh` | `/usr/local/bin/gh` | `gh` wrapper (read-only) |
| selected workspace | `/workspace` | project under development |
| `ARCHIE_HOME_DIR` (normally `~/.nexus`) | `/home/archie/.nexus` | config, credentials, sessions, brain |
| `~/.agents` | `/home/archie/.agents` | agent-related user content |

The image contains dependencies and system tools. `gh` on `PATH` is a repository wrapper (`container/bin/gh`, bind-mounted over the copy in the image) around the real binary, `gh-base`; it selects a GitHub token per call from the credential store, so GitHub tokens are not part of the container environment (see [Authentication and credentials](auth-credentials.md#github-cli-token-selection)). Other mapped credentials and plugin settings are not passed to `docker run` either: the agent adds them to each shell command and exec run from the current store and `config.yaml` (see [Command environment projection](auth-credentials.md#command-environment-projection)). The runtime user is `archie`, with its UID matched to the host user during `archie build`. The home and persona mounts are read-write: a session can modify shared user data and repository-tracked persona content. Treat those mounts as a deliberate trust boundary, not as isolated per-container state.

The system prompt's Environment section is the plain-markdown `persona/prompts/environment.md`, with installed tools and paths written out and a few probed values filled in by `agent/src/archie_agent/environment.py`. See [System prompt](system-prompt.md) for the prompt's structure and how to add a tool.

## Host HTTP API

The orchestrator's route table has two parts. JSON routes (and the session WebSocket relay) live under `/api` (`API_PREFIX` in `archie_shared.protocol`), defined in `orchestrator/src/archie_orchestrator/api.py`; their callers are code: the CLI, the TUI and agents. Pages live at the root, defined in `orchestrator/src/archie_orchestrator/pages.py`; their callers are browsers. API handlers never render templates and page handlers never return JSON. An htmx fragment is served by its page: the page URL returns the fragment when `HX-Request` is set, and a fragment with no page of its own is a sub-path of its page (`/sessions/{id}/live`). The root also serves `/health` and `/static/*`.

Pages:

| Method | Route | Role |
| --- | --- | --- |
| `GET` | `/` | running sessions web page (status, name when set, last activity) |
| `GET` | `/sessions?q=<query>` | session log viewer: persisted logs, newest first, with search (`HX-Request` gets the results fragment) |
| `GET` | `/sessions/{id}` | session log viewer: transcript of one log; titles a session that has only its ID ([Session titles](#session-titles)) |
| `GET` | `/sessions/{id}/live?after=<event id>` | server-sent HTML fragments relayed read-only from the agent `/stream` |
| `GET` | `/insights` | usage insights overview; filters `since`, `from`, `until`, `workspace`, `model`, `scope` |
| `GET` | `/insights/sessions?sort=<key>` | insights: sortable session table |
| `GET` | `/insights/sessions/{id}` | insights: one session's turns, requests, tools and subagents |
| `GET` | `/insights/models` | insights: usage, latency and errors per model |
| `GET` | `/insights/tools` | insights: calls, errors, duration and result size per tool |
| `GET` | `/insights/subagents` | insights: subagent types and skill loads |
| `GET` | `/insights/errors` | insights: request, turn and subagent errors |
| `GET` | `/auth` | credential status web page |
| `GET` | `/auth/callback/{provider}` | complete browser OAuth callback (registered as the provider redirect URI) |

JSON API:

| Method | Route | Role |
| --- | --- | --- |
| `GET` | `/api/sessions?include=ended` | session records, newest first; only sessions that haven't ended unless `include=ended` |
| `POST` | `/api/sessions` | start a session; body `{"workspace":"name"}`, optional `"backend": "native" \| "kiro"` and `"name"` (non-blank, at most 80 characters, no control characters, stored trimmed); 202 with the `starting` record |
| `GET` | `/api/sessions/feed?include=ended` | server-sent events: a `snapshot` event (`{"sessions": [...]}`, filtered like the list), then one `session` event with the full record per change, whatever the filter; `: ping` after 15 s idle |
| `GET` | `/api/sessions/{id}` | one session record, live or ended |
| `PATCH` | `/api/sessions/{id}` | `name` (a string, validated as for `POST`, sets it with `name_source: user`; `null` resets it to the ID with `name_source: id`) and/or `unseen: false`; allowed on ended sessions; any other body is 400 |
| `DELETE` | `/api/sessions/{id}` | stop a running session and wait; 200 with the `stopped` record, 409 while `starting` or `stopping` or once ended, 500 if the container is still running (previous status restored) |
| `GET` | `/api/sessions/{id}/events?after=<id>` | session history read: proxied to the agent while it runs, read from the session log once the session has ended (an ended session with no log returns an empty body); 503 while `starting`. Without `after`, also titles a session that has only its ID ([Session titles](#session-titles)) |
| `GET` | `/api/sessions/{id}/metrics` | metrics for one session |
| WebSocket | `/api/sessions/{id}/stream` | bidirectional client/session stream |
| `GET` | `/api/metrics` | aggregate metrics; optional `since=<ISO-8601>` |
| `GET` | `/api/workspaces` | every workspace under `global.workspace_root` with its git repos, sorted by name (see [Workspaces](#workspaces)) |
| `GET` | `/api/workspaces/{name}` | one workspace; 404 for a name that is missing, hidden or resolves outside `workspace_root` |
| `GET` | `/api/auth/providers` | typed, secret-free provider definitions |
| `GET` | `/api/auth/status` | redacted credential status |
| `PUT` | `/api/auth/credential/{provider}` | replace one static provider credential |
| `DELETE` | `/api/auth/credential/{provider}` | remove one provider credential |
| `POST` | `/api/auth/login/{provider}` | start orchestrator-owned OAuth |
| `GET` | `/api/auth/flow/{flow_id}` | query redacted OAuth flow status |
| `POST` | `/api/auth/refresh/{provider}` | refresh OAuth credentials |

Other root routes:

| Method | Route | Role |
| --- | --- | --- |
| `GET` | `/health` | orchestrator liveness (`{"status":"ok"}`) |
| `GET` | `/static/*` | static assets for the web pages (vendored nimble.css, htmx and Chart.js under `/static/vendor/`) |

The agent service itself listens on port 8080 and provides `/status`, `/events`, and `/stream`. The orchestrator’s session routes are the normal client-facing interface; it does not proxy the agent's `/status`, since the session record carries the status, model and pending question. Ended-session history from `/api/sessions/{id}/events` has the agent's format: the same NDJSON lines and the same 409 `{"error": "cursor_not_found", "cursor": ...}` for an unknown cursor.

The session log viewer reads `<ARCHIE_HOME_DIR>/sessions/*.jsonl` directly, so stopped sessions remain browsable. Usage totals come from `llm_request` events. For an active session, `/sessions/{id}/live` opens the agent `/stream` WebSocket and never sends on it. The insights pages read only `archie.db` (the metrics tables joined to the session records); each request first syncs it without waiting (if another sync holds the lock, the page renders from the current index with an "Index is updating" notice). The viewer and insights pages send a restrictive `Content-Security-Policy`; like the other web pages, they have no authentication and assume the trusted local deployment model.

### Workspaces

`/api/workspaces` describes the directories directly under `global.workspace_root`, as `Workspace` (`shared/src/archie_shared/workspaces.py`): `name`, `live_sessions` (from the tracker's memory), `last_session_at` (the newest `started_at` over the stored and in-memory session records) and `repos`. A workspace's repos are the workspace directory itself and its direct children that hold a git repository; hidden directories, symlinked children and deeper directories are not scanned. Each repo has `path` (relative to the workspace, `""` for the workspace itself), `origin`, `branch` (null when detached), `commit` and `worktrees` (`path`, `branch`, `commit`, `missing`). A linked worktree is listed under its repo, never as a repo of its own. At most 100 repos per workspace and 100 worktrees per repo are listed.

Everything is read from the filesystem on each request and nothing is stored; one workspace with four repos takes about 7 ms. The orchestrator never runs `git` here: sessions can write a repo's `.git/config`, and git runs commands named there (`core.fsmonitor`, filters) even for `git status`, so running git on the host would let a session execute host commands. `orchestrator/src/archie_orchestrator/workspaces.py` instead reads `HEAD`, loose refs, the first 8 MiB of `packed-refs`, the `origin` URL from the repo's own config (no includes or `insteadOf`; credentials, query and fragment are removed: all userinfo for HTTP(S), the password otherwise) and the worktree records. Those files are untrusted too: reads are confined to the workspace directory after resolving symlinks, accept only regular files and are size-capped. A file that can't be read or parsed gives null fields, or leaves out the repo or worktree it describes; it never fails the request. Paths git recorded inside a container (`/workspace/...`) are mapped to the host workspace; a worktree outside the workspace keeps its absolute host path. Repositories using the reftable ref format get a null `branch` and `commit`.

### Authentication API security

The orchestrator owns provider-scoped static credential updates and OAuth callbacks while retaining the shared `<ARCHIE_HOME_DIR>/credentials.yaml` store. Responses and logs are redacted; the store uses atomic writes and restrictive permissions. These routes have no client authentication and remain intended only for the trusted local deployment model. `bedrock` and `aws` are separate provider entries. See [Authentication and credentials](auth-credentials.md) for the complete provider, storage, CLI, OAuth, and agent-access contract.
## Turn and data flow

A user message follows this path:

1. The TUI sends a `message` command over the session WebSocket.
2. The orchestrator relays it to the agent without interpreting the command.
3. The agent publishes canonical events through the session coordinator. The coordinator orders
   them, persists persisted events before delivery, and broadcasts them to connected clients.
4. LLM provider streams become canonical text, request, tool, and terminal events. The catalog
   provider picks the client: `bedrock` (Converse), `bedrock-openai` (Responses on
   bedrock-runtime), `openai` (Responses on `https://api.openai.com/v1`, `openai-*` keys,
   authenticated with the `openai` API key credential; these requests leave AWS), or `ollama`. Tool calls may
   invoke native tools, the exec runner, or child agents through the `task` tool.
   Native `read` returns image files (PNG/JPEG/GIF/WebP) as image content, downscaled with
   Pillow to at most 1568px on the long edge and ~1.2MP. Images ride on `ToolResultBlock.images`
   in memory only; provider adapters send them when the model's catalog entry has `can_see`
   (Converse `image` blocks; Responses `input_image` data URLs) and otherwise substitute a fixed
   "image omitted" note. Ollama is always text-only. Inside exec, `read` returns the caption text.
5. The orchestrator's metrics sync (every 15 s, plus on each insights page and `archie stats`
   run) tails the session logs into the metrics tables of `archie.db`. It never touches the relay.
6. The TUI applies live and stored events through one `_apply_event(event)` path. On connection or reconnect it requests `/events?after=<last-applied-persisted-id>`, then reconciles buffered live events by event ID and exact assistant request identity. Behind that path, the shared `TranscriptBuilder` (`archie_shared.transcript`), which the web viewer also uses, reduces events into the transcript model; see [How the TUI applies the events](session-protocol-walkthrough.md#10-how-the-tui-applies-the-events).

On a Kiro session (`ARCHIE_BACKEND=kiro`), step 4 runs in `KiroHarness` instead: Kiro CLI calls
the model over ACP, Archie's tools run in an in-process MCP server, and the ACP stream is
translated into the same canonical events. See [kiro.md](kiro.md).

The orchestrator WebSocket proxy is intended to be transparent: it forwards client text to the agent and agent text/binary frames back to the client without inspecting them.

## Event layers

The public event union is the only server-to-client event contract for both live delivery and
history reads. The complete catalog, field types, persisted/live-only classification, identity rules,
and migration contract are in [Event specification](event-spec.md).

Public events are flat tagged `msgspec` records defined in
`shared/src/archie_shared/events.py`. Persisted records are newline-delimited JSON in:

```text
<ARCHIE_HOME_DIR>/sessions/<session-id>.jsonl
```

The persisted stream includes `session_started`, `user_message`, `iteration_start`, `llm_request`,
`context_compacted`, `tool_call`, `tool_result`, `assistant_message`, `turn_complete`, `turn_error`,
and `turn_interrupted`. The live-only set is exactly `handshake`, `session_status`, `text_delta`, and
`error_notice`.

Root turns may compact at iteration boundaries before a provider request. Manual `/compact` performs
one dedicated no-tools summary request and replaces the in-memory root transcript with one compacted
user turn; automatic threshold and cold-cache compactions use the same replacement. After the
summary, the replacement turn has a block that names the session log path and explains how to
search it (event types, `turn`, `content`, `input`, `tool_use_id`), so the agent can recover details
the summary dropped. The session log remains append-only: `compaction_start` is persisted before the
compaction `llm_request`, followed by `compaction_complete`. New compactions emit only this pair;
clients still decode and render historical `context_compacted` records. The completion carries the
status, error or summary, token estimates, display cost, duration, and `skills_loaded` (the skills
loaded when compaction started). Compaction failures are reported through the failed completion,
not a separate `error_notice`. Compaction
requests use `purpose=compaction`, are priced at the configured compaction model, and contribute to
session totals without changing the active conversation context display.

`GET /events` returns ordered persisted NDJSON. Its optional `after` parameter is the client's last
applied persisted event ID; live-only frames never advance that cursor. The TUI applies history
and live frames through `_apply_event(event)`; both paths deduplicate by event ID,
and the shared `TranscriptBuilder` reconciles streamed text by request identity.

A connect sends one live-only `handshake` containing protocol and session identity, followed by
one `session_status` frame carrying the current model, Git branch, session state and any pending
question. There is no
`session_snapshot` or `session_info` envelope and no parallel wire event schema.
`PROTOCOL_VERSION` is 4; clients and agents use this final contract together.

The coordinator persists a persisted event before enqueueing it for broadcast. Live-only events are
never appended. `text_delta` is intentionally excluded from history reads; assistant content is
rebuilt from persisted `assistant_message` events, with partial text retained before an
interruption or error. `llm_request` contains provider usage and immutable cost, so it is the sole
accounting source for clients and the orchestrator metrics index.

## Logs, accounting, and metrics

### Canonical log rules

- `SessionLog.append()` validates canonical events and rejects conflicting duplicate IDs; identical duplicates are idempotent.
- `SessionLog.read()` preserves append order and skips malformed or unrecognised records with a warning.
- The user message is written before provider streaming; available assistant text is written before the matching terminal event, including partial text on interruption or error.
- Legacy `MessageEntry` structures remain only in `session.migrate` for the one-shot host migration; direct `!` shell output is rendered only by the initiating TUI.

Each `llm_request` contains the model key, status, sent time, duration, token categories (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`), context token count, immutable `cost_usd`, and `purpose` (`conversation` or `compaction`). Session totals are recomputed from these events on disk, so a newly attached client does not depend on the agent process’s in-memory state. Model changes are live-only status updates and do not reprice historical requests.

### Subagent scopes

Root events use `scope=None`. A child’s scope is the launching `task` tool call’s `tool_use_id`; `subagent_index` identifies the child’s display slot. The parent of a scope is found through the launching tool call. `scope_direct_costs` sums requests in one scope; `scope_inclusive_costs` adds descendants with cycle protection. Request identity is `(scope, subagent_index, turn, iteration, request_id)`.

### Orchestrator operational logs

The host orchestrator writes coloured terminal output plus two files under `<ARCHIE_HOME_DIR>/logs/`:

- `orchestrator.error.log` contains non-access application and Uvicorn logs.
- `orchestrator.access.log` contains HTTP access logs from `uvicorn.access`.

At startup, an existing file is moved to the matching `.1` path (one backup generation). Log level and service/logger name are colourised in terminal and file output. `LOG_LEVEL` controls the minimum level and defaults to `INFO`.

### Orchestrator datastore

`<ARCHIE_HOME_DIR>/archie.db` (`orchestrator/src/archie_orchestrator/store.py`) is the orchestrator's datastore. It holds canonical tables, which are never rebuilt, and the rebuildable metrics tables described below. The canonical tables are:

- `sessions`: one row per session with the `SessionDescriptor` fields except the computed `container_name` (`unseen` as 0/1, `question` as JSON; schema version 2 replaced `name_fixed` with `name_source`), indexed on `status` and `started_at`. A row stops changing once its session ends, apart from `PATCH`, so it is also the historical record. Only the session tracker writes it.
- `meta`: key/value rows; `metrics_version` holds the metrics schema version.

The canonical schema is versioned by `PRAGMA user_version` and changed only by the ordered migrations in `store.MIGRATIONS`, each in its own transaction under an exclusive `flock` on `archie.db.lock`. A database newer than the code is refused. Migration 1 creates both tables and imports every log already in `<ARCHIE_HOME_DIR>/sessions` once, as a `stopped` row whose `name` is the session ID: `started_at` is the `session_started` time, else the ID's ULID time, else the log's mtime; `last_activity_at` and `ended_at` are the log's mtime; `model_key` is the last root request's model, else the `session_started` model; `backend` is `kiro` for a `kiro/` model. Whichever process opens the database first (the orchestrator, `archie stats` or `archie migrate-sessions`) runs it. After that, a session gets its row from the tracker when it is created or when reconcile adopts its running container.

Access is synchronous `sqlite3` with one short-lived connection per call (WAL, 5 s busy timeout); the orchestrator calls it through `asyncio.to_thread`, and the API reads live records from the tracker's memory, so requests don't wait on the SQLite lock.

The old `<ARCHIE_HOME_DIR>/metrics.db` (with its `-wal`, `-shm`, `.lock` and `.legacy.*` files) is no longer read or written, and can be deleted.

### Orchestrator metrics

The metrics index is the `metrics_*` tables of `archie.db`: a derived, metadata-only index of every log in `<ARCHIE_HOME_DIR>/sessions`. `MetricsIndex` (`orchestrator/src/archie_orchestrator/metrics.py`) reads each log from a byte offset stored in `metrics_ingest_files` and folds the canonical events into tables:

- `metrics_requests`: one row per `llm_request`, including `status`, `stop_reason`, `error`, `subagent_index` and `purpose`. Recorded cost is never repriced.
- `metrics_turns`: root turns with status (`open`, `completed`, `error`, `interrupted`) and first/last request times.
- `metrics_tool_calls`: name, skill name for `skill` calls, request time and model, error flag, duration (`0` stored as NULL), result size, and the first line of an error result (≤200 characters).
- `metrics_compactions`: reason, status, tokens before and after, saving and duration.
- `metrics_subagents`: one row per `task` entry with its agent name, parent scope, and outcome from the child's terminal turn event.
- `metrics_ingest_files`: offset, size, mtime, first-line hash, invalid-line count and last error per log.

The tables join to `sessions` on `session_id` (no foreign key) for workspace, start time, last activity and name; the metrics sync never writes `sessions`. A log whose session has no `sessions` row is skipped and recorded in `metrics_ingest_files` with the error `no session record`; it is checked again on every sync and is not an error in strict mode. Insights and `archie stats` list only sessions whose log was indexed without error, and show a session's name unless it is only the ID (`name_source` is `user` or `model`).

No other message content, tool input or tool output is stored. Sync is incremental and idempotent: unchanged files are skipped by size and mtime, a trailing partial line waits for the next sync, and a log that shrank or whose first line changed is re-ingested from the start. Rows for deleted logs are kept. Invalid lines are counted and skipped; an unreadable file records its error and the sync moves on.

The orchestrator syncs every 15 seconds; `/insights` pages and `archie stats` sync before they query. Each sync and rebuild first runs any pending canonical migrations, then holds the exclusive `flock` on `archie.db.lock`. The metrics schema version is the `meta` row `metrics_version` (currently 5); when it is missing or different, every `metrics_*` table is dropped and recreated, with the new version row, in one transaction and refilled from the logs, and the canonical tables are left alone. `archie migrate-sessions` rebuilds the metrics tables the same way from the migrated logs in strict mode, so a file or SQLite error fails the command; `--db` chooses the database (default `<ARCHIE_HOME_DIR>/archie.db`). Queries for both surfaces live in `insights.py`. The index is a view of the logs, not a replacement for the session JSONL source of truth.

## Configuration, security, and ownership

`shared/config.py` resolves `ARCHIE_HOME_DIR` (default `~/.nexus`), `ARCHIE_PERSONA_DIR` (host repository `persona`, container `/opt/archie/persona`), and `ARCHIE_PLUGINS_DIR` (host repository `plugins`, container `/opt/archie/plugins`). `ARCHIE_BRAIN_DIR` defaults to `<ARCHIE_HOME_DIR>/brain`; a relative override is resolved beneath the home directory, while an absolute override is used as-is. An absolute brain override must also be available inside the container through the runtime environment/mount configuration. `ARCHIE_HOST` is a CLI-only `host[:port]` override for targeting an orchestrator and defaults to port 7600 when no port is supplied.

`shared/schemas.py` loads `config.yaml` with defaults:

```yaml
global:
  model: bedrock-openai-gpt-5-6-luna
  region: eu-west-1
  workspace_root: ~/dev
agent:
  subagents:
    max_concurrent: 5
orchestrator:
  profiles:
    default:
      host: 127.0.0.1
      port: 7600
plugins: {}
```

`plugins.<id>` configures each plugin and is validated against its manifest; see [Configuration](config.md#plugins).

Use `profile/value` CLI arguments to select a named orchestrator profile for workspace or session commands. `archie serve` binds to the configured default profile. Profiles with non-loopback hosts and these unauthenticated auth endpoints require private-network controls.

The agent owns the session process, provider requests, tool execution, and canonical log. The orchestrator owns Docker/session lifecycle, the session records and the metrics index. The CLI owns presentation and user commands; it must consume server-provided accounting rather than reconstructing prices. Containers receive read-write access to the selected workspace, the configured home directory, and persona content, so agent code and tools can modify those host-backed locations.

## Change impact guide

- **Changing `shared/events.py`** affects persisted logs, replay, agent writes, orchestrator metrics, and clients. Add compatibility tests, update [event-spec.md](event-spec.md), and update this document.
- **Changing `shared/commands.py`** affects client commands, agent WebSocket dispatch, and protocol versioning.
- **Changing mounts, ports, or lifecycle** affects `Dockerfile`, orchestrator lifecycle, readiness checks, security assumptions, and local setup instructions.
- **Changing the `sessions` table or `SessionDescriptor`** affects the API, the feed, the CLI and existing databases: add a migration to `store.MIGRATIONS` (never edit an applied one) and update this document. Changing a `metrics_*` table needs a `METRICS_VERSION` bump, which rebuilds the metrics tables from the logs.
- **Changing accounting** requires tests for model switches, interruptions/errors, duplicate ingestion, and nested scopes.
- **Changing `persona/`** changes runtime prompts or capabilities without changing package APIs; review the resulting agent behavior and relevant skill/agent metadata.
- **Changing `plugins/<id>/`** changes one integration; `plugin.yaml` changes also affect host-side credential providers and `config.yaml` validation. Follow [Plugins](plugins.md) and run the contract tests in `tests/test_plugin_layout.py`.
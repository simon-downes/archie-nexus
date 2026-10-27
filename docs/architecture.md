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
- **Orchestrator (`orchestrator/`)** — host-side Starlette control plane. It starts/stops Docker containers, discovers sessions, proxies session HTTP/WebSocket traffic, serves the session page, and records aggregate LLM request metrics.
- **Agent (`agent/`)** — container-side Starlette application. It owns one session, the agent loop, model client, tools, skills, subagents, and session event log.
- **Shared (`shared/`)** — cross-boundary types and contracts: configuration, model catalog, credentials, client commands, public events, session-log storage, and accounting.

`persona/` is content rather than a package: prompts, agents, and procedure skills are mounted into the container and loaded by the agent.

`plugins/` holds one directory per integration (Datadog, incident.io, Jira, Notion, Slack, Google, AWS, GitHub, Scalr) with its `plugin.yaml`, code, skills, and tests. The host reads manifests through `shared/src/archie_shared/plugins/` for credentials and config validation; inside the container the exec runner imports plugin code as `archie_plugins.<id>` and exposes its tools only inside `exec`, under the plugin's namespaces. Core tools (`exec/tools/`) are flat and native. See [Plugins](plugins.md).

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
3. `archie start <workspace>` asks `POST /sessions` for a workspace directory below `global.workspace_root` (default `~/dev`).
4. The orchestrator validates the single directory name, creates a session ID, runs a container, publishes container port 8080 on an ephemeral localhost port, and waits for `/status` to succeed (up to 30 seconds).
5. The CLI connects through the orchestrator’s session routes. The orchestrator resolves the session by Docker container name and proxies to the mapped agent port.
6. `archie stop` calls `DELETE /sessions/{session_id}` and stops the container. Containers use `--rm`; stopping/removing a container does not remove the host session JSONL log or SQLite metrics. After an orchestrator restart, discovery includes only currently running containers.

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

The image contains dependencies and system tools. `gh` on `PATH` is a repository wrapper (`container/bin/gh`, bind-mounted over the copy in the image) around the real binary, `gh-base`; it selects a GitHub token per call from the credential store, so GitHub tokens are not part of the container environment (see [Authentication and credentials](auth-credentials.md#github-cli-token-selection)). The runtime user is `archie`, with its UID matched to the host user during `archie build`. The home and persona mounts are read-write: a session can modify shared user data and repository-tracked persona content. Treat those mounts as a deliberate trust boundary, not as isolated per-container state.

The system prompt's Environment section is the plain-markdown `persona/prompts/environment.md`, with installed tools and paths written out and a few probed values filled in by `agent/src/archie_agent/environment.py`. See [System prompt](system-prompt.md) for the prompt's structure and how to add a tool.

## Host HTTP API

The orchestrator exposes these application routes:

| Method | Route | Role |
| --- | --- | --- |
| `GET` | `/` | session web page |
| `GET` | `/ui/auth` | credential status web page |
| `GET` | `/ui/sessions?q=<query>` | session log viewer: persisted logs, newest first, with search |
| `GET` | `/ui/sessions/{id}` | session log viewer: transcript of one log |
| `GET` | `/ui/sessions/{id}/live?after=<event id>` | server-sent HTML fragments relayed read-only from the agent `/stream` |
| `GET` | `/health` | orchestrator liveness (`{"status":"ok"}`) |
| `GET` | `/sessions` | list running sessions |
| `POST` | `/sessions` | start a session; body `{"workspace":"name"}` |
| `DELETE` | `/sessions/{id}` | stop a session |
| `GET` | `/sessions/{id}/status` | proxy agent status |
| `GET` | `/sessions/{id}/events?after=<id>` | proxy session history read |
| `GET` | `/sessions/{id}/metrics` | metrics for one session |
| `GET` | `/metrics` | aggregate metrics; optional `since=<ISO-8601>` |
| `GET` | `/auth/providers` | typed, secret-free provider definitions |
| `GET` | `/auth/status` | redacted credential status |
| `PUT` | `/auth/credential/{provider}` | replace one static provider credential |
| `DELETE` | `/auth/credential/{provider}` | remove one provider credential |
| `POST` | `/auth/login/{provider}` | start orchestrator-owned OAuth |
| `GET` | `/auth/callback/{provider}` | complete browser OAuth callback |
| `GET` | `/auth/flow/{flow_id}` | query redacted OAuth flow status |
| `POST` | `/auth/refresh/{provider}` | refresh OAuth credentials |
| WebSocket | `/sessions/{id}/stream` | bidirectional client/session stream |
| `GET` | `/static/*` | static assets for the web pages (vendored nimble.css and htmx under `/static/vendor/`) |

The agent service itself listens on port 8080 and provides `/status`, `/events`, and `/stream`. The orchestrator’s session routes are the normal client-facing interface.

The session log viewer reads `<ARCHIE_HOME_DIR>/sessions/*.jsonl` directly, so stopped sessions remain browsable. Usage totals come from `llm_request` events. For an active session, `/ui/sessions/{id}/live` opens the agent `/stream` WebSocket, never sends on it, and does not feed the metrics writer. The viewer pages send a restrictive `Content-Security-Policy`; like the other web pages, they have no authentication and assume the trusted local deployment model.

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
5. The orchestrator observes canonical `llm_request` frames and asynchronously writes them to
   SQLite metrics. The metrics writer is best-effort and does not block the event relay.
6. The TUI applies live and stored events through one imperative `_apply_event(event, historical=...)` path. On connection or reconnect it requests `/events?after=<last-applied-persisted-id>`, then reconciles buffered live events by event ID and exact assistant request identity.

The orchestrator WebSocket proxy is intended to be transparent: it forwards client text to the agent and agent text/binary frames back to the client. Metrics collection is best-effort and must not block or break the relay.

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
user turn; automatic threshold and cold-cache compactions use the same replacement. The session log
remains append-only: `compaction_start` is persisted before the compaction `llm_request`, followed by
`compaction_complete`. New compactions emit only this pair; clients still decode and render historical
`context_compacted` records. The completion carries the status, error or summary, token estimates,
display cost, and duration. Compaction failures are reported through the failed completion, not a
separate `error_notice`. Compaction
requests use `purpose=compaction`, are priced at the configured compaction model, and contribute to
session totals without changing the active conversation context display.

`GET /events` returns ordered persisted NDJSON. Its optional `after` parameter is the client's last
applied persisted event ID; live-only frames never advance that cursor. The TUI applies history
and live frames through `_apply_event(event, historical=...)`, using the historical flag to select
reconciliation behavior; both paths deduplicate by event ID and request identity.

A connect sends one live-only `handshake` containing protocol and session identity, followed by
one `session_status` frame carrying the current model and Git branch. There is no
`session_snapshot` or `session_info` envelope and no parallel wire event schema.
`PROTOCOL_VERSION` remains 2; clients and agents use this final contract together.

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

### Orchestrator metrics

The orchestrator writes only `llm_request` records to `<ARCHIE_HOME_DIR>/metrics.db`, table `requests`. Uniqueness is `(session_id, event_id)`, making ingestion idempotent. The writer uses a bounded asynchronous queue and a schema version; an incompatible existing database is archived as a `.legacy.<UTC>` file before a fresh schema is created. The host-only `archie migrate-sessions` command archives and resets the index, then backfills it from migrated logs. Metrics are an aggregate/indexed view, not a replacement for the session JSONL source of truth.

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

The agent owns the session process, provider requests, tool execution, and canonical log. The orchestrator owns Docker/session lifecycle and the metrics index. The CLI owns presentation and user commands; it must consume server-provided accounting rather than reconstructing prices. Containers receive read-write access to the selected workspace, the configured home directory, and persona content, so agent code and tools can modify those host-backed locations.

## Change impact guide

- **Changing `shared/events.py`** affects persisted logs, replay, agent writes, orchestrator metrics, and clients. Add compatibility tests, update [event-spec.md](event-spec.md), and update this document.
- **Changing `shared/commands.py`** affects client commands, agent WebSocket dispatch, and protocol versioning.
- **Changing mounts, ports, or lifecycle** affects `Dockerfile`, orchestrator lifecycle, readiness checks, security assumptions, and local setup instructions.
- **Changing accounting** requires tests for model switches, interruptions/errors, duplicate ingestion, and nested scopes.
- **Changing `persona/`** changes runtime prompts or capabilities without changing package APIs; review the resulting agent behavior and relevant skill/agent metadata.
- **Changing `plugins/<id>/`** changes one integration; `plugin.yaml` changes also affect host-side credential providers and `config.yaml` validation. Follow [Plugins](plugins.md) and run the contract tests in `tests/test_plugin_layout.py`.

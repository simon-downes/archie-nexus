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

`persona/` is content rather than a package: prompts, agents, and skills are mounted into the container and loaded by the agent.

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
AWS Bedrock / Ollama / local workspace
```

1. `archie build` builds `archie:latest` from `Dockerfile`.
2. `archie serve` runs the host orchestrator; the default profile is `127.0.0.1:7600`.
3. `archie start <workspace>` asks `POST /sessions` for a workspace directory below `global.workspace_root` (default `~/dev`).
4. The orchestrator validates the single directory name, creates a session ID, runs a container, publishes container port 8080 on an ephemeral localhost port, and waits for `/status` to succeed (up to 30 seconds).
5. The CLI connects through the orchestrator’s session routes. The orchestrator resolves the session by Docker container name and proxies to the mapped agent port.
6. `archie stop` calls `DELETE /sessions/{session_id}` and stops the container. Containers use `--rm`; stopping/removing a container does not remove the host session JSONL log or SQLite metrics. After an orchestrator restart, discovery includes only currently running containers.

### Container mounts

The lifecycle code mounts:

| Host content | Container path | Purpose |
| --- | --- | --- |
| `agent/` | `/opt/archie/agent` | live agent source |
| `shared/` | `/opt/archie/shared` | shared source (read-only) |
| `persona/` | `/opt/archie/persona` | prompts, agents, skills |
| selected workspace | `/workspace` | project under development |
| `ARCHIE_HOME_DIR` (normally `~/.nexus`) | `/home/archie/.nexus` | config, credentials, sessions, brain |
| `~/.agents` | `/home/archie/.agents` | agent-related user content |

The image contains dependencies and system tools. The runtime user is `archie`, with its UID matched to the host user during `archie build`. The home and persona mounts are read-write: a session can modify shared user data and repository-tracked persona content. Treat those mounts as a deliberate trust boundary, not as isolated per-container state.

## Host HTTP API

The orchestrator exposes these application routes:

| Method | Route | Role |
| --- | --- | --- |
| `GET` | `/` | session web page |
| `GET` | `/health` | orchestrator liveness (`{"status":"ok"}`) |
| `GET` | `/sessions` | list running sessions |
| `POST` | `/sessions` | start a session; body `{"workspace":"name"}` |
| `DELETE` | `/sessions/{id}` | stop a session |
| `GET` | `/sessions/{id}/status` | proxy agent status |
| `GET` | `/sessions/{id}/events?after=<id>` | proxy session history read |
| `GET` | `/sessions/{id}/metrics` | metrics for one session |
| `GET` | `/metrics` | aggregate metrics; optional `since=<ISO-8601>` |
| `POST` | `/credentials` | replace the host credentials file |
| WebSocket | `/sessions/{id}/stream` | bidirectional client/session stream |
| `GET` | `/static/*` | static assets for the session page |

The agent service itself listens on port 8080 and provides `/status`, `/events`, and `/stream`. The orchestrator’s session routes are the normal client-facing interface.

### Credential push security

`POST /credentials` accepts raw YAML, up to 1 MiB, and atomically replaces `<ARCHIE_HOME_DIR>/credentials.yaml` with mode `0600`. Credential contents are not logged. The endpoint has no authentication or authorization; it is intended for a trusted local/private network and must not be exposed on an untrusted network. In particular, binding `archie serve` to `0.0.0.0` or using a remote profile makes this endpoint a credential-write surface. Use transport/network access controls until an authenticated credential-push protocol exists.

## Turn and data flow

A user message follows this path:

1. The TUI sends a `message` command over the session WebSocket.
2. The orchestrator relays it to the agent without interpreting the command.
3. The agent publishes canonical events through the session coordinator. The coordinator orders
   them, persists persisted events before delivery, and broadcasts them to connected clients.
4. LLM provider streams become canonical text, request, tool, and terminal events. Tool calls may
   invoke native tools, the exec runner, or child agents through the `task` tool.
5. The orchestrator observes canonical `llm_request` frames and asynchronously writes them to
   SQLite metrics. The metrics writer is best-effort and does not block the event relay.
6. The TUI applies live and stored events through one imperative `_render_canonical()` path; live `_apply_event()` dispatches into it. On connection or reconnect it requests `/events?after=<last-applied-persisted-id>`, then reconciles buffered live events by event ID and exact assistant request identity.

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
`tool_call`, `tool_result`, `assistant_message`, `turn_complete`, `turn_error`, and
`turn_interrupted`. The live-only set is exactly `handshake`, `session_status`, `text_delta`, and
`error_notice`.

`GET /events` returns ordered persisted NDJSON. Its optional `after` parameter is the client's last
applied persisted event ID; live-only frames never advance that cursor. The TUI applies history
frames through `_render_canonical(event, replay=True)` and live frames through `_apply_event()`,
which delegates to the same renderer; both paths deduplicate by event ID and request identity.

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

Each `llm_request` contains the model key, status, sent time, duration, token categories (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`), context token count, and immutable `cost_usd`. Session totals are recomputed from these events on disk, so a newly attached client does not depend on the agent process’s in-memory state. Model changes are live-only status updates and do not reprice historical requests.

### Subagent scopes

Root events use `scope=None`. A child’s scope is the launching `task` tool call’s `tool_use_id`; `subagent_index` identifies the child’s display slot. The parent of a scope is found through the launching tool call. `scope_direct_costs` sums requests in one scope; `scope_inclusive_costs` adds descendants with cycle protection. Request identity is `(scope, subagent_index, turn, iteration, request_id)`.

### Orchestrator metrics

The orchestrator writes only `llm_request` records to `<ARCHIE_HOME_DIR>/metrics.db`, table `requests`. Uniqueness is `(session_id, event_id)`, making ingestion idempotent. The writer uses a bounded asynchronous queue and a schema version; an incompatible existing database is archived as a `.legacy.<UTC>` file before a fresh schema is created. The host-only `archie migrate-sessions` command archives and resets the index, then backfills it from migrated logs. Metrics are an aggregate/indexed view, not a replacement for the session JSONL source of truth.

## Configuration, security, and ownership

`shared/config.py` resolves `ARCHIE_HOME_DIR` (default `~/.nexus`) and `ARCHIE_PERSONA_DIR` (host repository `persona`, container `/opt/archie/persona`). `ARCHIE_BRAIN_DIR` defaults to `<ARCHIE_HOME_DIR>/brain`; a relative override is resolved beneath the home directory, while an absolute override is used as-is. An absolute brain override must also be available inside the container through the runtime environment/mount configuration. `ARCHIE_HOST` is a CLI-only `host[:port]` override for targeting an orchestrator and defaults to port 7600 when no port is supplied.

`shared/schemas.py` loads `config.yaml` with defaults:

```yaml
global:
  model: bedrock-openai-gpt-5-6-luna
  region: eu-west-1
  workspace_root: ~/dev
agent:
  subagents:
    max_concurrent: 3
orchestrator:
  profiles:
    default:
      host: 127.0.0.1
      port: 7600
```

Use `profile/value` CLI arguments to select a named orchestrator profile for workspace or session commands. `archie serve` binds to the configured default profile. Profiles with non-loopback hosts and the unauthenticated `/credentials` endpoint require private-network controls.

The agent owns the session process, provider requests, tool execution, and canonical log. The orchestrator owns Docker/session lifecycle and the metrics index. The CLI owns presentation and user commands; it must consume server-provided accounting rather than reconstructing prices. Containers receive read-write access to the selected workspace, the configured home directory, and persona content, so agent code and tools can modify those host-backed locations.

## Change impact guide

- **Changing `shared/events.py`** affects persisted logs, replay, agent writes, orchestrator metrics, and clients. Add compatibility tests, update [event-spec.md](event-spec.md), and update this document.
- **Changing `shared/commands.py`** affects client commands, agent WebSocket dispatch, and protocol versioning.
- **Changing mounts, ports, or lifecycle** affects `Dockerfile`, orchestrator lifecycle, readiness checks, security assumptions, and local setup instructions.
- **Changing accounting** requires tests for model switches, interruptions/errors, duplicate ingestion, and nested scopes.
- **Changing `persona/`** changes runtime prompts or capabilities without changing package APIs; review the resulting agent behavior and relevant skill/agent metadata.

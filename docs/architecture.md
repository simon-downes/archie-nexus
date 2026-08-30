# archie-nexus architecture

This document describes the implementation currently in the repository. Code and tests are authoritative when this document and the implementation disagree. Update this file when a component boundary, route, mount, event schema, persisted format, or accounting rule changes.

## Contents

- [System shape](#system-shape)
- [Runtime topology and lifecycle](#runtime-topology-and-lifecycle)
- [Host HTTP API](#host-http-api)
- [Turn and data flow](#turn-and-data-flow)
- [Event layers](#event-layers)
- [Logs, accounting, and metrics](#logs-accounting-and-metrics)
- [Configuration, security, and ownership](#configuration-security-and-ownership)
- [Change impact guide](#change-impact-guide)

## System shape

Nexus has four Python workspace packages:

- **CLI (`cli/`)** — host-side `archie` command, Textual TUI, authentication commands, and clients for the orchestrator.
- **Orchestrator (`orchestrator/`)** — host-side Starlette control plane. It starts/stops Docker containers, discovers sessions, proxies session HTTP/WebSocket traffic, serves the session page, and records aggregate LLM request metrics.
- **Agent (`agent/`)** — container-side Starlette application. It owns one session, the agent loop, model client, tools, skills, subagents, and session event log.
- **Shared (`shared/`)** — cross-boundary types and contracts: configuration, model catalog, credentials, wire events/commands, canonical events, log replay, and accounting.

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
| `GET` | `/sessions/{id}/events?after=<id>` | proxy canonical event replay |
| `POST` | `/sessions/{id}/shell` | proxy direct-shell log entry |
| `GET` | `/sessions/{id}/metrics` | metrics for one session |
| `GET` | `/metrics` | aggregate metrics; optional `since=<ISO-8601>` |
| `POST` | `/credentials` | replace the host credentials file |
| WebSocket | `/sessions/{id}/stream` | bidirectional client/session stream |
| `GET` | `/static/*` | static assets for the session page |

The agent service itself listens on port 8080 and provides `/status`, `/events`, `/shell`, and `/stream`. The orchestrator’s session routes are the normal client-facing interface.

### Credential push security

`POST /credentials` accepts raw YAML, up to 1 MiB, and atomically replaces `<ARCHIE_HOME_DIR>/credentials.yaml` with mode `0600`. Credential contents are not logged. The endpoint has no authentication or authorization; it is intended for a trusted local/private network and must not be exposed on an untrusted network. In particular, binding `archie serve` to `0.0.0.0` or using a remote profile makes this endpoint a credential-write surface. Use transport/network access controls until an authenticated credential-push protocol exists.

## Turn and data flow

A user message follows this path:

1. The TUI sends a `message` command over the session WebSocket.
2. The orchestrator relays it to the agent without interpreting the command.
3. The agent persists the user/canonical turn records, runs the LLM/tool loop, and broadcasts live frames to connected clients.
4. LLM provider streams become text, tool, usage, and terminal turn events. Tool calls may invoke native tools, the exec runner, or child agents through the `task` tool.
5. The agent appends canonical records to the session JSONL log. The orchestrator observes broadcast `llm_request` frames and asynchronously writes them to SQLite metrics.
6. The TUI renders live events. On connection or reconnect it uses the snapshot cursor and `/events` replay to reconcile persisted history, then resumes live streaming.

The orchestrator WebSocket proxy is intended to be transparent: it forwards client text to the agent and agent text/binary frames back to the client. Metrics collection is best-effort and must not block or break the relay.

## Event layers

### Canonical persisted events

Defined in `shared/src/archie_shared/canonical_events.py`, canonical events are flat tagged `msgspec` records. Persisted records are newline-delimited JSON in:

```text
<ARCHIE_HOME_DIR>/sessions/<session-id>.jsonl
```

Each record has a generated ULID-like `id` in normal operation. The schema requires only a non-empty string; persistence rejects conflicting duplicate IDs and treats an identical retry as idempotent. Append order is the replay order. The persisted union currently includes:

- `session_started`
- `user_message`
- `iteration_start`
- `llm_request`
- `tool_call`
- `tool_result`
- `assistant_message`
- `turn_complete`
- `turn_error`
- `turn_interrupted`
- `model_switch`
- `shell_command`

`text_delta` is part of the canonical type family for live streaming but is intentionally excluded from the persisted union. Persisted replay therefore reconstructs assistant content from `assistant_message`, not from token deltas.

`GET /events` returns ordered NDJSON. The optional `after` cursor is an event ID; a missing cursor returns a conflict rather than silently replaying from the beginning.

The session log module still contains `MessageEntry`/`MessageMetadata` helpers for transition compatibility. Current canonical session persistence and accounting use the event helpers described above.

### WebSocket wire protocol

Defined in `shared/src/archie_shared/events.py`, protocol version 1 uses JSON envelopes:

```json
{"type":"<event_type>","turn_index":0,"data":{}}
```

Session-level frames omit `turn_index`. On connect, the agent sends `session_snapshot` followed by `session_info`. The snapshot contains the latest persisted event ID and disk-derived accounting totals; this lets a client replay from a known cursor. Client commands are `message`, `interrupt`, and `switch_model`.

Wire event categories include iteration starts, text deltas, usage, tool calls/results, turn completion/error/interruption, model switches, status updates, and scoped child-agent activity. A raw canonical `llm_request` frame is also broadcast so accounting consumers can use the immutable request record.

Do not treat all wire frames as durable. In particular, text deltas, connection snapshots, usage/status refreshes, and other presentation frames are live-only unless they correspond to a persisted canonical event. Conversely, persisted body events may not be broadcast as a live frame.

## Logs, accounting, and metrics

### Canonical log rules

- `append_event()` validates the serialized event and rejects conflicting duplicate IDs; identical duplicates are idempotent.
- Replay preserves append order and skips malformed lines with a warning.
- The user message is written before provider streaming; the assistant message is written after streaming completes.
- `MessageMetadata` is compatibility/display metadata. It is not the authoritative accounting ledger.

Each `llm_request` contains the model key, status, sent time, duration, token categories (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`), context token count, and immutable `cost_usd`. Session totals are recomputed from these events on disk, so a newly attached client does not depend on the agent process’s in-memory state. Model switches do not reprice historical requests.

### Subagent scopes

Root events use `scope=None`. A child’s scope is the launching `task` tool call’s `tool_use_id`; `subagent_index` identifies the child’s display slot. The parent of a scope is found through the launching tool call. `scope_direct_costs` sums requests in one scope; `scope_inclusive_costs` adds descendants with cycle protection. Request identity is `(scope, turn_iteration, request_id)`.

### Orchestrator metrics

The orchestrator writes only `llm_request` records to `<ARCHIE_HOME_DIR>/metrics.db`, table `requests`. Uniqueness is `(session_id, event_id)`, making ingestion idempotent. The writer uses a bounded asynchronous queue and a schema version; an incompatible existing database is archived as a `.legacy.<UTC>` file before a fresh schema is created. Metrics are an aggregate/indexed view, not a replacement for the session JSONL source of truth.

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

- **Changing `shared/canonical_events.py`** affects persisted logs, replay, agent writes, orchestrator metrics, and clients. Add compatibility tests and update this document.
- **Changing `shared/events.py`** affects the CLI/TUI, agent WebSocket handlers, proxy behavior, and protocol versioning.
- **Changing mounts, ports, or lifecycle** affects `Dockerfile`, orchestrator lifecycle, readiness checks, security assumptions, and local setup instructions.
- **Changing accounting** requires tests for model switches, interruptions/errors, duplicate ingestion, and nested scopes.
- **Changing `persona/`** changes runtime prompts or capabilities without changing package APIs; review the resulting agent behavior and relevant skill/agent metadata.

# Archie Nexus canonical event specification

This is the public server-to-client event contract for Archie Nexus. The same tagged
`msgspec` union is used for live WebSocket delivery and persisted session replay. The
schema is defined in `shared/src/archie_shared/events.py`.

## Encoding and identity

Events are UTF-8 JSON objects encoded as newline-delimited JSON for session logs. Every
event has a `type` tag and a non-empty ULID-like `id`. Persisted log order is append
order and therefore replay order. Event IDs are assigned during event construction by the
producing component; `EventFactory` is the shared helper for root and child turn-loop events.
The session coordinator assigns ordering, persists persisted events, and enqueues delivery.

The event ID's ULID timestamp component is the event's creation-time signal. Consumers
that need event creation time should derive it from the ULID rather than expecting a
universal timestamp field. Domain timestamps remain where they describe a distinct
operation: `session_started.sent_at` and `llm_request.sent_at` are provider/session timestamps, not a replacement for event ID ordering.

The public event union has exactly four live-only types: `text_delta`, `handshake`,
`session_status`, and `error_notice`. Every other remaining type below is persisted and may be
replayed through `GET /events`.

## Event catalog

`id` is included in every row below. Optional fields use `?`; `scope` and
`subagent_index` identify child-agent activity where present.

| Type | Delivery | Fields besides `id` |
| --- | --- | --- |
| `session_started` | persisted | `schema_version: int`, `sent_at: string`, `model_key: string` |
| `user_message` | persisted | `turn: int`, `scope: string?`, `content: string`, `subagent_index: int?` |
| `iteration_start` | persisted | `turn: int`, `iteration: int`, `scope: string?`, `subagent_index: int?` |
| `text_delta` | live-only | `turn: int`, `iteration: int`, `scope: string?`, `request_id: string`, `text: string`, `subagent_index: int?` |
| `llm_request` | persisted | `scope: string?`, `turn: int`, `iteration: int`, `model_key: string`, `sent_at: string`, `duration_ms: int`, `status: completed \| interrupted \| error \| no_usage`, `input_tokens: int`, `output_tokens: int`, `cache_read_tokens: int`, `cache_write_tokens: int`, `context_tokens: int`, `cost_usd: float`, `stop_reason: string?`, `error: string?`, `subagent_index: int?` |
| `tool_call` | persisted | `turn: int`, `iteration: int`, `scope: string?`, `request_id: string`, `tool_use_id: string`, `name: string`, `input: dict[string, object]`, `subagent_index: int?` |
| `tool_result` | persisted | `turn: int`, `iteration: int`, `scope: string?`, `request_id: string`, `tool_use_id: string`, `content: string`, `is_error: bool`, `duration_ms: int`, `result_bytes: int`, `result_lines: int`, `subagent_index: int?` |
| `assistant_message` | persisted | `turn: int`, `scope: string?`, `request_ids: list[string]`, `content: string`, `interrupted: bool`, `subagent_index: int?` |
| `turn_complete` | persisted | `turn: int`, `scope: string?`, `stop_reason: string`, `subagent_index: int?` |
| `turn_error` | persisted | `turn: int`, `scope: string?`, `message: string`, `subagent_index: int?` |
| `turn_interrupted` | persisted | `turn: int`, `scope: string?`, `subagent_index: int?` |
| `shell_command` | persisted | `command: string`, `exit_code: int`, `output: string`, `turn: int?`, `scope: string?` |
| `handshake` | live-only | `protocol_version: int`, `session_id: string` |
| `session_status` | live-only | `model_key: string`, `git_branch: string` |
| `error_notice` | live-only | `kind: string`, `message: string` |

There is no public `usage` event. Provider usage is folded into the persisted
`llm_request`; clients derive cumulative token and cost totals by summing those events,
deduplicated by `id`. Model switches are live-only status updates and do not reprice historical
requests. The model catalog maps `model_key` to display metadata, so `session_status` and
`handshake` do not repeat unnecessary model fields.

## Turn and replay rules

`turn` and `iteration` are structured integers. Request and tool identity is
`(scope, turn, iteration, request_id)`; the old composite `turn_iteration` string is not
part of the schema.

The coordinator persists an event before broadcasting it. Live-only events are enqueued
without appending. The client advances its replay cursor only after applying a persisted
event, so the cursor always names a logged event. On reconnect, the client replays
`GET /events?after=<cursor>`, then flushes buffered live events through the same reducer
and deduplicates by event ID. `handshake` is followed by `session_status`; neither is a
snapshot or accounting authority.

`error_notice` is used for targeted command rejections and best-effort storage-failure
notification. It is not a persisted turn terminal event. Accepted turns emit exactly
one persisted `turn_complete`, `turn_error`, or `turn_interrupted`, except when the
append path itself fails.

## Persistence and migration

Session logs live at `<ARCHIE_HOME_DIR>/sessions/<session-id>.jsonl`. The host command
`archie migrate-sessions` is a one-shot operation to run with sessions and the
orchestrator stopped. It upgrades legacy canonical identity fields, removes model-switch records while preserving IDs and
order of remaining records, converts legacy shell `MessageEntry` records to `shell_command`,
retains the source as `.legacy`, and writes each log through a same-directory temporary file and
atomic rename. Logs declaring `session_started.schema_version == 2` are skipped. The command then
archives and rebuilds `<ARCHIE_HOME_DIR>/metrics.db` from migrated `llm_request` events. Metrics
use integer `turn`/`iteration` columns and deduplicate by `(session_id, event_id)`; the session log
remains the source of truth.

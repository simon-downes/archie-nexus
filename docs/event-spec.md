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

## Client commands

Client-to-agent commands are strict, flat tagged `msgspec` records defined in
`shared/src/archie_shared/commands.py`. They are encoded without a nested `data` object:

```json
{"type":"message","content":"hello"}
{"type":"interrupt"}
{"type":"interrupt","scope":"task-01","subagent_index":0}
{"type":"switch_model","model_key":"bedrock-anthropic.claude-sonnet-4-6"}
{"type":"answer","question_id":"tooluse_01","option":1}
{"type":"answer","question_id":"tooluse_01","text":"Use MySQL"}
{"type":"answer","question_id":"tooluse_01","dismiss":true}
```

Unknown fields and tags are rejected. An `interrupt` command either targets the root with no
fields or targets a child with both `scope` and `subagent_index`; partial targets are invalid.
An `answer` command replies to the pending question named by `question_id` (the `question` tool
call's `tool_use_id`) with exactly one of a 0-based `option`, a non-empty `text`, or
`dismiss: true`; anything else fails to decode. A stale or unknown `question_id` gets a targeted
`error_notice` with `kind=question_not_pending`, and an out-of-range option gets
`kind=invalid_answer`; the question stays pending in both cases.

## Event catalog

`id` is included in every row below. Optional fields use `?`; `scope` and
`subagent_index` identify child-agent activity where present.

| Type | Delivery | Fields besides `id` |
| --- | --- | --- |
| `session_started` | persisted | `schema_version: int`, `sent_at: string`, `model_key: string` |
| `user_message` | persisted | `turn: int`, `scope: string?`, `content: string`, `subagent_index: int?` |
| `iteration_start` | persisted | `turn: int`, `iteration: int`, `scope: string?`, `subagent_index: int?` |
| `text_delta` | live-only | `turn: int`, `iteration: int`, `scope: string?`, `request_id: string`, `text: string`, `subagent_index: int?` |
| `llm_request` | persisted | `scope: string?`, `turn: int`, `iteration: int`, `model_key: string`, `sent_at: string`, `duration_ms: int`, `status: completed \| interrupted \| error \| no_usage`, `input_tokens: int`, `output_tokens: int`, `cache_read_tokens: int`, `cache_write_tokens: int`, `context_tokens: int`, `cost_usd: float`, `stop_reason: string?`, `error: string?`, `subagent_index: int?`, `purpose: conversation \| compaction`, `credits: float` (Kiro only; absent otherwise) |
| `compaction_start` | persisted | `turn: int`, `iteration: int`, `scope: string?`, `request_id: string`, `reason: manual \| threshold \| cold_cache`, `tokens_before: int`, `estimated_next_request_cost_usd: float?`, `model_key: string`, `sent_at: string`, `subagent_index: int?` |
| `compaction_complete` | persisted | `turn: int`, `iteration: int`, `scope: string?`, `request_id: string`, `reason: manual \| threshold \| cold_cache`, `status: success \| failure \| interrupted`, `tokens_before: int`, `tokens_after: int?`, `cost_saved_usd: float`, `duration_ms: int`, `error: string?`, `summary: string?`, `debug: string`, `subagent_index: int?`, `estimated_next_request_cost_usd: float?`, `skills_loaded: list[string]` |
| `context_compacted` | persisted (legacy, no longer emitted) | `turn: int`, `iteration: int`, `scope: string?`, `request_id: string`, `reason: manual \| threshold \| cold_cache`, `summary: string`, `model_key: string`, `tokens_before: int`, `tokens_after: int`, `duration_ms: int`, `debug: string`, `subagent_index: int?` |
| `tool_call` | persisted | `turn: int`, `iteration: int`, `scope: string?`, `request_id: string`, `tool_use_id: string`, `name: string`, `input: dict[string, object]`, `subagent_index: int?` |
| `tool_result` | persisted | `turn: int`, `iteration: int`, `scope: string?`, `request_id: string`, `tool_use_id: string`, `content: string`, `is_error: bool`, `duration_ms: int`, `result_bytes: int`, `result_lines: int`, `subagent_index: int?`, `images: list[{format: string, width: int, height: int, bytes: int, source: string}]` |
| `assistant_message` | persisted | `turn: int`, `iteration: int`, `scope: string?`, `request_id: string`, `content: string`, `interrupted: bool`, `subagent_index: int?` |
| `turn_complete` | persisted | `turn: int`, `scope: string?`, `stop_reason: string`, `subagent_index: int?` |
| `turn_error` | persisted | `turn: int`, `scope: string?`, `message: string`, `subagent_index: int?` |
| `turn_interrupted` | persisted | `turn: int`, `scope: string?`, `subagent_index: int?` |
| `handshake` | live-only | `protocol_version: int`, `session_id: string` |
| `session_status` | live-only | `model_key: string`, `git_branch: string`, `state: idle \| working \| blocked \| done \| error` (default `idle`), `question: {id: string, question: string, options: list[{label: string, description: string}], recommended: int?}?` |
| `error_notice` | live-only | `kind: string`, `message: string` |

Compaction operations are persisted as `compaction_start` followed by the compactor `llm_request` (omitted when the transcript cannot be fitted and no provider request is made) and exactly one `compaction_complete`. Compaction failures are reported only through a `failure` or `interrupted` completion. `estimated_next_request_cost_usd` is the active conversation model's pre-compaction estimate for a cold-cache request; it is set only for `cold_cache` compactions and repeated on the completion. Clients display it when present and `cost_saved_usd` otherwise. `skills_loaded` lists the sorted names of skills whose body was loaded when compaction started; it is set on every status and decodes as `[]` from older logs. `tokens_before` and `tokens_after` are provider-facing context estimates: heuristic estimates include the system prompt and tool configuration; provider-baseline estimates retain the provider's reported boundary. `cost_saved_usd` is a display estimate and is not an accounting source. On success it prices the removed tokens at the active model's cache write rate (its input rate when it has none): the cache write the next request avoids, assuming a cold cache. It ignores cache reads on later requests, and it overstates the saving for a compaction that happens while the cache is warm.

There is no public `usage` event. Provider usage is folded into the persisted
`llm_request`; clients derive cumulative token and cost totals by summing those events,
deduplicated by `id`. Model changes are live-only `session_status` updates and do not reprice
historical requests. The model catalog maps `model_key` to display metadata, so `session_status`
and `handshake` do not repeat unnecessary model fields.

`session_status.state` uses the OSC 7501 vocabulary. The agent currently emits `idle`, `working`
(a root turn or manual compaction is running) and `blocked` (the root `question` tool is waiting;
`question` carries the pending question). A new frame is sent when a turn starts, when a question
is asked or resolved, and when the turn ends. `GET /status` returns the same `state` and
`question` (or `null`). The pending question is live state only: the persisted record of a question
is its `tool_call` and `tool_result`, whose content is `User selected option N: <label>`,
`User answered: <text>`, or a dismissal sentence.

## Turn and replay rules

`turn` and `iteration` are structured integers. Request and tool identity is
`(scope, subagent_index, turn, iteration, request_id)`; the old composite `turn_iteration` string
is not part of the schema.

The coordinator persists an event before broadcasting it. Live-only events are enqueued
without appending. The client advances its replay cursor only after applying a persisted
event, so the cursor always names a logged event. On reconnect, the client replays
`GET /events?after=<cursor>` and then flushes buffered live events, applying both through the
same `apply(event)` path (the TUI's `SessionController.apply`), and deduplicates by event ID and request
identity. `handshake` is followed by `session_status`; neither is a
snapshot or accounting authority.

`error_notice` is used for targeted command rejections, best-effort storage-failure
notification, and tool-policy reload warnings (`tool_policy_reload_failed`,
`tool_policy_reloaded`). It is not a persisted turn terminal event. Accepted turns emit exactly
one persisted `turn_complete`, `turn_error`, or `turn_interrupted`, except when the
append path itself fails. Available partial assistant text is persisted as an interrupted
`assistant_message` before the matching terminal event.

## Persistence and migration

Session logs live at `<ARCHIE_HOME_DIR>/sessions/<session-id>.jsonl`. The host command
`archie migrate-sessions` is a one-shot operation to run with sessions and the
orchestrator stopped. It upgrades legacy identity fields, selects the final request ID
that generated each assistant message and derives its turn/iteration from the referenced
`llm_request`, removes model-switch records while preserving IDs and order of remaining
records, discards legacy shell records by discriminator, retains the source as `.legacy`,
and writes each log through a same-directory temporary file and atomic rename. Logs
declaring `session_started.schema_version == 2` are skipped. The command then drops and
rebuilds the metrics tables (`metrics_*`) in `<ARCHIE_HOME_DIR>/archie.db` from the migrated logs
(all canonical event types; session records are kept; see
[Architecture](architecture.md#orchestrator-metrics)). Requests deduplicate
by `(session_id, event_id)`; the session log remains the source of truth.

`tool_result.images` was added after schema version 2 and defaults to an empty list, so
earlier logs decode unchanged. It records metadata only for images returned by the native
`read` tool (format, final dimensions, encoded size, source path). Image bytes are kept only
in the agent's in-memory transcript and are never persisted or sent to clients.

`compaction_complete.skills_loaded` was added without a protocol version change and defaults
to an empty list, so earlier logs decode unchanged. Events are decoded with unknown fields
forbidden, so a client built before the field existed drops every new `compaction_complete`
as malformed; update the CLI with the agent. Before this change, `cost_saved_usd` priced the
removed tokens at the input rate, so older records are about 20% lower for the same saving
on models whose cache write costs 1.25× input.

`llm_request.credits` was added without a protocol version change. Only Kiro-backed sessions
(`archie start --kiro`) write it: one root `llm_request` per Kiro turn, written after the
turn's tool calls, with `credits` summed from Kiro's turn summary, zero token counts and
`cost_usd` derived from the credits (see [kiro.md](kiro.md)). Native requests omit the field,
so their lines are unchanged. A client built before the field existed drops Kiro
`llm_request` lines as malformed; update the CLI with the agent.

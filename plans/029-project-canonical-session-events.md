# 029 — Canonical Session Events and Usage Ledger

## Objective

Replace the current split between internal agent events, wire events, message-oriented JSONL logs, TUI-local cost calculation, and orchestrator-side cost reconstruction with one canonical event stream.

The agent creates each canonical event once, appends its serialized form to the session JSONL file, and broadcasts that same serialized form to connected clients. The stream supports accurate per-request usage and cost accounting, model switching, replay for newly attached clients, subagent attribution, and simpler debugging.

This project is the accounting and event-stream foundation for `028-native-subagent-tool`.

## Context

Nexus currently has separate representations:

- internal agent events in `agent/events.py`
- serialized wire events in `shared/events.py`
- reduced message entries in `shared/session/log.py`
- TUI-local token and cost accumulation
- orchestrator-local metrics and cost calculation

Usage is not persisted as a first-class record. Tool-loop requests are collapsed into one assistant message, and the in-memory session cost reprices all historical tokens using the current model after a model switch.

This is a single-user application. Existing session logs do not need migration or backward-compatible decoding; the current schema may be replaced.

The existing subagent plan assumes `MessageEntry` attribution fields and generic agent IDs. This plan replaces those assumptions with canonical `llm_request` events and `scope` values derived from the launching `tool_use_id`.

## Requirements

### Canonical event representation

- MUST use one canonical event representation for replayable WebSocket events and persisted session-log records
  - AC: For persisted event types, the JSON serialized for broadcast is byte-equivalent to the JSON appended to the session log
  - AC: Live-only events (`text_delta`, `session_snapshot`, `status_updated`) are broadcast but never appended; they are exempt from the persisted byte-equivalence rule
  - AC: The orchestrator relays canonical events without translating their contents

- MUST represent event-specific fields at the top level of each record
  - AC: Records contain fields such as `type`, `id`, `turn_iteration`, and `cost_usd` directly
  - AC: Replayable records do not wrap event-specific fields inside a generic `data` object

- MUST assign every event a unique ULID `id`
  - AC: No two events in a session log have the same `id`
  - AC: Event IDs can be used by metrics ingestion for idempotent deduplication

- MUST preserve append order as the authoritative event order
  - AC: Replaying a session returns events in the same order as their JSONL lines
  - AC: Event ordering does not depend on lexical sorting of ULIDs

- MUST NOT include `session_id` in persisted event records
  - AC: The session is identified exclusively by its JSONL filename

### Event types

- MUST support the following persisted and replayable event tags: `session_started`, `user_message`, `iteration_start`, `text_delta`, `llm_request`, `tool_call`, `tool_result`, `assistant_message`, `turn_complete`, `turn_error`, `turn_interrupted`, `model_switch`, and `shell_command`
  - AC: Each listed tag can be serialized, appended to JSONL, deserialized, and replayed
  - AC: Each listed tag has a documented field set in the shared event definitions

- MUST use `llm_request` as the single usage-ledger event tag
  - AC: There is no separate persisted `usage` record for the same provider request
  - AC: Every request-level token and cost field is present on exactly one `llm_request` record

- MUST keep `session_snapshot` as a connection-only event and MUST NOT persist it
  - AC: A newly attached client receives current session metadata and the latest event ID without that snapshot being appended to JSONL

- MUST treat `text_delta` as a live-only streaming event and MUST NOT persist it
  - AC: `text_delta` events are broadcast over the live WebSocket for streaming UX but never appended to JSONL
  - AC: Replay renders assistant responses from persisted `assistant_message` records alone; no per-delta reconciliation is required
  - AC: The full assistant text is recoverable from `assistant_message.content` without any `text_delta` records

- MUST keep `status_updated` as a connection/live-state event and MUST NOT persist it
  - AC: Git-branch/status refreshes are generated from current state and are not replayed as historical conversation events

- MUST NOT persist internal worker events such as sentinels or worker-thread errors
  - AC: Internal control events never appear in JSONL or the public WebSocket stream

### Turn and scope attribution

- MUST identify each root-agent LLM request using a combined `turn_iteration` value such as `2.1`
  - AC: A tool-loop turn that makes three LLM requests produces `2.0`, `2.1`, and `2.2` request values

- MUST represent root-agent scope as `null`
  - AC: Requests made directly by the primary agent have `scope: null`

- MUST represent a subagent scope using the `tool_use_id` of the tool call that launched that subagent
  - AC: All LLM request events produced by a subagent contain the launching tool's `tool_use_id` as `scope`
  - AC: Nested subagents use their own launching tool IDs as scope values

- MUST allow identical `turn_iteration` values in different scopes
  - AC: Root scope and a child scope can both contain `2.1` without their records being confused

### LLM usage and cost

- MUST persist one LLM request event for every logical provider request
  - AC: A root turn with two tool-loop iterations produces two LLM request records
  - AC: Each subagent provider request produces its own LLM request record

- MUST include the active `model_key` on every LLM request event
  - AC: Requests before and after a model switch contain their respective model keys
  - AC: A historical request does not depend on the session's current model to identify its model

- MUST include request timing as `sent_at` and `duration_ms`
  - AC: A completed request contains the time the provider request was sent and its elapsed duration
  - AC: Failed or interrupted requests can record their status and duration

- MUST include request status
  - AC: Completed, interrupted, error, and unavailable-usage requests are distinguishable

- MUST persist actual provider-reported token usage on the corresponding LLM request event
  - AC: The event contains `input_tokens`, `output_tokens`, `cache_read_tokens`, and `cache_write_tokens`
  - AC: No estimated token values are persisted as actual usage

- MUST persist `cost_usd` calculated using the model and pricing active when the request completed
  - AC: Persisted cost remains unchanged after a model switch or model-catalog update
  - AC: Historical session cost is calculated by summing persisted request costs rather than repricing cumulative tokens with the current model

- MUST calculate actual context size as `input_tokens + cache_read_tokens + cache_write_tokens`
  - AC: The event exposes the actual context-token count or allows it to be calculated directly from these fields
  - AC: No estimated `context_pct` value is persisted as accounting data

- MUST NOT duplicate pricing rates in every usage record
  - AC: An LLM request identifies its model with `model_key` and stores `cost_usd`, but does not contain per-token pricing fields

### Message and tool correlation

- MUST allow an assistant message to reference all LLM requests that contributed to it
  - AC: A tool-loop assistant response contains an ordered `request_ids` list

- MUST associate each tool call with the LLM request that emitted it
  - AC: A tool-call event contains the originating request ID

- MUST associate each tool result with its tool call
  - AC: A tool-result event contains the corresponding `tool_use_id`

- MUST persist structured tool input and result data in canonical tool events
  - AC: Tool-call events contain structured input rather than only a UI-formatted summary
  - AC: The TUI can render tool activity from canonical data without Rich-formatted strings generated by the agent

### Persistence, broadcast, and replay

- MUST assign the event ID and append a canonical event to the session log before broadcasting it to clients
  - AC: Every event observed by a client is already present in the session log
  - AC: A broadcast failure does not remove the event from the persisted log

- MUST allow a client to request events after a previously received event ID
  - AC: A client providing its last event ID receives all later events in JSONL append order
  - AC: A client can attach while a turn is active without losing events produced during replay

- MUST use the same canonical request events for live delivery and replay
  - AC: A replayed usage event has the same ID, model key, token counts, and cost as the original live event

### Client accounting

- MUST have the TUI consume authoritative `cost_usd` values from LLM request events
  - AC: The TUI does not calculate historical cost from the latest model-switch pricing rates
  - AC: Switching models does not alter previously displayed cost

- MUST restore session token and cost totals when a client attaches to an existing session
  - AC: A newly attached client displays totals derived from replayed request events or an equivalent session accounting snapshot

- MAY derive a display-only context percentage from actual context tokens and the active model context limit
  - AC: Any displayed percentage changes when actual context-token usage changes
  - AC: The percentage is not used as the authoritative ledger value

### Orchestrator metrics

- MUST consume canonical LLM request events rather than reconstructing cost from separate usage and model-switch state
  - AC: Each metrics row contains the request event ID, model key, token counts, and persisted cost

- MUST make metrics ingestion idempotent by event/request ID
  - AC: Replaying the same request event does not create a second usage or cost entry
  - AC: Multiple attached clients cannot multiply the recorded cost of one request

### Internal loop boundary

- MUST allow the pure agent loop to emit semantic events without depending on WebSockets, session-log files, or client state
  - AC: `run_loop()` can be exercised with a fake LLM and event consumer without starting the HTTP server or opening a WebSocket
  - AC: The harness adds event IDs, scope, turn/iteration context, timing, model, and cost before persistence and broadcast

### Subagent integration

- MUST attribute subagent usage through the launching tool's `tool_use_id`
  - AC: Subagent request costs can be aggregated by the launching tool call
  - AC: The session log does not require generic `agent_id` or `parent_agent_id` fields

- MUST derive direct and inclusive costs from atomic LLM request events
  - AC: Direct cost includes only requests in a given scope
  - AC: Inclusive cost includes descendant subagent scopes
  - AC: Session cost equals the sum of all persisted request costs

## Technical Design

### Overview

Use a flat, tagged `msgspec` event union in `archie-shared` as the canonical protocol and persistence model. The agent harness creates an event once, persists its serialized form, and broadcasts the same serialized form. The orchestrator relays it and deduplicates request events for metrics.

The pure loop continues to emit semantic events without transport or persistence dependencies. A new agent-side event factory enriches those events with ULIDs, scope, turn/iteration, model, timing, and cost before producing canonical records.

### Technical Stack

- Reuse the existing `msgspec` dependency declared in `shared/pyproject.toml` for canonical event structs and serialization. Note: `msgspec` is currently a `archie-shared`-only dependency; canonical structs live in `shared` and the agent constructs events through the factory, so the agent does not gain a direct `msgspec` dependency.
- Reuse the existing `python-ulid` dependency in the agent (`agent/pyproject.toml`) for event ID generation; shared structs store IDs as strings and do not gain a ULID dependency.
- Add no new dependencies.

### Architecture

**Shared canonical events** (`shared/src/archie_shared/events.py`)

- Own event types, tagged serialization, and deserialization.
- Do not generate IDs, access files, or calculate cost.
- Use flat top-level `type` tags.

**Shared event log** (`shared/src/archie_shared/session/log.py`)

- Own append and ordered replay of canonical event JSONL.
- Write the exact serialized event string supplied by the agent.
- Do not own session totals or model state.

**Agent event factory** (`agent/src/archie_agent/event_log.py`)

- Own ID generation and canonical event construction.
- Calculate request cost using the active `ModelEntry`.
- Append before broadcast.
- Track request IDs associated with the active turn.

**Agent harness** (`agent/src/archie_agent/harness.py`)

- Consume internal loop events.
- Provide the event factory with session, turn, scope, and model context.
- Replace message-specific persistence helpers with canonical event emission.

**Agent application** (`agent/src/archie_agent/app.py`)

- Provide connection snapshots separately from historical events.
- Expose canonical event replay after an event ID.
- Route model switches and shell events through canonical event creation.

**Orchestrator**

- Relay canonical JSON unchanged.
- Parse `llm_request` records for metrics only.
- Deduplicate metrics by request event ID.

**TUI**

- Process replayed and live canonical events through one handler.
- Rebuild conversation, model state, usage totals, and cost from events.

### Data Model

Every persisted/replayable event has:

```text
type: string, required
id: ULID string, required
```

Applicable correlation fields are:

```text
scope: string | null, required on scoped events
turn: integer | null, required on turn/message events
turn_iteration: string | null, required on request/tool/stream events
```

The canonical event definitions MUST specify exact field types and nullability. Timestamps use ISO-8601 UTC strings. Durations and token counts use non-negative integers. Monetary values use decimal JSON numbers rounded to six decimal places when persisted. Error text and tool content remain strings; no additional truncation is performed by the event schema.

`llm_request` records have:

```text
id: ULID string
scope: string | null
turn_iteration: string
model_key: string
sent_at: ISO-8601 UTC string
duration_ms: non-negative integer
status: "completed" | "interrupted" | "error" | "no_usage"
input_tokens: non-negative integer
output_tokens: non-negative integer
cache_read_tokens: non-negative integer
cache_write_tokens: non-negative integer
context_tokens: non-negative integer
cost_usd: non-negative number
stop_reason: string | null
error: string | null
```

For `status` values other than `completed`, unavailable numeric values are recorded as zero and the status/error fields explain why. `context_tokens` equals:

```text
input_tokens + cache_read_tokens + cache_write_tokens
```

The `id` of an `llm_request` is its request ID. Messages have `request_ids: list[str]` where they represent the response from multiple provider requests. Tool calls have `request_id` and `tool_use_id`; tool results have `tool_use_id`. `shell_command` records contain command, exit code, output, turn, and scope fields.

Only `llm_request` records contribute to cost aggregation:

- Direct cost for a scope is the sum of its own `llm_request.cost_usd` values.
- A child scope's parent is the scope on the `tool_call` whose `tool_use_id` equals the child scope.
- Inclusive cost is direct cost plus recursively linked descendant scopes.
- Root direct cost includes only `scope: null`; root inclusive cost includes all linked descendants.
- An orphan scope is counted in session totals and its own direct total but is not assigned to an unrelated parent.
- Unknown or missing parent links generate a warning and do not prevent request persistence.

The canonical persisted/replayable event fields are:

| Tag | Required fields | Optional/null fields | Persisted/replayed |
|---|---|---|---|
| `session_started` | `id: str`, `schema_version: int`, `sent_at: str`, `model_key: str` | none | yes |
| `user_message` | `id: str`, `turn: int`, `scope: str \| None`, `content: str` | `scope` is null for root | yes |
| `iteration_start` | `id: str`, `turn_iteration: str`, `scope: str \| None`, `index: int` | `scope` is null for root | yes |
| `text_delta` | `id: str`, `turn_iteration: str`, `scope: str \| None`, `request_id: str`, `text: str` | none | no (live-only, broadcast; never appended) |
| `llm_request` | `id: str`, `scope: str \| None`, `turn_iteration: str`, `model_key: str`, `sent_at: str`, `duration_ms: int`, `status: str`, `input_tokens: int`, `output_tokens: int`, `cache_read_tokens: int`, `cache_write_tokens: int`, `context_tokens: int`, `cost_usd: float` | `stop_reason: str \| None`, `error: str \| None` | yes |
| `tool_call` | `id: str`, `turn_iteration: str`, `scope: str \| None`, `request_id: str`, `tool_use_id: str`, `name: str`, `input: dict` | none | yes |
| `tool_result` | `id: str`, `turn_iteration: str`, `scope: str \| None`, `request_id: str`, `tool_use_id: str`, `content: str`, `is_error: bool`, `duration_ms: int`, `result_bytes: int` | none | yes |
| `assistant_message` | `id: str`, `turn: int`, `scope: str \| None`, `request_ids: list[str]`, `content: str`, `interrupted: bool` | `scope` is null for root | yes |
| `turn_complete` | `id: str`, `turn: int`, `scope: str \| None`, `stop_reason: str` | `scope` is null for root | yes |
| `turn_error` | `id: str`, `turn: int`, `scope: str \| None`, `message: str` | `scope` is null for root | yes |
| `turn_interrupted` | `id: str`, `turn: int`, `scope: str \| None` | `scope` is null for root | yes |
| `model_switch` | `id: str`, `model_key: str`, `sent_at: str` | none | yes |
| `shell_command` | `id: str`, `command: str`, `exit_code: int`, `output: str` | `turn: int \| None`, `scope: str \| None` | yes |

All IDs are non-empty ULID strings. `turn`, `index`, `duration_ms`, token counts, and `result_bytes` are non-negative integers. `turn_iteration` is the string form `<turn>.<index>`. `status` is exactly one of `completed`, `interrupted`, `error`, or `no_usage`. `cost_usd` is a finite non-negative JSON number rounded to six decimal places. Canonical decoding rejects unknown fields and invalid scalar types without coercion. Invalid event records are never appended.

The connection-only `session_snapshot` uses the same flat top-level style but is not part of the persisted union. It contains current model/status data and `latest_event_id`. `status_updated` is a live-only flat event containing current git/status data.


The agent exposes:

```text
GET /events?after=<event_id>
```

The orchestrator exposes the corresponding:

```text
GET /sessions/{session_id}/events?after=<event_id>
```

The response is `application/x-ndjson`, containing the exact stored event lines after the cursor, in JSONL order. Omitting `after` means an initial attach and returns the complete canonical event log. Reconnect uses the last event ID actually applied by the client reducer; the snapshot's `latest_event_id` is informational and is never used as the initial replay cursor. An empty log returns `200` with an empty body. An unknown non-empty cursor returns `409` with a structured cursor-not-found error; the client must restart replay from the beginning.

The cursor-not-found body is:

```json
{"error":"cursor_not_found","cursor":"01J..."}
```

Success and empty responses use `Content-Type: application/x-ndjson`; errors use `application/json`. The orchestrator forwards only the `after` query parameter and relays the agent status, response body, and content type unchanged.

The WebSocket client subscribes before requesting replay. The agent registers the WebSocket for broadcasts before sending its snapshot. The client buffers live events while the HTTP replay is in progress and applies replay plus buffered live events through one event reducer, ignoring duplicate IDs.

The reducer renders assistant responses from persisted `assistant_message` records directly; `text_delta` events are live-only and never replayed. During live streaming a connected client MAY render incoming `text_delta` chunks into an in-progress message keyed by `request_id`/scope/turn; when the authoritative `assistant_message` arrives it replaces that in-progress render with the final content. On replay there is no in-progress stream to reconcile — the client renders each `assistant_message` as-is. A duplicate final event ID is ignored. The client does not optimistically render user input; it waits for the canonical `user_message` event, preventing duplicate user messages during replay.

On agent startup, the event log is checked before the session becomes ready. An empty file receives one `session_started` event with `schema_version: 1`. A non-empty file must begin with a valid canonical `session_started` record with `schema_version: 1`; otherwise startup fails with a legacy-session error and the agent does not accept clients. This prevents appending canonical records to a pre-029 `MessageEntry` log.


1. Allocate the turn and persist a `user_message` event.
2. Before each provider call, allocate the request ULID and capture `sent_at` immediately before invoking `LLMClient.stream()`.
3. Run one loop iteration while carrying that request context through text, tool, usage, and terminal events.
4. On provider stream completion, exception, or interruption, capture `duration_ms` and finalize exactly one `llm_request` record.
5. Populate actual usage when received; use zero numeric fields plus `no_usage`, `error`, or `interrupted` status when usage is unavailable.
6. Calculate `context_tokens` and `cost_usd` from the request's usage and model snapshot.
7. Append the `llm_request` event and all other replayable events in event order, then broadcast each exact serialized event.
8. Persist the final `assistant_message` with its ordered request IDs.
9. Persist and broadcast `turn_complete`.

The request lifecycle is therefore one logical record, not separate start/completion records:

```text
allocate request ID
  → capture sent_at
  → invoke provider
  → receive usage/response or failure
  → capture duration
  → finalize llm_request
```

The loop receives an injected request-context callback/factory so it can carry request IDs and timing without accessing persistence or WebSockets. The interface is:

```python
@dataclass(frozen=True)
class RequestContext:
    request_id: str
    sent_at: str

request_context_factory: Callable[[], RequestContext]
```

`run_loop()` calls the factory once per provider request before `_stream_once()`. `_stream_once()` carries the context through the worker call, captures duration in its `finally` path, and always returns a result even when the provider raises. The internal `_RequestResult` carries the context, duration, usage if received, stop reason, status, and error. `run_loop()` yields one internal `RequestFinished` event for every request before yielding its tool/lifecycle continuation; `RequestFinished` has `context`, `duration_ms`, `status`, `usage: Usage | None`, `stop_reason: str | None`, and `error: str | None`. The harness converts that event to the persisted `llm_request`. `RequestFinished` is internal-only and is never serialized or persisted.

For each root request, append order is explicit:

```text
iteration_start
iteration_start
text_delta*  (live broadcast only — NOT appended)
tool-use accumulation
llm_request
(tool_call → tool_result)*
```

For a normal turn, the full order is:

```text
user_message
[request sequence above, repeated per iteration]
assistant_message
turn_complete
```

For an error or interruption, `llm_request` is appended before `turn_error` or `turn_interrupted`; any partial assistant message follows the request record and precedes the terminal event. Every record is appended before its own broadcast, even though the finalized `llm_request` necessarily follows the streamed deltas it accounts for.


1. Reject switches during an active turn.
2. Replace the active model/client.
3. Persist and broadcast `model_switch` with the new model key.
4. Price future request events using the new model.

**Subagent**

1. Parent emits a subagent `tool_call`.
2. The tool's `tool_use_id` becomes child `scope`.
3. Child requests emit independent `llm_request` events.
4. Child tool activity uses the same scope.
5. Parent receives the subagent `tool_result`.

**Shell command**

1. The client executes the command through the existing shell path.
2. The agent receives the shell result through `/shell`.
3. The agent creates a `shell_command` event with command, output, exit code, turn, and scope.
4. The event is appended and broadcast through the canonical event path.

**Replay**

1. Client subscribes to live WebSocket events.
2. Agent registers the client for broadcasts before sending the connection snapshot.
3. Client requests `GET /events?after=<last_event_id>` through the orchestrator.
4. Agent reads the JSONL file in append order.
5. Client buffers live events while the finite NDJSON replay is read.
6. Client applies replayed and buffered events through the same reducer, deduplicating by event ID.
7. Client resumes direct live processing after replay completes.

### Error Handling and Edge Cases

- Provider returns no usage → finalize one `llm_request` with `status: "no_usage"`, zero numeric usage/cost fields, duration, and no fabricated token values.
- Provider fails before or during streaming → finalize one `llm_request` with `status: "error"`, duration, model key, partial actual usage if available, and error text; the parent turn also emits `turn_error`.
- Request is interrupted → finalize one `llm_request` with `status: "interrupted"`, duration, and any usage actually received; the parent turn emits `turn_interrupted`.
- Log append fails → do not broadcast the failed event; stop canonical emission for the affected turn, send a canonical `turn_error` only if that error event itself can be appended, otherwise close the live stream with a server error and leave the turn incomplete in the log; never emit a noncanonical error event.
- Duplicate event ID at append → treat it as an idempotent no-op only when the serialized event is byte-identical; reject the write if the same ID has different content.
- Duplicate replay/metrics event → ignore an already processed event ID.
- Unknown replay cursor → return HTTP 409 with a cursor-not-found error; the client retries from the beginning.
- Malformed replay line → log its line number, skip it, and continue with later valid lines; an unknown event tag is handled the same way and does not terminate the whole replay.
- Concurrent subagents → generate independent ULIDs and use scope/request IDs for attribution. Synchronous append calls on the single agent event loop cannot interleave bytes.
- Existing pre-029 session log → unsupported. The new reader accepts only canonical event logs; deployment requires stopping old sessions and starting fresh canonical sessions. No legacy conversion is implemented.

### Code Structure

- Replace the hand-written frozen dataclasses and manual `to_json`/`from_json` serializers in `shared/src/archie_shared/events.py` with canonical tagged `msgspec` event structs. (Note: `shared/session/log.py` already uses `msgspec.Struct`; only `events.py` is hand-rolled today.)
- Extend `shared/src/archie_shared/session/log.py` with canonical append and replay helpers, replacing the current `MessageEntry`/`MessageMetadata` structs.
- Add `agent/src/archie_agent/event_log.py` for event creation, request accounting, and append-before-broadcast coordination.
- Move `agent/src/archie_agent/tool_formatters.py` to `shared/src/archie_shared/` (e.g. `shared/tool_summaries.py`) so tool-call/result summaries are derived from canonical event data by any presentation surface (TUI now; web UI and orchestrator session viewer later). The agent stops computing summaries; `WireToolCall.input_summary` and `WireToolResult.summary` are removed from the wire schema.
- Replace message-specific persistence in `agent/src/archie_agent/harness.py`.
- Add canonical event replay to `agent/src/archie_agent/app.py`.
- Update `cli/src/archie_cli/tui/app.py` and `status.py` for canonical replay and authoritative costs.
- Update `orchestrator/src/archie_orchestrator/proxy.py` and `metrics.py` for transparent relay and idempotent ingestion.
- Update `plans/028-native-subagent-tool.md` to consume `scope` and canonical `llm_request` events.
- Replace affected tests under `tests/`.

### Patterns and Conventions

- Use `msgspec.Struct` tagged unions for shared canonical events.
- Generate ULIDs in the agent event factory.
- Use flat JSON records with a top-level `type`.
- Treat JSONL order as authoritative.
- Persist `cost_usd`, not pricing snapshots.
- Use `model_key` as the model/provider identifier.
- Keep `run_loop()` independent of persistence and transport.
- Persist before broadcasting.
- Use event IDs for replay cursors and metrics idempotency.

### Infrastructure and Deployment

- No new services.
- No new dependencies.
- No new environment variables.
- Existing session JSONL files are not migrated; canonical replay supports newly created canonical sessions only. Old sessions must be stopped and archived/recreated before rollout.
- Existing SQLite metrics storage is version-checked. A pre-canonical database is renamed to a `.legacy` file and a fresh canonical schema is created; historical metrics migration is out of scope.
- Docker boundaries remain unchanged.

### Non-Functional Concerns

- Reliability: append before broadcast so clients never observe an event absent from the log.
- Observability: every request records ID, model, status, duration, tokens, and cost.
- Replay correctness: event IDs and JSONL order provide replay and deduplication.
- Performance: synchronous append is the deliberate initial behavior; buffering/compaction is explicitly out of scope for this project and requires a follow-up plan if log growth or append latency becomes a problem.
- Data exposure: structured tool inputs/results are exposed to attached clients because this is a single-user local application; raw provider payloads are not persisted.

### Key Decisions

- Canonical event stream over separate message and usage logs: supports exact replay and removes divergent representations.
- Flat fields over `data`: reduces schema indirection and makes logs directly inspectable.
- ULIDs without a separate sequence field: one append-only writer makes JSONL order authoritative while ULIDs provide identity and idempotency.
- Tool-use scope over generic agent IDs: matches the existing subagent invocation relationship.
- Persisted cost over persisted pricing: keeps historical costs stable without per-record rate duplication.
- `msgspec` over hand-written serializers: already exists in shared and removes duplicated serialization logic.
- Harness enrichment over loop-owned records: preserves pure loop testing and centralizes session accounting.
- Tool summaries derived client-side from canonical data, formatter placed in `shared` (not the CLI): the agent no longer formats presentation strings; `WireToolCall.input_summary`/`WireToolResult.summary` are removed. The formatter lives in `shared` so the future web UI and orchestrator session viewer reuse identical summary logic. Verified there is no information loss — the raw `tool_result.content` is exactly the string the completion formatter already parses.
- Live-only `text_delta` (broadcast, not persisted): streaming UX is preserved live while the persisted log stays whole-message and replay needs no stream-to-final reconciliation; the full text is always recoverable from `assistant_message.content`. `text_delta` joins the existing live-only bucket alongside `session_snapshot` and `status_updated`.
- Full canonical-event rewrite over an isolated ledger fix: the model-switch repricing bug alone is a ~30-line `Session`/`record_usage` change (see M3), but a deliberate decision was made to build the canonical event stream as the shared accounting/replay/subagent foundation for plan 028 rather than patch the ledger in isolation. The isolated fix would leave the divergent event/log/metrics representations in place and require a second pass before 028.

## Milestones

### M1 — Canonical shared event schema and event-log primitives

**Approach**

Introduce the canonical event union and log primitives alongside the current event API so the repository remains buildable during M1. Update every current shared-event consumer and test to the canonical API before M1 completes; M2 then switches runtime harness emission and removes the legacy definitions. The old `MessageEntry` writer remains available only as an internal transition seam during M1 and is removed when M2 lands.

**Tasks**

- Define canonical event structs and the server-event union in `shared/events.py`.
- Define fields for every persisted/replayable tag: session start, messages, iteration, text, LLM requests, tools, lifecycle, model switches, and shell commands.
- Implement canonical serialization/deserialization.
- Add `append_event` and `read_events(after_id=None)` alongside the current writer.
- Add malformed-line handling and event-ID lookup behavior.
- Add strict field/type tests, including status/nullability and six-decimal cost precision.
- Update current agent, CLI, orchestrator, and test imports/consumers so the package-wide suite passes against the canonical event API.
- Leave old runtime message persistence in place only until M2 replaces the harness call sites.

**Edge Cases**

- Empty/missing canonical log → replay returns an empty list.
- Malformed line → log its line number and skip it.
- Unknown event type → log and skip the record during replay.
- Duplicate event ID → permit an identical append as an idempotent no-op; reject a conflicting payload.
- Pre-canonical first line → do not append; report a legacy-log error to the caller.

**Deliverable**

Shared code and all current package consumers build and test against the canonical event API, while runtime behavior remains on the old persistence path until M2.

**Verify**

`uv run pytest` with assertions for exact serialized equality, event unions, ULID IDs, ordered replay, malformed-line behavior, duplicate handling, legacy-log detection, and existing package consumers.

### M2 — Agent canonical emission and authoritative request ledger

**Approach**

Add the agent event factory and make `AgentHarness` emit canonical events for root-agent turns. The harness remains responsible for enriching pure loop events with model, turn/iteration, timing, cost, and IDs. Persist every replayable event before broadcasting it.

**Wiring**

- `run_loop()` continues to emit internal semantic events.
- `run_loop()` receives an injected request-context factory/callback that allocates a request ULID, captures `sent_at` immediately before `LLMClient.stream()`, and carries the context through one iteration.
- `AgentHarness` creates canonical records through `event_log.py`.
- `event_log.py` finalizes one `llm_request` record on provider completion, exception, or interruption.
- `event_log.py` appends the record and returns the serialized string.
- Harness broadcasts that exact string to all clients.

**Tasks**

- Initialize/validate the canonical session log with `session_started` before the agent reports readiness.
- Add the event factory and ULID generation.
- Replace `_persist_message`, `_persist_tool_call`, `_persist_tool_result`, and `_persist_assistant` with canonical event emission.
- Emit canonical `user_message`, `iteration_start`, `text_delta`, `tool_call`, `tool_result`, and turn-lifecycle events.
- Add request-context propagation through `_stream_once` without giving the loop persistence or WebSocket dependencies.
- Create one `llm_request` record per logical provider request using the request context.
- Capture `sent_at` immediately before the provider stream call and `duration_ms` in a `finally`-equivalent completion path.
- Finalize request status as `completed`, `interrupted`, `error`, or `no_usage`.
- Populate actual usage when received; use zero numeric fields when usage is unavailable.
- Calculate context tokens and cost from the request's model snapshot.
- Track request IDs for the final assistant message.
- Ensure every canonical append occurs before its broadcast.
- Preserve pure `run_loop()` tests and internal event semantics.

**Edge Cases**

- Usage arrives after provider `Done` → finalize the request only after the provider stream drains.
- Provider fails before yielding anything → emit one error request with duration and zero usage.
- No usage after a successful response → emit `no_usage` without fabricated cost.
- Error/interruption after partial usage → retain the actual partial usage and record status.
- Log write failure → do not broadcast the failed event.

**Deliverable**

A normal root-agent turn produces one canonical JSONL/WebSocket stream, including separate request records for every tool-loop iteration and immutable per-request costs.

**Verify**

`uv run pytest tests/test_harness.py tests/test_loop.py` plus new canonical-emission tests asserting log lines equal broadcast frames, request lifecycle fields are populated, and tool-loop requests are individually recorded.

### M3 — Model switching and context accounting

**Approach**

Replace current-model-based cumulative cost calculation with event-backed accounting. Model switches become canonical events, and every request retains the model key and cost calculated at request completion.

**Tasks**

- Refactor `Session` accounting so totals are based on immutable request records or an equivalent request ledger.
- Remove repricing of historical totals through `Session.model`.
- Add `context_tokens = input + cache_read + cache_write` to request events and in-memory accounting.
- Emit and persist `model_switch` events.
- Ensure switching remains forbidden during active turns.
- Add per-scope direct and inclusive cost aggregation helpers.
- Update model-switch tests and cost tests.

**Edge Cases**

- Switch before first turn → first request uses the new model.
- Switch after several turns → prior request costs remain unchanged.
- Tool-loop request pricing → every iteration uses the model active for that request.
- Child scope pricing → future subagent requests use their own model key and cost.

**Deliverable**

Model switching no longer changes historical cost, and context usage is represented by actual token counts rather than an estimated percentage.

**Verify**

`uv run pytest tests/test_model_switch.py tests/test_017_features.py tests/test_harness.py` with a model-switch session asserting request-by-request cost totals and context-token values.

### M4 — Canonical replay endpoint and TUI reconstruction

**Approach**

Replace the current in-memory `/history` reconciliation path with canonical event replay. The TUI uses one event handler for both live and replayed events and reconstructs conversation, model, usage, and cost state from the event stream.

**Wiring**

- Agent exposes `GET /events?after=<event_id>` as finite `application/x-ndjson` replay.
- Orchestrator proxies it at `GET /sessions/{session_id}/events?after=<event_id>`.
- The agent registers a WebSocket client before sending its `session_snapshot`.
- TUI subscribes to WebSocket events before requesting replay.
- TUI buffers live events during HTTP replay and deduplicates overlap by event ID.

**Tasks**

- Add the agent `GET /events` route with complete-log, empty-log, valid-cursor, and unknown-cursor behavior.
- Add the orchestrator replay proxy with query forwarding and error propagation.
- Include the latest event ID and current model/status data in the connection-only `session_snapshot`.
- Update `archie_cli.tui.app` to track the last event ID.
- Replace `/history` rendering with a canonical event reducer.
- Render canonical `user_message` events instead of optimistically rendering submitted text.
- Render `text_delta` live-only into an in-progress message keyed by `request_id`/scope/turn during streaming; drop that in-progress render when the authoritative `assistant_message` arrives. On replay, no `text_delta` events exist — render each `assistant_message` directly.
- Render `assistant_message` as the authoritative response; on live, it replaces any in-progress streamed render.
- Reconcile tool calls/results by `tool_use_id`.
- Restore cumulative tokens, costs, model state, and conversation on attach.
- Render structured tool inputs/results locally: the TUI calls the relocated shared `format_tool_pending(name, input)` on `tool_call` and `format_tool_complete(name, input, content, is_error)` on `tool_result`, correlating the two by `tool_use_id`. No Rich-formatted summary strings are read from the wire. (Verified no information loss: `format_tool_pending` needs only `name` + `input`, both on `tool_call`; `format_tool_complete` additionally parses the raw result string, which is exactly `tool_result.content`. `exec` remains client-rendered from structured data via `ToolEntry.complete()`.)
- Remove the agent-side `_pending_tools` display correlation (`harness.py`) and the `format_tool_*` call sites; input-dict correlation now happens client-side.
- Remove TUI cost calculation from mutable model rates.
- Preserve live streaming behavior during replay.

**Edge Cases**

- Empty session → snapshot followed by a `200` empty replay body.
- Unknown cursor → agent returns `409` cursor-not-found; client retries from the beginning.
- Client attaches during an active turn → no events are lost between subscription and replay.
- Duplicate live/replayed event → apply once by event ID.
- Reconnect after partial turn → replay all persisted events and resume live delivery.
- Final assistant message → live render replaces any in-progress streamed render; replay renders `assistant_message` directly (no persisted deltas to reconcile).

**Deliverable**

A newly attached TUI reconstructs the same conversation and accounting state as an already-connected TUI using the canonical event stream.

**Verify**

`uv run pytest tests/test_cli_attach.py tests/test_harness.py` plus new replay integration tests covering complete replay, unknown cursors, mid-turn attach, duplicate overlap, live-stream-to-final-message replacement (live only), replay rendering from `assistant_message` alone, restored cost state, and client-side tool-summary reconstruction (pending from `tool_call.input`, complete from `tool_result.content`/`is_error`) producing the same summaries the agent formerly sent.

### M5 — Orchestrator metrics from canonical request events

**Approach**

Change metrics ingestion to consume `llm_request` events directly. Remove mutable session-rate reconstruction for cost calculation and make ingestion idempotent by event ID.

**Tasks**

- Set `METRICS_SCHEMA_VERSION = 2` via `PRAGMA user_version`.
- Create the canonical table with this DDL:

```sql
CREATE TABLE requests (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    turn_iteration TEXT NOT NULL,
    scope TEXT,
    model_key TEXT NOT NULL,
    status TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    context_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    duration_ms INTEGER NOT NULL,
    UNIQUE (session_id, event_id)
);
```

- Add indexes on `(session_id)`, `(timestamp)`, and `(model_key)`.
- On startup, `user_version = 0` or any version other than `2` causes the existing database to be closed and renamed to `<path>.legacy.<UTC timestamp>`, adding `-1`, `-2`, and so on until the destination is unused; then a fresh version-2 database is created and `user_version` is set to `2` in the same initialization path.
- Parse canonical `llm_request` records from the proxy stream.
- Change the proxy metrics marker set from legacy `usage`/rate events to canonical `llm_request` frames and enqueue only live WebSocket relay frames; HTTP `/events` replay is not sent to metrics.
- Store persisted `cost_usd` without recalculating from current rates.
- Deduplicate repeated request events using the unique `(session_id, event_id)` key; identical duplicates are no-ops and conflicting duplicates are logged as errors.
- Handle replayed events and multiple attached clients.
- Remove obsolete dependence on `session_info` and `model_switched` pricing rates.
- Update all metrics queries/endpoints to use `event_id`, `turn_iteration`, `scope`, `model_key`, `status`, `context_tokens`, and `cost_usd`.
- Update metrics endpoints/tests.

**Edge Cases**

- Same event observed from multiple clients → one database row due to the unique key.
- Event replayed after restart → existing row remains unchanged.
- Error/no-usage request → status is retained with zero or actual available usage and no fabricated cost.
- Unknown event type → relay unaffected and metrics skips it.

**Deliverable**

Orchestrator metrics report the same request costs as the agent ledger regardless of client count, replay, or model switches.

**Verify**

`uv run pytest tests/test_orchestrator_metrics.py` plus tests feeding duplicate and replayed canonical request events and asserting one stored row per request ID.

### M6 — Subagent scope contract and downstream-plan alignment

**Approach**

Make the canonical event model ready for subagents without implementing the full subagent tool. Validate that scoped request attribution works independently of the root agent and update plan 028 to use the new contract.

**Tasks**

- Add scope fields to all applicable canonical events.
- Add tests for root, child, and nested-child scopes.
- Add direct and inclusive cost aggregation by scope.
- Verify child model keys and costs remain independent of the parent model.
- Produce the **subagent scope contract**: a concise spec (in this plan / a short contract doc) defining how subagent activity maps onto canonical events \u2014 child `scope` = launching `tool_use_id`, one `llm_request` per child provider request, `(scope, turn_iteration, request_id)` identity, and direct/inclusive cost aggregation by scope. This milestone delivers the *contract*, not edits to 028.
- Make 028 explicitly depend on 029 M6 and prevent subagent implementation from starting until the canonical event contract is available.
- Add a fake scoped event producer test representing a future subagent.

**Deferred to a discrete 028-revision step (not part of M6):** the actual rewrite of `plans/028-native-subagent-tool.md` to consume this contract. 028 is a fully-reviewed 12-milestone plan; revising it in place from within 029 risks drift. After M6 lands the contract, perform a deliberate 028 revision as its own reviewed change, covering: Context paragraphs (flat `turn_index`/`parent_tool_use_id` wire events \u2192 canonical `scope`/`turn_iteration`); resolved-decisions items 2 (nested wire-event design) and 5 (`Session.record_usage()`/transcript cost); Observability (attribution via one canonical `llm_request` per child request + persisted `cost_usd`); child-streaming identity (`(parent_tool_use_id, index)` \u2192 `(scope, turn_iteration, request_id)`); session-persistence (drop `MessageEntry.parent_tool_use_id`/`agent_id`/index; require canonical `tool_call`/`tool_result`/`assistant_message`/`llm_request` \u2014 note `text_delta` is live-only and not persisted); Design (frozen dataclasses/manual serializers/`_SERVER_EVENT_TYPES`/protocol-version bump \u2192 tagged `msgspec` union); `shared/session/log.py` (attributed `MessageEntry` + locking \u2192 canonical append/replay); Reuse (child `Session.record_usage()` \u2192 event-factory emission); Deviations (remove separate nested-wire-event and session-log-schema changes); and Milestones 8\u201312 (event names/fields/persistence assertions/replay/command correlation \u2192 canonical scope/request contract).

**Edge Cases**

- Two children use the same local `turn_iteration` → scope keeps records distinct.
- Nested child scope → parent tool-call chain remains reconstructable.
- Child fails before usage → error request remains attributable to the child scope.

**Deliverable**

The event stream can attribute and aggregate nested subagent activity using only launching tool IDs and canonical request events.

**Verify**

`uv run pytest tests/test_session_events.py tests/test_subagent_accounting.py` with root, child, nested-child, direct-cost, and inclusive-cost assertions.

### M7 — Cleanup, documentation, and full validation

**Approach**

Remove obsolete accounting paths, update architecture documentation, and validate all packages together.

**Tasks**

- Remove unused `MessageMetadata` cost/accounting paths.
- Remove old TUI-local rate accumulation.
- Remove obsolete orchestrator rate tracking.
- Update `docs/architecture.md`, `CONTRIBUTING.md`, and the relevant roadmap/plan references.
- Note in plan 028 the dependency on 029 (M6 contract) and that a full 028 revision is a separate, reviewed step; do not perform that rewrite inline here.
- Add fixtures/helpers for canonical event streams.
- Run formatting, linting, and the full test suite.

**Deliverable**

The repository has one documented event/accounting model with no active legacy cost path.

**Verify**

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## Sequencing

```text
M1 → M2 → M3 → M4
          └→ M5
M3 + M4 → M6
M1–M6 → M7
```

M1 establishes the shared schema. M2 makes the agent authoritative. M3 fixes model-switch and context accounting. M4 makes replay and TUI state canonical. M5 can proceed after M2 but depends on the canonical request event. M6 depends on the accounting and scope contracts. M7 follows all implementation slices.

## Status

| Milestone | Spec | Status |
|---|---|---|
| M1 — Canonical shared event schema and event-log primitives | Not yet split | Planned |
| M2 — Agent canonical emission and authoritative request ledger | Not yet split | Planned |
| M3 — Model switching and context accounting | Not yet split | Planned |
| M4 — Canonical replay endpoint and TUI reconstruction | Not yet split | Planned |
| M5 — Orchestrator metrics from canonical request events | Not yet split | Planned |
| M6 — Subagent scope contract and downstream-plan alignment | Not yet split | Planned |
| M7 — Cleanup, documentation, and full validation | Not yet split | Planned |

## Open Questions

The canonical event and accounting design is resolved. Notable settled decisions: `text_delta` is live-only (broadcast, not persisted) so replay renders from `assistant_message` alone; the persisted-vs-broadcast byte-equivalence invariant applies only to persisted event types. The subagent implementation plan (028) must consume this schema (via the M6 contract) rather than introduce a separate attribution model; the actual 028 rewrite is a deliberate follow-up step, not part of 029.

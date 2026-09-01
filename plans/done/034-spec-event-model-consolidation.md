# 034 — Event Model Consolidation and Session Coordinator

Type: spec. Status: planned (Phase 4 review pending).

## Objective

Make the canonical event schema the single public contract for every server→client message — live
and replay — delivered through one session-level coordinator that orders, persists, and broadcasts
each event. This removes the parallel wire schema, the dual-envelope demultiplexing on the client,
and the forked root/child turn logic, and makes multi-client attach/reconnect deterministic while
keeping the persisted log correctly structured for replay and analysis. (Event *identity* is
assigned during construction by `EventFactory`; the coordinator assigns *order* and owns
persistence and broadcast.)

## Context

### Why now

Plan 029 introduced the canonical event stream for **persistence, replay, metrics, accounting,
subagent scope, and the shared tool-summary formatter** — but left the root agent's **live
broadcast** on the legacy wire schema and never deleted the wire dataclasses. Verified in the
current tree:

- `shared/src/archie_shared/events.py` still defines the wire dataclasses (`SessionInfo`,
  `IterationStart`, `TextDelta`, `Usage`, `TurnComplete`, `TurnInterrupted`, `TurnError`,
  `ToolCall`, `ToolResult`, `ModelSwitched`, `StatusUpdated`, `SessionSnapshot`), the `ServerEvent`
  union, `serialize_event`/`deserialize_event`, and the commands.
- `shared/src/archie_shared/canonical_events.py` defines the `msgspec` canonical union used for
  persistence, replay, and child broadcast. It has **no** live-only session frames (no
  `session_snapshot`, no `status_updated`), **no** `usage` type, and its `ModelSwitch` carries only
  `model_key`/`sent_at` (the wire `ModelSwitched` also carried `model_name`/`supports_cache`).
- `agent/harness.py::handle_message` persists canonical events via `EventFactory` **and separately
  broadcasts wire twins**; `llm_request` is the one event already broadcast as canonical raw. The
  turn-active rejection broadcasts a **wire** `TurnError` to all clients (not persisted).
- `agent/agents.py::run_child` re-implements the reducer and broadcasts **raw canonical**; its
  outer `except` returns an error string without persisting a child terminal event.
- `agent/app.py::_handle_model_switch` sends a **wire** `TurnError` (not persisted) to reject
  switches during an active turn or unknown models; `stream` dispatches `handle_message` as a
  detached task with no reference to the submitting socket. `AgentHarness.__init__` appends
  `session_started` synchronously; `switch_model` is synchronous.
- `cli/tui/app.py` runs two reducers — `_handle_event` (wire) and `_render_canonical` (canonical);
  `cli/ws_client.py::receive()` string-sniffs to demultiplex the two envelope shapes.
- `orchestrator/metrics.py` ingests relayed `llm_request` frames into a `requests` table keyed on a
  `turn_iteration TEXT` column; `orchestrator/proxy.py` enqueues frames by an `"type":"llm_request"`
  marker.

This finishes 029's unfinished half and adds the coordinator, reconnection, and correctness pieces
a second client (or an unreliable connection) requires. Full analysis:
`docs/architecture-analysis-revised.md`.

### Prior decisions honoured

- Single-user, local-first. No multi-user authz/presence/locking is in scope.
- 029's shape stands: NDJSON, tagged union, ULID `id`, append-order = replay-order, one
  `llm_request` ledger event, `text_delta` live-only.
- **Pre-029 logs are unsupported** (029 decision). Existing logs are canonical (029+) and may
  contain legacy `role="shell"` `MessageEntry` lines (the only remaining `MessageEntry` writer).
  Migration targets exactly those two shapes; it does not convert hypothetical user/assistant/tool
  `MessageEntry` lines, which supported logs do not contain.
- Deferred (out of scope): multi-client command *delivery* semantics (ids/ack/idempotency on
  `message`); the `SessionBackend` extraction (029 M6 provides its scope contract; extract when a
  second backend is real); the auth/same-origin boundary; the web client; ACP; Gateway.

### Delivery note

A functioning system is **not** required between milestones. Intentional breakage is acceptable
when the milestone names it and a later milestone restores it. The intentional windows: M1 breaks
the live TUI (restored in M2); M3–M6 leave existing logs undecodable until the converter (M7);
`/shell` bypasses the coordinator until M6. The persisted log must remain **uncorrupted** across
every window (no spurious or duplicate persisted events), even where the live view is broken.

---

## Requirements

### Single public event contract

- MUST use one public event schema for all server→client messages, live and replay, with no wire
  event types remaining.
  - AC: `shared/events.py` retains no server-event dataclasses, no `ServerEvent` union, and no
    `serialize_event`/`deserialize_event`.
  - AC: every wire server type has a defined canonical disposition (see Data Model): `SessionInfo` +
    `SessionSnapshot` → one live-only `handshake`; `StatusUpdated` → live-only `status_updated`;
    `Usage` → folded into `llm_request`; `ModelSwitched` → canonical `model_switch` (client derives
    display name / cache support from the model catalog by `model_key`); `TurnError`'s *rejection*
    role → live-only `error_notice`; all turn/tool events → their existing canonical types.
  - AC: `EventFactory` emits no wire twin.
- MUST decode every server frame through a single decoder with no envelope-shape sniffing.
  - AC: `cli/ws_client.py::receive()` decodes via one path (`decode_event`).
  - AC: a canonical `tool_call` whose structured `input` contains a `data` key is decoded and
    rendered correctly.
- MUST classify each public event as persisted or live-only in one place.
  - AC: the live-only set is exactly `text_delta`, `handshake`, `status_updated`, `error_notice`;
    all other public events are persisted.
- MUST bump `PROTOCOL_VERSION` and provide no dual-protocol compatibility path.
  - AC: `PROTOCOL_VERSION` is incremented; no code path speaks the old wire protocol.

### Session coordinator, ordering, and persistence

- MUST route every agent-produced event (root and child) through one session-level coordinator that
  orders, persists (for persisted events), and broadcasts.
  - AC: with the root and ≥2 concurrent children producing events, the order of appended lines
    equals the per-client delivery order of frames.
  - AC: `EventFactory` performs no file writes; construction and persistence are separated.
  - AC: `session_started` and `model_switch` are recorded/broadcast through the coordinator.
- MUST persist a *persisted* event before broadcasting it; live-only events are broadcast without
  being appended.
  - AC: every *persisted* frame a client receives corresponds to a line already in the log; a
    broadcast failure does not remove or skip the persisted line.
  - AC: `text_delta`, `handshake`, `status_updated`, and `error_notice` are broadcast and never
    appended.
- MUST NOT allow a slow or stalled client to block persistence or other clients.
  - AC: a client whose socket never drains does not delay appends or other clients' delivery; it is
    disconnected once its buffer exceeds a bound (default 1024 queued frames).
  - Why: the coordinator must not hold its ordering path across per-client network sends.
- MUST detect duplicate appends using append-side state rather than re-reading the whole log.
  - AC: appending N events performs id-duplicate checking in O(N) total (no full-log reparse per
    append).
  - AC: an identical duplicate (same id, same bytes) is a no-op; the same id with different bytes
    raises a distinct conflict error.

### Event identity and log format

- MUST identify turns and iterations with structured integer fields, not a composite string.
  - AC: no persisted event carries a `"2.1"`-style `turn_iteration` string; events carry integer
    `turn` and `iteration`.
  - AC: request/tool identity is `(scope, turn, iteration, request_id)`.
  - AC: the optional `assistant_message.turn_iteration` back-compat field is removed.
- MUST NOT add a universal event-creation timestamp; retain existing domain timestamps.
  - AC: `llm_request.sent_at`, `session_started.sent_at`, and `model_switch.sent_at` remain; no
    other event type gains a timestamp field.
  - AC: the documented spec describes deriving event-creation time from the ULID `id`.
- MUST NOT change the log's structural shape (NDJSON, one tagged union, ULID `id`, append-order =
  replay-order, single `llm_request` ledger event).

### Connect handshake and reconnection

- MUST send exactly one connect frame carrying only connection metadata not derivable from the
  event stream.
  - AC: `SessionSnapshot`/`SessionInfo` are removed; on connect the agent sends one live-only
    `handshake` (`protocol_version`, `session_id`, `model_key`) followed by one `status_updated`.
- MUST surface a protocol-version mismatch at connect.
  - AC: a client whose `PROTOCOL_VERSION` is lower than the server's shows a mismatch warning.
- MUST reconstruct conversation and accounting on attach and reconnect without duplication.
  - AC: a client reconnecting after a completed turn shows no duplicate conversation entries and no
    doubled accounting.
  - AC: the client's replay cursor is its last-applied **persisted** event id; live-only events
    never advance it (so `/events?after=` always references a logged id).
  - AC: two clients attached at once display the same conversation and accounting.

### Accounting

- MUST derive client cumulative accounting solely by summing `llm_request` events, deduplicated by
  `id`.
  - AC: totals come only from summed `llm_request` events; no connect frame supplies authoritative
    totals.
  - AC: switching models does not change historical totals.

### Multi-client correctness

- MUST broadcast `user_message` to all connected clients.
  - AC: a second attached client renders the initiating prompt, not only the response.
- MUST accept at most one active turn via an atomic check-and-set and reject a concurrent submission
  only to the submitting client.
  - AC: two simultaneous submissions cannot both be accepted.
  - AC: the rejected client receives a live-only `error_notice`; other clients' active turn display
    is unaffected; no rejection is persisted.
- MUST reject model switches (active turn, unknown model) to the submitting client only, without
  persisting the rejection.
  - AC: the rejection is an `error_notice` on the submitting socket; nothing is appended.

### Terminal-event guarantee

- MUST persist exactly one terminal event per accepted turn (root and child) for every failure
  other than a failure of the append path itself.
  - AC: an exception in the harness/child body, event construction, or `user_message` handling
    still yields exactly one terminal event (`turn_complete`/`turn_error`/`turn_interrupted`) in the
    log and clears turn-active state.
  - AC: no accepted turn leaves turn-active state set.
- MUST clear turn state and emit a best-effort live `error_notice` when the append path itself
  fails.
  - AC: on an injected append failure, turn-active state is cleared and an `error_notice` is
    broadcast; the process neither crashes nor hangs. (A durable terminal event is necessarily
    absent — storage is the thing that failed.)
  - AC: the append-failure error is a distinct type from the duplicate-id conflict error.

### Shell commands

- MUST persist shell commands as canonical events.
  - AC: a command posted to `/shell` appears in `/events` as a `shell_command` and renders on
    replay.
  - AC: the legacy `MessageEntry` shell writer is removed.

### Migration and metrics

- MUST provide a one-shot, host-side migration that converts existing session logs to the new spec
  without losing identity or order.
  - AC: the migration is a CLI command intended to run while sessions and the orchestrator are
    stopped (no concurrent log or metrics writer).
  - AC: it converts old-identity canonical lines (`"2.1"` → integer `turn`/`iteration`) and legacy
    `role="shell"` `MessageEntry` lines → `shell_command`, preserving every event's ULID `id` and
    append order.
  - AC: a log already at `schema_version == 2` is skipped (idempotent re-run).
  - AC: each log is rewritten atomically (temp file + rename) and the original is retained as
    `.legacy`; unrecognised/malformed lines are copied through verbatim with a warning.
  - AC: if a rewrite fails mid-file, the original log is unchanged (temp discarded).
- MUST reset the metrics index to the new schema and backfill it from migrated logs.
  - AC: metrics rows use integer `turn`/`iteration` (not `turn_iteration`) and dedup by
    `(session_id, event_id)`.
  - AC: backfill runs after log migration; a failed backfill leaves migrated logs intact and is
    safe to re-run.

### Documentation

- MUST document the finalised event spec.
  - AC: `docs/architecture.md` (or a referenced event-spec doc) lists each event type with its
    fields, marks persisted vs live-only, and describes deriving creation time from the ULID `id`.

---

## Technical Design

### Overview

The canonical `msgspec` union in `shared/canonical_events.py` becomes the sole public event
contract, extended with the live-only frames needed to absorb the wire types (`handshake`,
`status_updated`, `error_notice`) and with `Usage` folded into `llm_request`. The legacy wire schema
in `shared/events.py` is deleted and the client's envelope-sniffing demux removed. A new
session-global coordinator (`SessionEventBus`) owns the client registry, an ordering step, and
per-client send queues; `EventFactory` is reduced to pure construction. Root and child turn code
emit canonical events through the coordinator, so per-client delivery order equals append order. The
TUI collapses its two reducers into one canonical handler (live + replay, deduped by `id`) and sums
`llm_request` for accounting. A one-shot host CLI migrates existing logs and rebuilds metrics.

### Architecture

- **agent** owns event identity (via `EventFactory`), ordering, persistence, and broadcast (via
  `SessionEventBus`). The pure loop is unchanged.
- **shared** owns the single canonical schema and the log module.
- **orchestrator** relays canonical frames unchanged and ingests `llm_request` frames for metrics
  (marker-based; new integer columns).
- **cli** consumes one schema through one reducer; owns its replay cursor and accounting
  accumulation; hosts the migration command.
- Event flow (per persisted event): producer → `EventFactory` builds the event → coordinator orders
  (append the line, update the id index) → enqueues the serialized frame to each client's send queue
  → orchestrator relays → client decodes + reduces. Live-only events skip the append but use the
  same ordered enqueue. Targeted frames (connect `handshake`/`status_updated`, and `error_notice`
  rejections to one client) are enqueued to that one client's queue via `send_to`, preserving
  per-client order — never sent directly, which would race the sender task.

### Components

**`SessionEventBus`** (new, `agent/src/archie_agent/session_bus.py`)
- Responsibility: the single ordered sink and client registry.
- API (every method enqueues to per-client queues; nothing awaits a socket send under the ordering
  lock):
  - `append(event) -> str` (sync): persist a persisted event only (no broadcast); used for
    `session_started` at construction when there are no clients and no running-loop context. Raises
    `LogAppendError` on write failure and `EventIdConflict` on a same-id/different-bytes duplicate.
  - `async publish(event) -> str`: under the ordering lock, `append` the persisted event then enqueue
    its serialized form to every client queue; returns the line. Propagates `LogAppendError`/
    `EventIdConflict` (nothing enqueued if append fails).
  - `async broadcast(event)`: under the ordering lock, enqueue a **live-only** event to every client
    queue (no append). Used for `text_delta` and the best-effort storage-failure `error_notice`.
  - `async broadcast_serialized(line: str)`: enqueue an already-serialized string to every client
    queue (no append). Transitional only — lets M1's retained wire rejection/status frames ride the
    same per-client queues after the client registry moves to the bus; removed in M2.
  - `async send_to(ws, event)`: enqueue a live-only event to **one** client's queue, preserving its
    order relative to that client's other frames. Used for the connect `handshake` + initial
    `status_updated` and for targeted `error_notice` rejections (turn-active, model-switch). A direct
    `send_text` is **not** used for these — it would race the client's sender task and cannot
    guarantee the frame precedes queued live events.
  - `add_client(ws)` / `discard_client(ws)`: register/unregister a client, creating/cancelling its
    bounded `asyncio.Queue` (capacity 1024 frames) and a dedicated sender task.
- State: `dict[WebSocket, asyncio.Queue]` (per-client send queues) + sender tasks; an
  `asyncio.Lock` (ordering); an in-memory `dict[str, str]` id→line index seeded from the log at
  construction (idempotency/conflict detection without reparsing).
- Ordering vs. sends: the lock is held only for append + enqueue (fast, in-memory); network
  `send_text` happens in each client's sender task draining its queue in order, so a stalled client
  backs up only its own bounded queue. On overflow the bus disconnects that client.

**`EventFactory`** (`agent/event_log.py`, modified): drop all `append_event` calls; each method
returns the built canonical event. Keeps id/scope/model/cost construction. No filesystem access.

**`AgentHarness`** (`agent/harness.py`, modified): emits canonical events through the coordinator;
owns the turn lifecycle in one guarded region; exposes `try_begin_turn() -> bool` (atomic
check-and-set of `_turn_active`, called from `stream`). `clients` moves to the bus.

**`run_child`** (`agent/agents.py`, modified): emits through the coordinator; its outer `except`
publishes a scoped child terminal event.

**TUI** (`cli/tui/app.py`, `cli/ws_client.py`, modified): one reducer for live + replay deduped by
`id`; single decode path; accounting summed from `llm_request`; consumes `handshake`/`status_updated`
and derives model display name / cache support from the catalog by `model_key`; owns the replay
cursor (last-applied persisted id).

**Metrics** (`orchestrator/metrics.py`, modified): schema v-bump; integer `turn`/`iteration` columns
replace `turn_iteration TEXT`; ingestion mechanism unchanged; add a public
`reset_and_backfill(db_path, session_log_paths)` (promoting today's private `MetricsWriter`
archive/create/ingest logic) for the migration command.

**Migration command** (new, `cli` + a `shared`/`agent` migration module): host-side one-shot log
converter + metrics rebuild (see Data Flow / Error Handling).

### Data Model

Canonical events (`shared/canonical_events.py`):

- Identity change: replace `turn_iteration: str` on `iteration_start`/`text_delta`/`llm_request`/
  `tool_call`/`tool_result` with `turn: int` + `iteration: int` (fold `iteration_start.index` into
  `iteration`); remove `assistant_message.turn_iteration`. Bump `session_started.schema_version` to
  `2`.
- `Usage` fold: no public `usage` event. The internal loop `Usage` remains internal (feeds the
  harness's in-memory running totals); clients derive live context tokens/percentage from
  `llm_request` (which already carries `context_tokens` and `model_key`) plus the catalog.
- `model_switch` stays persisted with `model_key`/`sent_at`; clients map `model_key` → display name
  and cache support via `shared/models`. No `model_name`/`supports_cache` fields added.
- New live-only structs (not in `PersistedEvent`):
  - `handshake`: `protocol_version: int`, `session_id: str`, `model_key: str` (the catalog key, not
    a display name — the client derives display name / cache support from the catalog by `model_key`,
    exactly as for `model_switch`).
  - `status_updated`: `git_branch: str`.
  - `error_notice`: `kind: str` (e.g. `turn_active`, `unknown_model`, `switch_during_turn`,
    `storage_error`), `message: str`.
- `scope`/`subagent_index` unchanged.

Persisted vs live-only is expressed structurally: `PersistedEvent` is the persisted subset;
the live-only set (`text_delta`, `handshake`, `status_updated`, `error_notice`) is excluded from it.

Append index (bus): in-memory `dict[str, str]` (id→exact line), seeded once from the log at
construction. `shared/session/log.py` gains a stateless line append (write + newline, no reparse);
duplicate/conflict logic moves to the bus using the index.

Metrics `requests` table: replace `turn_iteration TEXT NOT NULL` with `turn INTEGER NOT NULL`,
`iteration INTEGER NOT NULL`; keep `UNIQUE(session_id, event_id)`; bump schema version (old DB reset
to `.legacy.<ts>`, per the existing 029 pattern).

### Data Flow

**Normal root turn** — `stream` receives a `message`; calls `try_begin_turn()` (atomic). If accepted,
it dispatches `handle_message` as a task. Inside the guarded region, the harness publishes
`user_message` (persist + broadcast), then per loop event builds a canonical event and
`publish`es it (`text_delta` via `broadcast`); ends with `assistant_message` then `turn_complete`.

**Child turn** — `run_child` publishes each event through the coordinator; concurrent children
serialize through the ordering lock, so frames interleave in one order equal to append order.

**Attach / reconnect** — as one critical section under the ordering lock, the agent `add_client`s
the socket and enqueues `handshake` then the initial `status_updated` to it via `send_to`, so no
concurrent `publish`/`broadcast` can place a live frame in the new client's queue before the
handshake or between the handshake and the initial status.
Client subscribes (buffering), requests `/events?after=<cursor>` (`cursor` = last-applied persisted
id; absent on fresh attach → full replay), renders replay through the one reducer, flushes buffered
live frames deduped by `id`, and sums `llm_request` for accounting.

**Concurrent submit** — a second `message` while a turn is active: `try_begin_turn()` returns False;
`stream` sends an `error_notice` (`kind=turn_active`) to the submitting socket via `bus.send_to`.
Other clients unaffected.

**Model-switch rejection** — `_handle_model_switch` sends an `error_notice` to the submitting socket
via `bus.send_to` for an active turn or unknown model; nothing appended.

**Append-failure (storage)** — `publish` raises `LogAppendError`; the turn's outermost `finally`
clears `_turn_active` and calls `bus.broadcast(error_notice(kind=storage_error, ...))`
(broadcast-only, non-persisted); the turn is left terminally incomplete in the log.

**Migration** — the host CLI iterates session logs: for each not at `schema_version==2`, copy the
original to `<name>.legacy` (refuse if a `.legacy` already exists unless `--force`), write the
migrated output to `<name>.tmp` in the same directory, `fsync`, then atomically rename `<name>.tmp`
over the original. After all logs migrate, reset `metrics.db` and backfill by re-reading migrated
logs' `llm_request` records.

### Error Handling & Edge Cases

- **Non-storage turn failure** (body/construction/`user_message` exception, append path healthy):
  the outermost `finally` publishes exactly one terminal event (`turn_error` unless a terminal was
  already emitted) and clears `_turn_active`.
- **Storage/append failure:** `publish`/`append` raise `LogAppendError`; the `finally` clears
  `_turn_active` and broadcasts an `error_notice`; no terminal event is persisted (irreducible).
- **Duplicate-id append:** identical bytes → no-op; different bytes → `EventIdConflict` (distinct
  from `LogAppendError`). Both are distinct from arbitrary construction exceptions.
- **Concurrent turn acceptance:** `try_begin_turn()` is a synchronous check-and-set with no `await`
  between check and set, so two `stream` coroutines cannot both accept.
- **Slow/stalled client:** its send queue fills to the bound; the bus disconnects it
  (`discard_client` + close) without blocking the ordering lock, persistence, or other clients.
- **Replay cursor is a live-only id:** impossible — the client only advances the cursor on persisted
  events, so `/events?after=` always references a logged id (avoids 409).
- **Unknown cursor (rotated/absent):** agent returns 409; the client resets *all* replay-derived
  state together — clears the rendered conversation, `_seen_event_ids`, `_seen_accounted_ids`, and
  cumulative totals — then re-derives everything from a full replay. (Clearing the seen-id sets
  without also clearing the conversation and totals would duplicate the conversation while
  suppressing the re-summed accounting.)
- **Adversarial `data` key in `tool_call.input`:** decoded via the single path.
- **Migration — commit order:** copy original → `.legacy`, write `.tmp`, `fsync`, atomic-rename
  `.tmp` over the original. The original is replaced only after a complete `.tmp` exists, so a crash
  leaves either the untouched original (possibly with a stale `.tmp`/`.legacy`) or the fully migrated
  file — never a partial original.
- **Migration — `.legacy` already exists:** a prior run was interrupted; skip with a warning unless
  `--force` is given (then overwrite). Never silently clobber a backup.
- **Migration — already migrated (`schema_version==2`):** skip (no re-backup).
- **Migration — malformed/unrecognised line:** copy through verbatim with a warning (the `.legacy`
  backup is the safety net; never silently drop content).
- **Migration — rewrite fails mid-file:** discard the `.tmp`; the original is untouched; re-run is
  safe (the aborted run's `.legacy` triggers the collision rule above).
- **Migration — backfill fails after logs migrated:** logs remain valid; metrics rebuild by
  re-running the backfill; log migration and backfill are independently idempotent.

### Code Structure

- New: `agent/src/archie_agent/session_bus.py` (coordinator), a migration module (e.g.
  `shared/src/archie_shared/session/migrate.py`) with a self-contained legacy decoder for
  `role="shell"` `MessageEntry` lines, and a `migrate-sessions` `@main.command()` in
  `cli/src/archie_cli/cli.py` invoking it (+ a public `reset_and_backfill` in `orchestrator/metrics.py`).
- Modify: `agent/event_log.py` (remove appends), `agent/harness.py` (publish via bus; guarded
  lifecycle; `try_begin_turn`; move `clients`; `session_started` via sync `append`),
  `agent/agents.py` (publish via bus; child terminal event), `agent/app.py` (handshake +
  `status_updated` on connect; `try_begin_turn` in `stream`; `error_notice` rejections; register on
  the bus; `/shell` → canonical in M6; `model_switch` published from the async handler),
  `shared/canonical_events.py` (identity fields, live-only structs), `shared/events.py` (delete wire
  schema), `shared/session/log.py` (stateless append; index moves to the bus; remove the
  `MessageEntry` writer in M6), `cli/ws_client.py` (single decode path), `cli/tui/app.py` (one
  reducer; accounting; cursor; handshake; model-name/cache derivation), `orchestrator/metrics.py`
  (integer columns + schema bump).
- Follow existing patterns: tagged `msgspec` structs with `forbid_unknown_fields=True`
  (`canonical_events.py`); ULIDs in `EventFactory`; persist-before-broadcast; behaviour-first tests
  under `tests/` (`test_harness.py`, `test_ws_integration.py`, `test_canonical_session_events.py`).

### Patterns and Conventions

Established codebase conventions apply. New: typed exceptions `LogAppendError` and `EventIdConflict`
in the log/bus module; the bus API (`append`/`publish`/`broadcast`/`broadcast_serialized`/`send_to`)
is the only way to emit public events from the agent.

### Infrastructure and Deployment

- No new dependencies, services, or environment variables.
- `PROTOCOL_VERSION` bump; client and agent upgraded together; running sessions restarted.
- Migration is a host CLI command run once at upgrade with sessions/orchestrator stopped; it backs
  up logs to `.legacy` and resets/backfills `metrics.db` (old DB → `.legacy.<ts>`).

### Non-Functional Concerns

- **Reliability:** persist-before-broadcast preserves the "clients never see an unlogged persisted
  event" invariant; the terminal-event guarantee is scoped by failure class; a stalled client is
  isolated by its bounded queue rather than wedging the agent.
- **Performance:** append becomes O(1) amortised via the id index (removes the per-append full-log
  reparse — observable as long-session appends no longer slowing and not risking keepalive stalls).
- **Observability:** shell commands become visible in `/events`; every turn is answerable from the
  log for all non-storage failures.
- **Security:** unchanged trust boundary; no new surface.

### Key Decisions

- **D1 — Ordering via the coordinator, no `seq` field.** One ordered append+enqueue step gives
  per-client delivery order == append order. Rejected an explicit sequence number: reopens 029's
  settled decision; its only real benefit (gap detection) is redundant with reconnect replay here.
- **D2 — Accounting summed from `llm_request`, deduped by id.** One rule for live and replay.
  Rejected snapshot-authoritative accounting: it special-cases replayed events and needs a boundary
  check, reintroducing the live/replay split this removes.
- **D3 — Session-global coordinator; `EventFactory` pure construction.** Rejected folding publish
  into `AgentHarness` (overloaded; `run_child` would reach back in) and per-scope factories owning
  the client set (client set + ordering are session-global; factories are per-scope for
  model/cost).
- **D4 — Clean live-protocol break; one-shot host-CLI log migration.** Rejected a dual-protocol shim
  (single-user, shipped together) and discarding logs (history has analysis value). `PROTOCOL_VERSION`
  bump owned by M4.
- **D5 — One slim `handshake`; client owns the cursor; git/status via `status_updated`.** Rejected
  keeping `SessionSnapshot`/`SessionInfo`: their content is redundant with the stream under D2, and
  the server-supplied cursor is a source of the reconnect double-count.
- **D6 — No new event-creation timestamp; ULID carries it.** Domain `sent_at` fields retained
  (provider-send time ≠ log-write time). Rejected a universal `ts`.
- **D7 — Structured `turn` + `iteration` identity.** Rejected the `"2.1"` string: identity/accounting
  shouldn't parse a string; clean break makes the rename free.
- **D8 — Complete the canonical family with live-only `handshake`/`status_updated`/`error_notice`;
  fold `Usage` into `llm_request`; derive model name/cache client-side.** Rejected retaining any wire
  type or adding `model_name`/`supports_cache` to persisted `model_switch` (derivable from the
  catalog). `error_notice` is a single non-persisted frame for targeted command rejections and the
  best-effort storage-failure notice — chosen over persisting rejections (which would corrupt the
  log) or reusing `turn_error` (a persisted terminal event with different semantics).
- **D9 — Per-client bounded send queues, sends outside the ordering lock.** Rejected awaiting each
  `send_text` under the lock (a stalled client would block persistence and all producers — the
  current `_broadcast` already serialises sends, and coupling that to the persistence lock would be
  a regression). Chosen because multi-client is an explicit goal.
- **D10 — Migration is a host-side one-shot CLI run with services stopped.** Rejected
  auto-on-agent-startup: the agent sees only its own log and would race the orchestrator's
  `metrics.db` writer; a host CLI owns both logs and metrics with no concurrent writer.

### Risks and Open Questions

Risks:
- The converter must preserve ids/order exactly or reconnection and metrics dedup break — mitigated
  by round-trip tests on real old-format fixtures (M7).
- The identity rename (D7) reaches events, factory, accounting, metrics, and TUI — mitigated by
  landing it as its own milestone (M3) on the finalised single schema.
- Bus queue bound (default 1024 frames): too low could disconnect healthy-but-bursty clients, too
  high could mask a wedged client. The default is generous for a single user; tune only if observed.

Open questions: none. (Converter ownership resolved as D10; queue bound defaulted to 1024 frames;
handshake carries `model_key`.)

---

## Milestones

### M1 — Session coordinator + persistence ownership (prefactor)

**Approach**
- New `agent/session_bus.py`: `SessionEventBus` with `append` (sync, persist-only), `async publish`
  (append + enqueue under the ordering lock), `async broadcast` (enqueue-only), `add_client`/
  `discard_client` (per-client bounded `asyncio.Queue` + sender task), the id→line index seeded from
  the log at construction, and typed `LogAppendError`/`EventIdConflict`. Add a stateless line append
  to `shared/session/log.py`; move duplicate/conflict detection to the bus index (no full-log
  reparse).
- Reduce `EventFactory` to construction only (remove `append_event`).
- Route the **persisted canonical event stream** through the bus: the root turn events, `run_child`
  events, `session_started` (via sync `append` at construction — no clients yet), and the
  `model_switch` event (published from the async `_handle_model_switch`, not sync `switch_model`).
- ⚠️ Keep the existing **non-persisted** wire frames for now — the turn-active rejection, the
  model-switch rejection, and the post-turn status refresh. Since the client registry now lives on
  the bus, route them through `bus.broadcast_serialized(<wire json>)` (enqueue-only, no append) so
  they ride the per-client queues without racing the sender tasks, rather than the removed
  `AgentHarness._broadcast`. They must **not** be persisted; this keeps the log uncorrupted while the
  live view is broken. (Sending the transitional rejections to all clients is harmless in this
  broken-view window — they become targeted canonical `error_notice` frames in M2/M5.)
- Test seam: the bus API + the persisted log.

**Wiring**
- State: one `SessionEventBus` per session, created in `AgentHarness.__init__` (replacing
  `self.clients` and the factory's append behaviour); holds client queues, the ordering lock, and
  the id index.
- Producers: `handle_message`, `run_child`, session start (`append`), `_handle_model_switch`
  (`publish`).
- Consumers: `app.py::stream` calls `bus.add_client`/`discard_client`; per-client sender tasks drain
  queues to `send_text`.
- Call site: `bus.append(SessionStarted(...))` at init; `await bus.publish(factory.tool_call(...))`.

**Edge cases**
- Concurrent root + children: ordering lock serialises append+enqueue.
- Duplicate id: identical no-op; conflict raises `EventIdConflict`.
- Slow client: bounded queue overflow → disconnect, no lock/persistence stall.
- Append write failure: `publish` raises `LogAppendError` (surfaced to caller; consumed properly in
  M5).

**Tasks**
- Add `session_bus.py` (API, per-client queues/senders, index, typed errors).
- Add the stateless append to `log.py`; move dup/conflict to the bus.
- Strip appends from `EventFactory`.
- Repoint root/child/session-start/model-switch emits at the bus; publish `model_switch` from the
  async handler; route the wire rejection + status refresh through `bus.broadcast_serialized`.

**Deliverable:** all *persisted* agent events (except `/shell`) flow through one ordered
persist-then-enqueue coordinator; `EventFactory` performs no appends; append is O(1) amortised; a
stalled client cannot block persistence.
**Breaks (flagged):** the live TUI (still a wire reducer) — restored in M2. `/shell` still writes
`MessageEntry` until M6. Turn-active rejection + status refresh remain wire (non-persisted) until
M2.
**Verify:** `uv run pytest tests/test_harness.py tests/test_subagent_accounting.py` plus new
`tests/test_session_bus.py`: (a) append order == per-client delivery order across root + two
children; (b) O(1) append; (c) idempotent vs conflicting append; (d) a never-draining client is
disconnected without stalling another client's delivery.

### M2 — Complete the canonical public schema + single reducer (restores the live view)

**Approach**
- Add canonical live-only structs `handshake`, `status_updated`, `error_notice`
  (`shared/canonical_events.py`), excluded from `PersistedEvent`. Fold `Usage` out of the public
  schema (clients derive context tokens/% from `llm_request` + catalog). Keep persisted
  `model_switch` minimal; the TUI maps `model_key` → display name / cache via `shared/models`.
- Replace the connect frames: `app.py::stream` sends `handshake` then `status_updated` to the
  just-registered socket via `bus.send_to` (preserving that client's order) instead of
  `SessionSnapshot`+`SessionInfo`. The post-turn status refresh becomes `status_updated` via
  `bus.broadcast`; `_handle_model_switch` rejections become an `error_notice` via `bus.send_to` to
  the submitting socket; the turn-active rejection becomes an `error_notice` (still broadcast-to-all
  here — made targeted in M5). Remove `bus.broadcast_serialized` (no more wire frames).
- Bump `PROTOCOL_VERSION` — the wire protocol is finalised here (schema deleted, `handshake` added)
  — and surface the handshake mismatch warning in the TUI.
- Delete the entire wire schema from `shared/events.py` (events, `ServerEvent`,
  `serialize_event`/`deserialize_event`) and the retained wire broadcast. Remove the `ws_client`
  demux — one `decode_event` path. Collapse the TUI to one canonical reducer (live + replay), deduped
  by `id`. Accounting is summed from `llm_request` (already via `_accumulate_ledger`); with the
  snapshot gone there is no overwrite to double-count.
- Test seam: `WSClient.receive` (single decode) + the reducer output.
- ⚠️ `llm_request` already broadcasts canonically today, so much of the canonical reducer exists in
  `_render_canonical`; the work is folding the wire branches in and wiring the new live-only frames.

**Edge cases**
- `tool_call.input` containing a `data` key: decoded and rendered (the misroute bug fixed).
- Duplicate id across replay + buffered live: rendered once.
- `model_switch` on replay/live: TUI derives name/cache from the catalog.

**Tasks**
- Add the three live-only structs; remove public `Usage`; delete the wire schema.
- Send `handshake` + `status_updated` on connect via `send_to`; convert status refresh + rejections
  to canonical frames.
- Bump `PROTOCOL_VERSION`; surface the handshake mismatch warning in the TUI.
- Single decode path in `ws_client.py`; one canonical TUI reducer; model-name/cache derivation.

**Deliverable:** the live TUI renders a full turn from canonical events through one reducer; no wire
types remain; connect uses `handshake` + `status_updated`.
**Breaks (flagged):** turn-active rejection is broadcast-to-all (disrupts other clients) until M5;
reconnect determinism/two-client correctness not yet addressed (M4). Existing logs unaffected.
**Verify:** `uv run pytest tests/test_ws_integration.py tests/test_tui_canonical.py
tests/test_model_switch.py tests/test_protocol_version.py` incl. a child `tool_call` with
`{"input": {"data": ...}}` through `WSClient.receive`, and a `handshake` whose `protocol_version`
exceeds the client's asserting the warning fires.

### M3 — Turn/iteration identity unification

**Approach**
- Replace `turn_iteration: str` with `turn: int` + `iteration: int` across the canonical structs;
  fold `iteration_start.index` into `iteration`; remove `assistant_message.turn_iteration`; bump
  `session_started.schema_version` to 2. Propagate through `EventFactory` (stop formatting
  `f"{turn}.{i}"`), accounting aggregation, the TUI reducer, and metrics.
- `orchestrator/metrics.py`: integer `turn`/`iteration` columns replace `turn_iteration TEXT`; bump
  the schema version (old DB reset per the existing pattern).
- Test seam: canonical round-trip + accounting aggregation + metrics ingestion.

**Edge cases**
- Two children sharing local `(turn, iteration)`: still distinct via `scope` in
  `(scope, turn, iteration, request_id)`.

**Tasks**
- Change the canonical structs + `EventFactory` call sites.
- Update accounting aggregation and the TUI reducer to integer fields.
- Migrate metrics columns; bump schema version.

**Deliverable:** one integer `turn`/`iteration` vocabulary; no `"2.1"` string anywhere.
**Breaks (flagged):** existing logs (old shape) no longer decode until M7; metrics reset to a fresh
schema, no backfill here (M7).
**Verify:** `uv run pytest tests/test_canonical_session_events.py tests/test_session_events.py
tests/test_subagent_accounting.py tests/test_orchestrator_metrics_api.py` with identity assertions.

### M5 — Turn lifecycle: user-message + guaranteed terminal event (root + child)

**Approach**
- Add `AgentHarness.try_begin_turn() -> bool` (atomic sync check-and-set of `_turn_active`) and call
  it from `app.py::stream` before dispatching `handle_message`; on rejection `stream` sends an
  `error_notice(kind=turn_active)` to the submitting socket via `bus.send_to`. `handle_message` no
  longer self-guards. Publish `user_message` **inside** the guarded region; wrap the turn in
  `try/finally`; the `finally` publishes exactly one terminal event if none was emitted and clears
  `_turn_active`.
- In `run_child`, apply the same lifecycle as the root: a `terminal_emitted` guard so at most one
  terminal event is persisted per child (an exception after a normal `turn_complete`/
  `turn_interrupted` must not emit a second), the outer `except` publishing exactly one scoped child
  `turn_error` **only if none was emitted**, and teardown in a `finally`.
- Distinguish failure classes identically for root and child: a non-storage failure → one durable
  terminal `turn_error`; a `LogAppendError` → no attempted durable terminal event, a best-effort live
  `bus.broadcast(error_notice(kind=storage_error))`, then teardown.
- Test seam: `handle_message`/`run_child` with a fake LLM and an injectable append failure; `stream`
  with two near-simultaneous submissions.

**Wiring**
- State: `_turn_active` (harness) — set atomically by `try_begin_turn` (called from `stream`),
  cleared strictly in the `finally`.
- Producers: `stream` accepts (set) or rejects (targeted `error_notice` via `bus.send_to`); the
  `finally` publishes the terminal event via the bus; the storage-error path uses `bus.broadcast`.
- Call site: `if _agent.try_begin_turn(): create_task(handle_message(...)) else: await
  bus.send_to(ws, ErrorNotice(kind="turn_active", ...))`.

**Edge cases**
- `user_message` publish raises (non-storage): `finally` still emits a terminal event; state cleared.
- `LogAppendError` (root or child): state cleared, best-effort `error_notice(kind=storage_error)`
  broadcast, no terminal event attempted.
- Child body raises with no terminal emitted: exactly one scoped child `turn_error`.
- Child body raises *after* a terminal event was already emitted: no second terminal event (guard).
- Two near-simultaneous submits: exactly one accepted; the other gets a targeted `error_notice`.

**Tasks**
- Add `try_begin_turn()` and call it from `stream`; targeted `error_notice` on rejection via
  `bus.send_to`.
- Move `user_message` publish into the guarded region.
- Add the outermost `finally` terminal guarantee (root) and the child terminal event (`run_child`).
- Implement failure-class handling using the typed errors from M1.

**Deliverable:** at most one active turn is accepted (atomic `try_begin_turn`), a concurrent submit
is rejected only to the submitting socket, every accepted turn yields exactly one terminal event
(all non-storage failures) and one broadcast `user_message`, append failure clears state + emits
`error_notice`, and no path leaves turn-active set.
**Verify:** `uv run pytest tests/test_harness.py tests/test_subagent_interrupt.py` plus new tests:
forced-exception (root + child, incl. `user_message` failure) asserting a durable terminal event +
cleared state; an injected `LogAppendError` asserting cleared state + `error_notice` without
crash/hang; and two near-simultaneous submissions asserting exactly one accepted and a targeted
`error_notice` to the other.

### M4 — Reconnect reconciliation + multi-client correctness

**Approach**
- Reconnect/accounting: the TUI owns the replay cursor as the last-applied **persisted** id (never
  advanced by `text_delta`); on reconnect it retains in-memory totals and applies only incremental
  replay + live, deduped by `id`; a fresh attach sums the full replay. On a 409 unknown cursor it
  resets *all* replay-derived state together (rendered conversation, `_seen_event_ids`,
  `_seen_accounted_ids`, cumulative totals) before a full replay — see Error Handling.
- Multi-client: confirm the `user_message` published by M5 reaches every connected client; the
  atomic accept + targeted turn rejection (M5) and model-switch rejection (M2) are exercised here
  with two clients.
- Test seam: WS connect + `/events` reconciliation and two concurrent `TestClient` sockets.

**Wiring**
- State: the TUI's `_seen_event_ids`, replay cursor (last-applied persisted id), and cumulative
  accounting — retained across a reconnect, reset together on a 409.
- Producers: `handle_message` (M5) publishes `user_message` to all; the agent enqueues
  `handshake` + `status_updated` to the connecting socket via `bus.send_to`.
- Consumers: every client reduces the broadcast `user_message`; a reconnecting client resumes from
  its retained cursor/totals (or a clean full-reset on 409).

**Edge cases**
- Two simultaneous submits: at most one accepted; the other gets a targeted `error_notice`; other
  clients unaffected (mechanism from M5).
- Reconnect after a completed turn: no duplicate entries or doubled accounting.
- 409 unknown cursor: full reset then full replay — no duplication, correct totals.
- Fresh attach: cost line reads zero until replay sums (acceptable, provisional).

**Deliverable:** fresh attach, reconnect (incl. 409 full reset), and two simultaneous clients show
correct, non-duplicated conversation + accounting; user messages propagate to all clients.
**Breaks (flagged):** pre-M4 clients cannot attach (clean break, D4).
**Verify:** `uv run pytest tests/test_cli_attach.py` plus new tests: combined
handshake+replay+buffered-live reconciliation; reconnect-after-completed-turn asserting no duplicate
entries/accounting; a 409 unknown-cursor asserting a clean full reset (no duplication, correct
totals); two-client user-message propagation.

### M6 — Shell as canonical event

**Approach**
- `agent/app.py::shell_log` builds a canonical `shell_command` and `publish`es it via the bus;
  remove the `MessageEntry`/`write_entry` shell path. Move the `MessageEntry`/`MessageMetadata`
  structs to the migration module (M7 needs to decode legacy shell lines) and delete `write_entry`
  from `shared/session/log.py`.
- The TUI already renders `shell_command` on replay; no client change.
- Test seam: `/shell` → `/events`.

**Edge cases**
- Non-zero exit / empty output: persisted with its `exit_code`/`output`.

**Tasks**
- Rewrite `shell_log` to publish canonical `shell_command`.
- Remove `write_entry`; relocate the legacy structs to the migration module.

**Deliverable:** shell commands appear in `/events` and render on replay; no runtime `MessageEntry`
writer remains.
**Breaks (flagged):** none.
**Verify:** `uv run pytest tests/test_017_features.py` (regroup by behaviour while here) plus a new
test posting to `/shell` and asserting a `shell_command` in `/events`.

### M7 — One-shot host-CLI log migration + metrics backfill

**Approach**
- New migration module (`shared/session/migrate.py`) with a self-contained legacy decoder for
  `role="shell"` `MessageEntry` lines and a transform to the new spec (`"2.1"` → integer
  `turn`/`iteration`; shell → `shell_command`; bump `schema_version`), preserving ULID `id`s and
  append order.
- CLI entry: a new `@main.command()` named `migrate-sessions` in `cli/src/archie_cli/cli.py`
  (follows the flat click-group pattern, e.g. the existing `ls` command), with a `--force` flag (see
  the `.legacy` collision rule in Error Handling). It iterates `~/.nexus/sessions`.
- Commit order (per log): copy original → `.legacy`, write `.tmp`, `fsync`, atomic-rename `.tmp`
  over the original (full sequence + collision handling in Error Handling); skip logs already at
  `schema_version==2`; copy unrecognised lines verbatim with a warning.
- Metrics: promote the private archive/create/ingest logic in `orchestrator/metrics.py` to a public
  `reset_and_backfill(db_path, session_log_paths)` that archives the old DB (→ `.legacy.<ts>`),
  creates the v-bumped schema, and ingests `llm_request` records from the migrated logs via the
  existing `(session_id, event_id)` upsert (so a partial/failed backfill is safely re-runnable). The
  `migrate-sessions` command calls it after all logs migrate.
- ⚠️ Intended to run with sessions and the orchestrator stopped (D10) — no concurrent log/metrics
  writer.
- Test seam: converter round-trip on fixture logs; `reset_and_backfill` on migrated fixtures.

**Edge cases** (see Error Handling): already-migrated skip; malformed line copied verbatim; rewrite
failure leaves original intact; backfill failure leaves logs intact and is re-runnable.

**Tasks**
- Implement the migration module (legacy decoder, transform, commit-order rewrite, `.legacy` backup
  + collision/`--force`, idempotent skip).
- Add the `migrate-sessions` command; add `metrics.reset_and_backfill` and call it.
- Add round-trip + `reset_and_backfill` tests on old-format fixtures.

**Deliverable:** running the migration converts existing logs to the new spec (ids/order preserved)
and rebuilds metrics. Re-running is a no-op for already-migrated logs (`schema_version==2`); the
metrics reset+backfill is safely repeatable (it re-archives the DB and rebuilds) — so a re-run is
not a whole-command no-op, but leaves a correct end state.
**Verify:** `uv run pytest` (full suite) plus new migration tests on fixtures (a `"2.1"` canonical
log and one with legacy shell `MessageEntry` lines) asserting id/order preservation, correct
restructuring, idempotent re-run, and a metrics-backfill assertion.

### M8 — Event-spec documentation

**Approach**
- Document the finalised event spec in `docs/architecture.md` (or a referenced doc): each event type
  with fields, persisted vs live-only, the `(scope, turn, iteration, request_id)` identity, and the
  ULID time-extraction procedure (D6). Update `CONTRIBUTING.md` invariants (single public schema;
  persist-before-broadcast; bus as sole emit path).
- Test seam: none (docs) — validated by review.

**Tasks**
- Write the event-spec section incl. ULID time extraction.
- Update architecture + contributing docs.

**Deliverable:** the event spec and its invariants are documented and match the shipped schema.
**Verify:** `uv run ruff format --check .` clean; manual review that every canonical type in
`shared/canonical_events.py` appears in the doc with correct persisted/live-only classification.

## Sequencing

```
M1 → M2 → M3 → M5 → M4 → M7 → M8
M1 → M6 ─────────────────→ M7
```

M1 relocates ownership (breaks the live view; keeps the log clean by routing the retained wire
rejection/status through `broadcast_serialized`). M2 completes the canonical schema (adds the
live-only family, folds `Usage`, deletes the wire schema, bumps `PROTOCOL_VERSION`) and restores the
live view. M3 settles identity. M5 owns the full turn lifecycle — the atomic `try_begin_turn` accept,
targeted turn rejection, `user_message`, and the terminal-event guarantee — using the `error_notice`
type from M2. M4 builds reconnect reconciliation + multi-client correctness on top. M6 (shell) needs
only M1 and relocates the legacy structs M7 depends on. M7 migrates logs + metrics against the
finalised spec. M8 documents it.

## Review

Phase 4 (`review-plan`) not yet run.

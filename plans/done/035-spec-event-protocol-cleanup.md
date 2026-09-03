# 035 — Event Protocol and Persistence Cleanup

Type: spec. Status: ready for implementation.

Implementation baseline: branch from `archie/034-event-model-consolidation` after plan 034 is
complete. At planning time that branch is at `77f80a8`. Plan 035 layers directly on 034; it is not
intended to apply independently to `main`.

## Objective

Complete the event-model consolidation begun by plan 034 by removing remaining transitional naming,
duplicated persistence classification, inconsistent command encoding, and unnecessary public events.

The final system will have one declarative public event model, one flat command model, one
event-emission API, one stateful session-log abstraction, explicit internal loop events, and
deterministic request-keyed TUI streaming reconciliation.

## Context

Plan 034 established the canonical event stream, persist-before-broadcast ordering, reconnect
history reads, and session-level coordinator. Its implementation exposed several remaining design
residues:

- `canonical_events.py` is now the only public event schema, while `events.py` contains commands;
- commands use dataclasses and a nested envelope while events use flat tagged `msgspec` structs;
- persistence is duplicated across `PersistedEvent`, `PersistedEventTypes`, the bus, and log helpers;
- `assistant_message.request_ids` retains obsolete turn-level semantics;
- shell and model-switch events are persisted without sufficient history value;
- child events are serialized, passed through a callback, and decoded before entering the
  coordinator;
- child reconnect reconciliation snapshots complete rendered line arrays rather than tracking
  request-local transient text.

The work deliberately cleans up every event-related layer while retaining the useful boundary
between provider events, internal loop events, and public session events.

The 034 protocol and schema are considered an unreleased intermediate state. No running session or
stored log requires compatibility with that state. The final protocol and log schema remain version
2, and the existing migration is updated to target the final 035 shape directly.

## Requirements

### Public protocol

- MUST expose server-to-client events from `archie_shared.events` and client-to-server commands from
  `archie_shared.commands`
  - AC: No runtime or test code imports `archie_shared.canonical_events`
  - AC: `canonical_events.py` is removed
  - AC: `events.py` contains only public server-to-client event definitions and codecs
  - AC: `commands.py` contains only client-to-server command definitions and codecs

- MUST encode commands as flat, tagged `msgspec` records
  - AC: A message encodes as `{"type":"message","content":"hello"}`
  - AC: A model switch encodes as `{"type":"switch_model","model_key":"..."}`
  - AC: A targeted interrupt encodes `scope` and `subagent_index` at the top level
  - AC: No command contains a nested `data` envelope
  - AC: Unknown fields and unknown command tags are rejected
  - AC: An interrupt providing only one targeting field is rejected

- MUST retain protocol version 2 as the final protocol produced by plans 034 and 035
  - AC: `PROTOCOL_VERSION` remains `2`
  - AC: The intermediate protocol implemented on the 034 branch has no compatibility path
  - Why: The 034 format has not been used outside its development branch

### Event model

- MUST define persistence once as a non-serialized property of each event type
  - AC: Every public event exposes a class-level persistence declaration
  - AC: The persistence declaration is absent from encoded JSON
  - AC: No manually duplicated runtime tuple such as `PersistedEventTypes` remains
  - AC: Changing an event's persistence classification requires changing only its event definition

- MUST reduce the persisted event set to durable conversation, request, tool, and terminal history
  - AC: The persisted set is exactly `session_started`, `user_message`, `iteration_start`,
    `llm_request`, `assistant_message`, `tool_call`, `tool_result`, `turn_complete`, `turn_error`,
    and `turn_interrupted`
  - AC: Each persisted event is appended before being delivered live
  - AC: History reads return the same canonical event content and append order as the session log

- MUST reduce the live-only event set to connection metadata, mutable session status, transient
  text, and notices
  - AC: The live-only set is exactly `handshake`, `session_status`, `text_delta`, and `error_notice`
  - AC: None of those events appears in the session JSONL log
  - AC: Live-only event IDs never advance the client's history cursor

- MUST remove `shell_command`, `model_switch`, and `status_updated` from the public event contract
  - AC: The event decoder rejects all three removed tags
  - AC: No producer or consumer references their removed classes
  - AC: Event specification documentation no longer lists them

### Connection and mutable status

- MUST separate immutable connection metadata from mutable session status
  - AC: `handshake` contains `protocol_version` and `session_id`
  - AC: `handshake` does not contain model or Git status
  - AC: `session_status` contains `model_key` and `git_branch`
  - AC: A newly connected client receives `handshake` followed by `session_status` before any
    concurrently produced event

- MUST notify all connected clients when mutable agent-owned status changes
  - AC: A successful model switch broadcasts `session_status` containing the new model key
  - AC: Root-turn cleanup broadcasts `session_status` containing the current model key and Git branch
  - AC: Model-switch rejection remains targeted to the submitting client through `error_notice`
  - AC: Local shell execution does not trigger an explicit status refresh

- MUST avoid persisting model-switch history
  - AC: A successful switch produces no persisted `model_switch` record
  - AC: Every subsequent `llm_request` records the model actually used
  - AC: A newly connected client learns the current model from `session_status`
  - AC: Historical request cost remains unchanged after a switch

### Assistant-message provenance

- MUST associate each assistant message with the single request and iteration that generated its
  content
  - AC: `assistant_message` contains singular `request_id`
  - AC: `assistant_message` contains integer `iteration`
  - AC: `assistant_message` does not contain `request_ids`
  - AC: Root and child assistant messages use identity `(scope, turn, iteration, request_id)`
  - AC: An assistant message never references earlier requests merely because they occurred in the
    same turn

- MUST preserve available root and child partial text before terminal interruption or failure
  - AC: Partial text is persisted as `assistant_message(interrupted=true)` before the matching
    terminal event
  - AC: Root and child behavior is equivalent
  - AC: A reconnect after interruption or failure reconstructs the persisted partial text

### Session log

- MUST provide one stateful session-log abstraction as the sole owner of runtime durable event storage
  - AC: The public write operation is `SessionLog.append()`
  - AC: The public read operation is `SessionLog.read(after_id=None)`
  - AC: `CursorNotFound`, `EventIdConflict`, and `LogAppendError` are defined in and publicly
    importable from `archie_shared.session.log`; callers do not import storage exceptions from the
    agent package
  - AC: No public storage API is named `replay`
  - AC: No runtime component writes session JSONL records outside `SessionLog`
  - AC: The offline migration remains the sole exception: it reads legacy source lines and performs
    its existing atomic temp-file rewrite because malformed and obsolete records are not final
    `PersistedEvent` values
  - AC: The log owns its path, ordered valid-event list, append-side ID index, duplicate detection,
    conflict detection, and cursor lookup

- MUST initialize `SessionLog` deterministically from an existing JSONL file
  - AC: Construction scans existing lines once in file order and decodes only final persisted events
  - AC: Each valid event is re-encoded canonically; the canonical line, not the raw source bytes, is
    stored in the ID index
  - AC: Malformed, unknown, and live-only lines produce line-numbered warnings and are excluded from
    both ordered reads and the ID index
  - AC: A repeated ID with identical canonical content produces a line-numbered warning; the first
    occurrence remains in ordered reads and the later duplicate is skipped
  - AC: A repeated ID with different canonical content raises `EventIdConflict` during construction
    and prevents startup
  - AC: After construction, `append()` updates the ordered event list and ID index only after the
    durable write succeeds

- MUST retain exactly one `session_started` event across agent process restarts
  - AC: Async startup emits `session_started` through `SessionEventBus.emit()` only when
    `SessionLog.read()` returns no valid existing events
  - AC: Starting against a non-empty valid session log does not append another `session_started`
  - AC: Focused startup tests cover both a new log and an existing log

- MUST preserve canonical event content and append order during reads
  - AC: `read()` returns decoded persisted events in append order
  - AC: Supplying a known `after_id` returns only events after that ID
  - AC: Supplying an unknown non-empty cursor produces the existing cursor-not-found behavior
  - AC: HTTP history re-encodes returned events through the canonical event encoder
  - AC: A live-only event found in a session log is treated as an invalid log record

- MUST retain idempotent append semantics
  - AC: `append()` returns `True` when it writes a new event
  - AC: `append()` returns `False` for the same ID and identical canonical content
  - AC: Reusing an ID with different content raises an event-ID conflict
  - AC: Event-ID conflicts remain distinguishable from filesystem append failures

### Event coordination

- MUST provide one event-emission path for all runtime producer events, with connection admission as
  the sole explicit exception
  - AC: Root, child, status-update, targeted-notice, persisted, and other live-only producers submit
    events through `SessionEventBus.emit()`
  - AC: Producers do not choose between separate `publish()` and `broadcast()` methods
  - AC: Events declaring persistence are appended before client queue admission
  - AC: Live-only events are queued without a log append
  - AC: Targeted delivery rejects events declaring persistence
  - AC: Root and concurrent child events retain one global order equal to persisted append order
  - AC: Initial `handshake` and `session_status` use
    `SessionEventBus.register_client(websocket, initial_events)` as the only public path that queues
    events without calling `emit()`
  - AC: Registration validates both initial events as live-only before mutating client state, then
    registers and enqueues them under the ordering lock through the same private encode/enqueue helper
    used by `emit()`

- MUST preserve slow-client isolation
  - AC: Each client retains an independent bounded delivery queue
  - AC: A stalled client cannot delay persistence or another client
  - AC: Queue overflow disconnects only the stalled client

### Internal event boundary

- MUST preserve distinct provider, loop, and public event layers
  - AC: Provider-specific stream events remain internal to provider and loop code
  - AC: Loop events remain in-process and contain no WebSocket, persistence, or public protocol
    behavior
  - AC: Public events contain complete session, request, scope, and accounting identity
  - Why: The loop must remain provider-neutral and independently testable

- MUST make the loop-event boundary explicit and consistently named
  - AC: Internal events live in `loop_events.py`
  - AC: `RequestFinished` lives with the other loop events
  - AC: No internal documentation refers to the command module as a wire-event module
  - AC: Public and internal event classes can be distinguished from their import paths

- MUST pass typed public events from root and child producers to the session coordinator
  - AC: Event construction returns typed event objects rather than event-plus-serialized tuples
  - AC: Child event delivery contains no serialize → callback → decode round trip
  - AC: Serialization occurs only at the event coordination or storage boundary

### TUI event application and reconnect

- MUST retain the current imperative TUI event-application architecture
  - AC: No standalone reducer abstraction is introduced
  - AC: The main handler is named to indicate that it applies events rather than only rendering
    canonical history
  - AC: Live and stored events continue through the same application path

- MUST reconcile transient assistant text by exact request identity
  - AC: Transient root and child text is keyed by
    `(scope, subagent_index, turn, iteration, request_id)`
  - AC: Whole-child-history snapshots are removed
  - AC: `assistant_message` replaces or finalizes only its matching transient buffer
  - AC: Tool, ledger, root, and sibling-child events do not clear unrelated transient text
  - AC: Matching transient state is removed after authoritative assistant content is applied

- MUST reconstruct conversation and accounting correctly after reconnect
  - AC: A reconnect during child streaming preserves all earlier durable child lines
  - AC: A reconnect does not render streamed and persisted copies of the same assistant text
  - AC: Stored and buffered-live overlap is deduplicated by event ID
  - AC: Accounting is summed only from deduplicated `llm_request` events
  - AC: An unknown cursor resets all history-derived UI and accounting state before a full read

### Direct shell execution

- MUST keep `!` shell execution local to the initiating TUI
  - AC: The TUI executes the command through host-side `docker exec`
  - AC: The TUI renders command output without sending it to the orchestrator or agent
  - AC: Other attached clients do not receive the command or its output
  - AC: Shell output is absent from session history and reconnect reads

- MUST remove the shell persistence path
  - AC: The agent `/shell` route is removed
  - AC: The orchestrator `/sessions/{id}/shell` route is removed
  - AC: Shell event IDs, persistence fallbacks, and canonical shell reconciliation state are removed
  - AC: Existing timeout, cancellation, truncation, and local execution-error behavior remains
    covered by tests

### Migration

- MUST update the existing migration to produce the final plan-035 schema version 2 directly
  - AC: Migrated `assistant_message` records contain singular `request_id` and integer `iteration`
  - AC: The request ID identifies the request that generated that message
  - AC: Iteration is derived from the referenced `llm_request`
  - AC: Migrated supported records decode through the final event union

- MUST remove obsolete shell and model-switch history during migration
  - AC: Each parsed object is classified exactly once before final-event decoding: `role="shell"` or
    `type="shell_command"` takes shell precedence; otherwise `type="model_switch"` is model-switch
  - AC: The selected removed category is omitted even when the record's remaining fields are malformed
  - AC: An overlapping `role="shell"` plus `type="model_switch"` record increments only the shell
    counter
  - AC: Malformed JSON with no readable discriminator retains the general copy-with-warning behavior
  - AC: `migrate_session_log()` and `migrate_session_logs()` both return immutable
    `MigrationStats(logs_migrated, shell_records_removed, model_switch_records_removed)` values; the
    aggregate function sums all three fields across files
  - AC: A skipped schema-version-2 file contributes zero to every field
  - AC: The CLI reports aggregate migrated-log, shell-removal, and model-switch-removal counts in one
    summary line
  - AC: Removed records remain available in the `.legacy` backup
  - AC: Atomic replacement and existing failure-recovery guarantees remain intact

- MUST rebuild metrics solely from migrated `llm_request` records
  - AC: Removed shell and model records create no metrics rows
  - AC: Re-running metrics backfill does not duplicate request rows
  - AC: Historical token and cost totals match the migrated request ledger

### Documentation and quality

- MUST document the final command, event, status, persistence, and reconnect contracts
  - AC: `docs/event-spec.md` matches the final event and command fields
  - AC: `docs/architecture.md` matches final component and storage ownership
  - AC: `docs/session-protocol-walkthrough.md` contains no removed event, route, or envelope shape
  - AC: Documentation reserves “replay” for client behavior and uses “read” for log access
  - AC: Documentation does not call the TUI handler a reducer

- MUST validate the final implementation across every affected package
  - AC: Focused tests cover command decoding, event persistence, session-log behavior, coordinator
    ordering, model status, assistant reconciliation, shell removal, child terminal text, and
    migration
  - AC: The complete test suite passes
  - AC: Ruff check and formatting pass
  - AC: `git diff --check` passes

## Technical Design

### Overview

Plan 035 replaces transitional event machinery rather than adding another compatibility layer. The
public protocol consists of flat `msgspec` commands and one tagged event union whose classes declare
their own persistence. A stateful `SessionLog` owns durable storage. `SessionEventBus` remains
responsible for ordering and client delivery but exposes one `emit()` path.

Provider events, internal loop events, and public session events remain separate boundaries. Root
and child producers emit typed public events without intermediate serialization. The TUI retains
its imperative event handler but reconciles streamed text using exact request identity.

**Done when:** A reader can identify the three event layers, the owner of durable storage, the owner
of delivery ordering, and the public command/event formats without consulting implementation code.

### Technical Stack

- Reuse the existing `msgspec` dependency for commands and events.
- Reuse existing ULID event IDs.
- Reuse JSONL session storage.
- Reuse existing `asyncio` locks and per-client queues.
- Add no dependencies, serialization libraries, storage engines, or services.

`msgspec.Struct` inheritance provides a non-serialized class-level persistence declaration. Focused
tests pin this behavior.

**Done when:** Every technology used by the design is identified as an existing dependency and no
implementor must select a new library.

### Architecture

```text
Provider stream event
    ↓
Internal LoopEvent
    ↓
Harness or child adapter
    ↓
EventFactory creates typed SessionEvent
    ↓
SessionEventBus.emit()
    ├── event.persist=True  → SessionLog.append()
    └── event.persist=False → no storage
    ↓
Per-client queues
    ↓
Orchestrator relay
    ↓
TUI _apply_event()
```

Ownership:

- `EventFactory`: public event identity and construction.
- `SessionLog`: durable serialization, ID indexing, append, and ordered reads.
- `SessionEventBus`: global ordering and client delivery.
- Harness and child adapters: loop-event to public-event conversion.
- TUI: transient presentation state and history cursor.
- Orchestrator: transparent relay and best-effort metrics ingestion.

**Done when:** Every event-processing responsibility has exactly one owning component and every
cross-component interaction names its interface.

### Components

**Public commands** (`archie_shared.commands`)
- Interface: `Command`, `encode_command()`, `decode_command()`.
- State: none.
- Responsibility: strict flat client-to-agent command contract.

**Public events** (`archie_shared.events`)
- Interface: `Event`, `PersistedEvent`, `SessionEvent`, `encode_event()`, `decode_event()`.
- State: none.
- Responsibility: complete server-to-client event contract and declarative persistence.

**SessionLog** (`archie_shared.session.log`)
- Interface: `append(event) -> bool`, `read(after_id=None) -> list[PersistedEvent]`; public exceptions
  `CursorNotFound`, `EventIdConflict`, and `LogAppendError` are defined in and importable from this
  module.
- State: log path, ordered valid-event list, and `id → canonical line` append index initialized by one
  deterministic scan of existing storage.
- Responsibility: all durable event storage, startup reconciliation, duplicate/conflict detection,
  cursor validation, and ordered reads.

**SessionEventBus**
- Interface: `emit(event, target=None)` plus
  `register_client(websocket, initial_events=(handshake, session_status))` for atomic admission.
- State: ordering lock, client queues/senders, `SessionLog` reference.
- Responsibility: ordering and delivery; obey event persistence declarations. Registration is the sole
  public event-queueing exception to `emit()` and shares its private encode/enqueue helper.

**Loop adapters**
- Interface: consume `LoopEvent`, create and emit `SessionEvent`.
- State: current request/iteration text and transcript state.
- Responsibility: enrich transport-neutral loop output with session identity.

**TUI event application**
- Interface: `_apply_event(event, historical=False)`.
- State: cursor/dedup/accounting/tool/child/transient request state.
- Responsibility: apply both live and stored events to the UI.

**Migration** (`archie_shared.session.migrate`)
- Interface: existing host migration command; dedicated raw-line converter; immutable public
  `MigrationStats(logs_migrated, shell_records_removed, model_switch_records_removed)` defined in
  and importable from this module, returned by both singular and aggregate migration functions.
- State: per-file conversion indexes and one-category removal counts; aggregate stats sum each field.
- Responsibility: read legacy source bytes, classify each parsed removed record exactly once before
  field validation, preserve other malformed/unrecognized lines, convert supported records, and
  atomically replace the file. Shell classification takes precedence over model-switch when
  discriminators overlap. It does not use `SessionLog.read()` for source input; metrics backfill opens
  the completed final log through `SessionLog`.

**Done when:** Each modified component names its interface, state ownership, and responsibility,
with no overlapping durable-storage or delivery ownership.

### Data Model

```python
class Event(msgspec.Struct):
    persist: ClassVar[bool] = False

class PersistedEvent(Event):
    persist: ClassVar[bool] = True
```

The non-serialized `SessionEvent` tagged union contains all concrete event classes and exists only
for decoding.

`AssistantMessage` fields:

```text
id: str (required)
turn: int (required)
iteration: int (required)
scope: str | null (required)
request_id: str (required)
content: str (required)
interrupted: bool (required)
subagent_index: int | null (optional)
```

`Handshake`: `id`, `protocol_version`, `session_id`.

`SessionStatus`: `id`, `model_key`, `git_branch`.

The TUI uses an immutable named `RequestKey` with `scope`, `subagent_index`, `turn`, `iteration`, and
`request_id`; it does not use an unlabelled tuple.

`SessionLog` keeps a process-lifetime ordered list of valid unique-ID persisted events plus
`dict[event_id, canonical_line]`; JSONL remains the durable store. Construction scans the file once:
valid persisted events are canonically re-encoded for the index, malformed/unknown/live-only lines
are warned and skipped, identical later duplicate IDs are warned and skipped, and conflicting IDs
raise `EventIdConflict`. Because runtime writes go only through this instance, successful appends
update both in-memory structures after the durable write.

Storage exceptions are part of the shared log API:

```text
archie_shared.session.log.CursorNotFound
archie_shared.session.log.EventIdConflict
archie_shared.session.log.LogAppendError
```

Migration functions return the same immutable aggregate-friendly value defined in
`archie_shared.session.migrate`:

```python
@dataclass(frozen=True)
class MigrationStats:
    logs_migrated: int = 0
    shell_records_removed: int = 0
    model_switch_records_removed: int = 0
```

For `migrate_session_log()`, `logs_migrated` is zero or one. `migrate_session_logs()` adds each field
across per-file results. The CLI renders exactly:

```text
Migrated {logs_migrated} session logs; removed {shell_records_removed} shell records and {model_switch_records_removed} model-switch records; rebuilt metrics at {metrics_db}
```

**Done when:** Every changed entity has complete fields, required/optional status, identity, and
storage lifecycle.

### Data Flow

**SessionLog open**
1. Read existing JSONL once in file order.
2. Decode each line as a final persisted event; warn and skip malformed, unknown, and live-only lines.
3. Canonically encode each valid event before comparing/indexing its ID.
4. Keep the first canonical occurrence of an ID; warn and skip a later identical occurrence.
5. Raise `EventIdConflict` and fail construction on a later conflicting occurrence.
6. Retain the resulting unique-ID events in order for `read()` and their canonical lines for append
   idempotency.

**Persisted event**
1. Producer creates a typed event and calls `bus.emit()`.
2. The bus locks and calls `SessionLog.append()` because `persist=True`.
3. `append()` encodes/writes and returns `True`.
4. The bus canonically encodes and enqueues the event.
5. Stored and delivered forms decode to the same event and canonical encoding is byte-stable.

**Identical duplicate**
1. `append()` finds the same ID and canonical content and returns `False`.
2. The bus does not enqueue a second delivery.

**Live-only event**
1. The bus sees `persist=False`.
2. It encodes and enqueues without calling the log.

**Connection** — the sole public exception to calling `emit()` for queued events
1. The connect handler constructs `handshake` and `session_status` and calls
   `register_client(websocket, initial_events)`.
2. The bus validates the complete batch as live-only before changing registration state.
3. Under the ordering lock, register the client and enqueue both events through the same private
   encode/enqueue helper used by `emit()`.
4. Release the lock so concurrent producer events may follow; validation failure leaves the client
   unregistered with no queued frames.

**History read**
1. Agent route calls `SessionLog.read(after_id)`.
2. The log validates the cursor and returns subsequent persisted events in order.
3. The route canonically encodes the events as NDJSON.

**Model switch**
1. Validate idle state and model key; construct provider client.
2. Update runtime model.
3. Emit live-only `session_status`; persist no switch event.

**Root/child loop event**
1. `run_loop()` yields `LoopEvent`.
2. Adapter updates local state and asks `EventFactory` for a typed `SessionEvent`.
3. Adapter passes the event directly to `bus.emit()`.

**Assistant stream**
1. `text_delta` updates transient state by `RequestKey`.
2. Matching `assistant_message` finalizes/replaces only that transient display.
3. The key is removed from transient state and added to finalized state.

**Local shell**
1. TUI executes `docker exec` and captures bounded output.
2. TUI renders locally; no event or request leaves the TUI.

**Migration and reporting**
1. Parse each raw source line without final-event decoding.
2. Classify shell first when `role="shell"` or `type="shell_command"`; otherwise classify
   `type="model_switch"`; omit the selected record and increment exactly one counter.
3. Convert/copy all other records under the existing migration rules and atomically replace the file.
4. Return per-file `MigrationStats`; aggregate migration sums each field.
5. Rebuild metrics, then print the specified one-line aggregate summary.

**Done when:** Every principal happy path describes each component transition from input through
storage/delivery to observable client behavior.

### Error Handling & Edge Cases

- SessionLog open sees malformed, unknown, or live-only line → line-numbered warning and skip.
- SessionLog open sees identical duplicate ID/content → line-numbered warning, retain first occurrence.
- SessionLog open sees conflicting duplicate ID/content → `EventIdConflict`, fail construction/startup.
- Live-only event passed to `SessionLog.append()` → `TypeError`, no write.
- Identical append duplicate → `False`, no second delivery.
- Same append ID/different content → `EventIdConflict`, no delivery.
- Append failure → `LogAppendError`; retain storage notice and turn cleanup.
- Unknown read cursor → `CursorNotFound`, HTTP 409.
- Persistent targeted event → reject before append/enqueue.
- Connection initial-event validation failure → leave client unregistered and enqueue nothing.
- Client queue overflow → disconnect only that client.
- Invalid/active model switch → targeted `error_notice`, no state change.
- Status delivery lost after switch → runtime remains switched; reconnect sends current status.
- Root/child failure with partial text → interrupted assistant event before terminal.
- Buffered delta for finalized key → ignore.
- Unrelated request event → do not mutate another transient entry.
- Assistant migration with no direct request match → abort that file, leave original unchanged.
- Parsed object with `role="shell"` or `type="shell_command"` → omit once and increment only the
  shell counter, even if `type="model_switch"` is also present; `.legacy` retains the raw record.
- Otherwise, parsed object with `type="model_switch"` → omit once and increment only the model-switch
  counter; `.legacy` retains the raw record.
- Other malformed/unrecognized line → copy with a line-numbered warning.
- Intermediate 034 version-2 log → outside the supported contract and must be discarded before
  startup; no detection or compatibility behavior is promised.

Idempotency uses event ID plus canonical encoding.

**Done when:** Every external-input, storage, duplicate, connection, migration, and transient-state
failure has one explicit outcome.

### Code Structure

Create:

```text
shared/src/archie_shared/protocol.py
shared/src/archie_shared/commands.py
agent/src/archie_agent/loop_events.py
agent/src/archie_agent/event_factory.py
docs/session-protocol-walkthrough.md
```

Move/replace:

```text
shared/src/archie_shared/canonical_events.py → shared/src/archie_shared/events.py
agent/src/archie_agent/events.py             → agent/src/archie_agent/loop_events.py
agent/src/archie_agent/event_log.py          → agent/src/archie_agent/event_factory.py
```

Modify:

```text
shared/src/archie_shared/session/log.py
shared/src/archie_shared/session/migrate.py
shared/src/archie_shared/__init__.py
agent/src/archie_agent/session_bus.py
agent/src/archie_agent/app.py
agent/src/archie_agent/harness.py
agent/src/archie_agent/agents.py
agent/src/archie_agent/loop.py
cli/src/archie_cli/ws_client.py
cli/src/archie_cli/tui/app.py
orchestrator/src/archie_orchestrator/app.py
orchestrator/src/archie_orchestrator/proxy.py
orchestrator/src/archie_orchestrator/metrics.py
```

Update affected tests, `docs/event-spec.md`, and `docs/architecture.md`. Create the session protocol
walkthrough because `docs/session-protocol-walkthrough.md` is present only as an untracked planning
artifact and is not part of the clean 034 baseline.

**Done when:** Every new, moved, removed, and materially modified module has a specified destination
and responsibility.

### Patterns and Conventions

- Tagged strict `msgspec.Struct` for public protocol; dataclasses for internal loop events.
- Typed cursor, conflict, and storage exceptions are defined with `SessionLog` in
  `archie_shared.session.log` and imported from that shared boundary.
- ULID event IDs and append-before-delivery under the bus lock.
- Network writes remain outside the ordering lock.
- Tests target command/event codecs, `SessionLog`, `SessionEventBus`, HTTP history, WebSocket
  integration, and TUI event application rather than private implementation details.
- Existing structured logging and test layout remain authoritative.

**Done when:** Naming, serialization, error, concurrency, and testing conventions are concrete enough
to copy without establishing a new pattern.

### Infrastructure and Deployment

- No dependencies, services, environment variables, secrets, mounts, ports, database tables, or CI
  changes.
- Protocol and log schema remain version 2.
- Intermediate 034 development logs must be discarded.
- Supported older logs migrate directly to final version 2.

**Done when:** Every infrastructure and compatibility impact is explicitly named, including
categories with no changes.

### Non-Functional Concerns

- Reliability: append-before-delivery and event-ID idempotency remain.
- Concurrency: one ordering lock and isolated per-client sender queues remain.
- Performance: append remains amortized O(1) through the ID index; canonical encode is bounded to
  storage and delivery boundaries.
- Observability: malformed-line and skipped-identical-duplicate warnings include line numbers;
  migration prints aggregate migrated-log, shell-removal, and model-switch-removal counts.
- Security: strict command decoding rejects unknown fields and incomplete targets; trust boundary is
  unchanged.
- Storage: direct shell output can no longer expand session logs.

**Done when:** Reliability, concurrency, performance, observability, security, and storage each name
a concrete mechanism.

### Key Decisions

- `SessionLog.read()` over `replay()`: storage reads; clients assign replay meaning.
- `append() -> bool` over a result wrapper: callers need only inserted-versus-duplicate.
- Decoded reads over `LogRecord`: callers consume protocol objects; canonical encoding is stable.
- Class persistence over unions/tuples: the event definition is the only policy source.
- One `emit()` over producer-selected publish/broadcast paths; atomic connection registration is the
  sole exception because the client and its initial two live frames must enter one ordering-lock
  critical section.
- `session_status` over terminal status fields: model/branch are mutable session state.
- Live model status over persisted model transitions: requests preserve actual model use.
- Local shell over shell events: output is temporary and potentially large.
- Singular request provenance over cumulative IDs: messages are per request/iteration.
- Typed child emission over serialization callbacks: serialization belongs at boundaries.
- Separate loop events over merging all event layers: the pure loop remains transport-neutral.
- Imperative TUI application over a new reducer: presentation architecture is out of scope.
- Final version 2 in place over a v3 migration: the intermediate 034 contract was never used.

**Done when:** Every load-bearing choice names the selected and rejected approach with
repository-specific rationale.

### Risks and Open Questions

Risks:

- Broad renames may leave stale imports; mitigate with repository-wide searches and full tests.
- Request-keyed TUI state may regress reconnect; mitigate with root/child/sibling/offline tests.
- Migration removes history; mitigate with `.legacy`, counts, and atomic replacement.
- Intermediate version-2 logs are not distinguishable by version alone.
  - Mitigation: they are outside the supported contract, no such logs require preservation, and
    operators must discard 034 development logs before starting the final implementation.
- `msgspec` inheritance is contract-critical; pin it with focused tests.

Open questions: none.

**Done when:** Every risk has a mitigation and no implementation decision remains open.

## Milestones

1. **Prefactor internal event construction and child delivery**
   Approach:
   - This is an explicit prefactor: preserve the current public protocol and runtime behavior while
     making subsequent schema changes local and typed.
   - Rename internal modules to `loop_events.py` and `event_factory.py`; keep dataclasses for loop
     events and move `RequestContext`/`RequestFinished` with them.
   - `EventFactory` returns typed public events only. Introduce a temporary typed emitter callback
     for children that routes through the existing bus methods until milestone 3 installs `emit()`.
   - Test seam: root/child event sequences observed through current WebSocket/log behavior.
   Wiring:
   - State: existing harness/child iteration state is unchanged.
   - Producers: root harness and `run_child` call typed `EventFactory` methods.
   - Consumers: temporary typed emitter accepts public events and routes them to the current bus.
   - Call site: `run_child(..., emit=emit_event)`; no serialized callback.
   Edge cases:
   - Child terminal fallback construction failure: retain direct typed terminal fallback.
   - Storage failure: preserve current root/child handling and terminal guarantee.
   Tasks:
   - Rename internal event/factory modules and update imports/docstrings.
   - Move request context/completion event definitions into `loop_events.py`.
   - Change factory methods to return event objects only.
   - Replace child serialized callback plumbing with a typed event emitter.
   - Update focused root, child, and factory tests without changing public behavior.
   Deliverable: Root and child events reach the existing session bus as typed objects without a
   serialize/callback/decode round trip.
   Verify: Run `uv run pytest tests/test_loop.py tests/test_harness.py tests/test_agents.py
   tests/test_task_tool.py tests/test_subagent_accounting.py tests/test_subagent_interrupt.py -q` and
   search agent code to confirm no child callback accepts serialized event strings.

2. **Consolidate public protocol modules and flat commands**
   Approach:
   - Move the command contract to `archie_shared.commands`, the public event contract to
     `archie_shared.events`, and `PROTOCOL_VERSION` to `archie_shared.protocol`.
   - Use tagged strict `msgspec.Struct` commands and one decoder; retain current event fields and
     persistence aliases temporarily so this milestone remains behaviorally focused.
   - Keep protocol version 2; client and agent change together with no dual envelope decoder.
   - Test seam: serialized WebSocket command accepted by the agent and decoded server event accepted
     by `WSClient`.
   Wiring:
   - Producers: `WSClient` encodes flat commands.
   - Consumers: agent `/stream` decodes the same command union.
   - Call site: `encode_command(MessageCommand(content=...))`.
   Edge cases:
   - Unknown command tag/field: log and continue socket receive loop.
   - Partial interrupt target: reject command without invoking interrupt.
   Tasks:
   - Move modules and update shared exports/imports across all packages and tests.
   - Replace command dataclasses/manual JSON with strict tagged `msgspec` structs/codecs.
   - Flatten message, interrupt, and model-switch payloads.
   - Update protocol, WebSocket client, agent command dispatch, and command tests.
   - Remove stale canonical/wire terminology exposed by module names and docstrings.
   Deliverable: Client and agent communicate through flat version-2 `msgspec` commands while all
   public server events come from `archie_shared.events`.
   Verify: Run `uv run pytest tests/test_ws_client.py tests/test_ws_integration.py
   tests/test_model_switch.py tests/test_subagent_interrupt.py tests/test_protocol_version.py -q` and
   search for `archie_shared.canonical_events` and nested command `data` construction; both searches
   return no runtime matches.

3. **Make SessionLog stateful and unify event emission**
   Approach:
   - Add `Event`/`PersistedEvent` inheritance and non-serialized `persist`; retain one complete
     `SessionEvent` union for decoding.
   - Define `CursorNotFound`, `EventIdConflict`, and `LogAppendError` in
     `archie_shared.session.log`; `SessionLog` owns the path, ordered valid-event list, canonical ID
     index, `append() -> bool`, and ordered `read()`.
   - Initialize storage with one deterministic scan: canonicalize valid persisted events, warn/skip
     invalid or identical duplicate lines, and fail construction on conflicting duplicate IDs.
   - `SessionEventBus.emit()` becomes the sole runtime producer path. It calls `append()` before
     queueing persistent events and suppresses delivery when `append()` returns `False`.
   - Retain `register_client(websocket, initial_events)` as the sole queueing exception: validate the
     live-only batch first, then register/enqueue atomically through the same private helper as
     `emit()`.
   - Move startup `session_started` emission into async lifespan initialization through the bus,
     retaining the existing empty-log-only condition so process restart does not add another event.
   - Test seams: `SessionLog` construction/public API, bus/client queue output, `/events` NDJSON
     endpoint, and atomic connection admission.
   Wiring:
   - State: one fully initialized `SessionLog` created per session and injected into
     `SessionEventBus`.
   - Producers: startup, app handlers, root harness, and children call `emit()`; only the connect
     handler calls `register_client()` with `handshake` and `session_status`.
   - Consumers: `/events`, runtime accounting, and post-migration metrics backfill use `read()`;
     legacy migration source conversion continues to read raw lines through its dedicated converter.
   - Call sites: `await event_bus.emit(event, target=websocket_or_none)` and
     `await event_bus.register_client(websocket, (handshake, session_status))`.
   Edge cases:
   - Empty new log: emit exactly one `session_started`.
   - Existing valid log on restart: emit no additional `session_started`.
   - Existing malformed/unknown/live line: warn and skip; continue later valid lines.
   - Existing identical duplicate ID/content: warn and keep only the first event in ordered reads.
   - Existing conflicting duplicate ID/content: raise `EventIdConflict` and fail startup.
   - Non-canonical but valid existing line: canonicalize for index comparison and returned encoding.
   - Identical append duplicate: `False`, no duplicate queue delivery.
   - Conflicting append duplicate: typed conflict, no delivery.
   - Live event passed to log: reject.
   - Persistent targeted event: reject before mutation.
   - Unknown cursor: HTTP 409 through `CursorNotFound`.
   - Invalid initial registration batch: leave client unregistered and enqueue nothing.
   Tasks:
   - Implement event persistence inheritance and remove duplicate persisted classifications.
   - Move/define the three storage exceptions in `archie_shared.session.log` and update imports.
   - Replace module-level log helpers with stateful `SessionLog`; implement the startup scan, ordered
     event state, canonical index, append updates, and typed cursor handling.
   - Replace bus persistence/index code and public methods with `emit()` plus the explicit atomic
     `register_client()` exception sharing one private encode/enqueue helper.
   - Migrate every producer and targeted notice to `emit()` and the connect handler to
     `register_client()`.
   - Move initial session event emission into async startup.
   - Update history, accounting, coordinator, integration, startup-reconciliation, failure, and
     concurrency tests.
   Deliverable: Runtime producers use one ordered `emit()` path, connection admission is one explicit
   atomic exception, and `SessionLog` deterministically owns all durable storage/read behavior.
   Verify: Run `uv run pytest tests/test_canonical_session_events.py tests/test_session_log.py
   tests/test_session_bus.py tests/test_ws_integration.py tests/test_proxy_metrics_integration.py
   tests/test_harness.py -q`; cover raw-versus-canonical startup lines, invalid lines, identical and
   conflicting on-disk duplicates, typed exception import paths, failed registration validation, and
   handshake/status ordering; confirm no runtime calls to removed bus/log helpers remain.

4. **Replace model/status transitions with live session status**
   Approach:
   - Replace `status_updated` with live-only `session_status(model_key, git_branch)` and remove the
     persisted `model_switch` event.
   - Slim `handshake` to protocol/session identity. Keep handshake + status admission atomic through
     bus client registration.
   - On successful switch, update runtime model then broadcast current status. Request events remain
     authoritative for historical model use/cost.
   - Update the raw migration classifier in this milestone so records with `type="model_switch"` and
     no shell discriminator are dropped before the public class is removed; reserving shell
     precedence now keeps the final one-category classifier deterministic.
   - Introduce `MigrationStats` and the exact aggregate CLI summary before the first removal category
     ships; model-switch omissions increment only `model_switch_records_removed`, while the shell
     count remains zero until milestone 5.
   - Test seam: connect-frame ordering, live model-switch behavior through agent WebSocket, and
     migration fixtures asserting valid/malformed model-switch removal, `MigrationStats`, and the CLI
     summary.
   Wiring:
   - State: active model remains in `Session`; branch is read on status construction.
   - Producers: connect handler, successful model switch, root-turn cleanup.
   - Consumers: TUI status bar/model catalog mapping.
   - Call sites: status producers use `await bus.emit(SessionStatus(...))`; the connect handler uses
     `await bus.register_client(websocket, (Handshake(...), SessionStatus(...)))`.
   Edge cases:
   - Active/unknown model: targeted notice, no state change/status broadcast.
   - Client misses switch status: reconnect reports current model.
   - Status queue overflow: only stalled client disconnects; runtime state remains changed.
   Tasks:
   - Add immutable `MigrationStats`, change singular/aggregate migration return contracts, aggregate
     all fields, update the CLI summary, and adapt existing migration callers/tests.
   - Teach the raw migration converter to omit/count a parsed `type="model_switch"` record only when
     neither `role="shell"` nor `type="shell_command"` is present, without importing or decoding the
     public event class.
   - Replace event schemas and remove model-switch persistence.
   - Update connection, switch, and turn-finalization producers.
   - Update TUI status application and protocol mismatch handling.
   - Update tests for event set, handshake/status order, multi-client status, accounting invariance,
     migration return contracts/CLI output, and valid/malformed model-switch removal.
   Deliverable: Clients obtain current model and branch exclusively through live `session_status`,
   with no persisted model transition; migration returns `MigrationStats` and reports the exact
   aggregate CLI summary.
   Verify: Run `uv run pytest tests/test_git_branch.py tests/test_model_switch.py
   tests/test_protocol_version.py tests/test_ws_integration.py tests/test_tui_canonical.py
   tests/test_session_migration.py -q`; assert exact model-removal stats and CLI summary, then inspect
   `/events` after a switch and confirm no `model_switch` line exists.

5. **Make direct shell execution local-only**
   Approach:
   - Preserve host-side `docker exec`, timeout, cancellation, truncation, and UI formatting.
   - Render output directly in the initiating TUI; remove every network/event/persistence path for
     direct shell output.
   - Update the raw migration classifier before removing `ShellCommand`: parsed `role="shell"` or
     `type="shell_command"` takes precedence over `type="model_switch"`, is omitted, and increments
     only the shell counter without importing, constructing, or field-validating the public class.
   - Test seam: TUI shell submission with mocked subprocess, route-table assertions, and migration
     fixtures containing valid, malformed, and overlapping shell-discriminator records.
   Edge cases:
   - Timeout/cancel/non-zero exit: render locally with existing exit semantics.
   - Docker exec failure: render local client error.
   - Large output: retain the existing 10,000-line limit.
   - Parsed shell discriminator with malformed fields or an overlapping `type="model_switch"`: drop
     once and increment only the shell counter.
   - Malformed JSON with no readable discriminator: retain plan 034 copy-with-warning behavior.
   Tasks:
   - Teach the raw migration converter to omit/count parsed `role="shell"` and
     `type="shell_command"` records without importing or constructing `ShellCommand`.
   - Add migration tests for valid, malformed, and overlapping shell-discriminator records before
     removing the public class.
   - Render completed shell output directly from `_run_direct_shell`.
   - Remove shell IDs, fallback reconciliation, HTTP logging helpers, and replay handling.
   - Remove `ShellCommand` from the event schema.
   - Remove agent/orchestrator shell routes, proxies, imports, and route tests.
   - Replace persistence tests with local TUI execution/rendering tests.
   Deliverable: A `!` command executes and renders only in its initiating TUI and creates no network
   request or session event; migration no longer depends on the removed shell event class.
   Verify: Run `uv run pytest tests/test_cli_shell.py tests/test_tui_input.py
   tests/test_orchestrator_proxy.py tests/test_017_features.py tests/test_session_migration.py -q`;
   confirm test collection succeeds; valid, malformed, and overlapping shell-discriminator records
   are omitted once and increment only `shell_records_removed`; registered routes/event unions contain
   neither `/shell` nor `shell_command`.

6. **Make assistant streams request-scoped end to end**
   Approach:
   - Change `AssistantMessage` to singular `request_id` plus `iteration`; remove turn-cumulative
     request tracking from root and child producers.
   - Persist partial child text before error/interruption, matching root behavior.
   - Add immutable named `RequestKey` and request-keyed transient/finalized maps in the TUI.
   - Keep durable child lines separate from transient text; remove whole-line snapshots and reverse
     request-list scans.
   - Test seam: public event sequence plus TUI state/render output across live, history, and reconnect.
   Wiring:
   - State: producers track current request/iteration; TUI owns transient/finalized maps keyed by
     exact identity.
   - Producers: root/child assistant and delta event construction.
   - Consumers: `_apply_event()` root/child stream presentation and reconnect buffer dispatch.
   - Call site: `AssistantMessage(..., iteration=i, request_id=r)`.
   Edge cases:
   - Assistant has no deltas: render durable content directly.
   - Content differs from deltas: replace transient content with durable content.
   - Historical assistant before buffered delta: mark finalized and suppress delta.
   - Sibling/root interleaving: mutate only exact key.
   - Child disconnect mid-stream: preserve prior durable lines and replace only matching transient.
   - Error/interruption with partial text: emit interrupted message before terminal.
   Tasks:
   - Update assistant schema/factory and root/child producer state.
   - Add child partial-text terminal emission parity.
   - Rename the TUI handler to `_apply_event` and migrate call sites.
   - Implement `RequestKey`, transient/finalized state, and root/child reconciliation.
   - Remove cumulative/replayed request sets and child stream baselines.
   - Add focused event-order, multi-iteration, sibling, reconnect, and interruption tests.
   Deliverable: Live and reconnected root/child assistant text reconciles by exact request identity
   without erasing durable history or duplicating content.
   Verify: Run `uv run pytest tests/test_harness.py tests/test_agents.py tests/test_tui_canonical.py
   tests/test_tui_subagents.py tests/test_ws_integration.py -q`, including a regression asserting
   child lines remain `['first answer', 'second answer']` after reconnect.

7. **Retarget migration and metrics to the final version-2 schema**
   Approach:
   - Extend the existing atomic raw-line migration rather than add a version-3 migration.
   - The migration must not use `SessionLog.read()` for source input: legacy, malformed, and removed
     records are not members of the final persisted event model and must remain available for
     conversion, copying, counting, or omission.
   - Build a request identity index before converting assistants; the final request ID in the legacy
     ordered list is the direct producer, and its `llm_request` supplies iteration.
   - Finalize the discriminator-first shell/model removal classifiers introduced before their public
     classes were removed in milestones 4 and 5: shell wins overlaps, each omitted line increments
     exactly one category, originals remain in `.legacy`, and both migration functions return
     aggregate-friendly `MigrationStats`.
   - Test seam: migrated JSONL decoded by the final event union, exact per-file/aggregate statistics
     and CLI summary, plus metrics DB backfill totals.
   Wiring:
   - State: per-file raw source lines, request index, and immutable `MigrationStats`; aggregate
     migration sums `logs_migrated`, `shell_records_removed`, and `model_switch_records_removed`.
   - Producers: host `migrate-sessions` command and dedicated migration converter.
   - Consumers: atomic output writer; aggregate CLI summary; after replacement, metrics backfill opens
     the final log through `SessionLog.read()`.
   - Call sites: `migrate_session_log(path) -> MigrationStats` and
     `migrate_session_logs(paths) -> MigrationStats`; no new command.
   Edge cases:
   - Empty/missing assistant request list or unmatched request: abort file, preserve original.
   - Existing `.legacy`: preserve existing collision/force behavior.
   - Parsed `role="shell"` or `type="shell_command"`: drop once and increment only shell, including
     when `type="model_switch"` also appears.
   - Otherwise parsed `type="model_switch"`: drop once and increment only model-switch.
   - Skipped schema-version-2 file: return zero for all `MigrationStats` fields.
   - Malformed JSON with no readable discriminator or other unrecognized line: copy with warning per
     plan 034.
   - Backfill failure: migrated logs remain; backfill remains safely rerunnable.
   Tasks:
   - Convert legacy assistant identity to singular request/iteration.
   - Integrate and finalize the existing shell/model precedence, output omission, and one-category
     counters without reintroducing removed public classes.
   - Preserve the `MigrationStats` contracts introduced in milestone 4 and verify aggregate sums
     across migrated and skipped files.
   - Preserve and verify the exact aggregate CLI summary introduced in milestone 4 after metrics
     backfill.
   - Update migration output and final schema-version-2 decode assertions.
   - Migrate metrics backfill to `SessionLog.read()` for the completed final log and update event
     imports.
   - Expand idempotency, failure, backup, decode, and accounting tests.
   Deliverable: Supported legacy logs migrate atomically into final schema version 2, return exact
   aggregate removal statistics, print the specified CLI summary, and rebuild request metrics without
   obsolete records.
   Verify: Run `uv run pytest tests/test_session_migration.py tests/test_orchestrator_metrics.py
   tests/test_orchestrator_metrics_api.py tests/test_canonical_session_events.py -q`; cover per-file,
   multi-file, skipped-file, malformed-field, and overlapping-discriminator fixtures; assert exact
   `MigrationStats`, exact CLI summary, final decode, preserved `.legacy` data, and accounting totals.

8. **Finalize documentation and repository validation**
   Approach:
   - Document only final behavior; do not describe intermediate milestone states.
   - Use “read” for storage and “replay” only for client application of history.
   - Name `_apply_event` an imperative event handler, not a reducer.
   - Test seam: documentation links/schema examples and repository-wide stale-name searches.
   Tasks:
   - Update the event specification and architecture documentation.
   - Create `docs/session-protocol-walkthrough.md` on the implementation branch using the verified
     untracked draft as source, then update it to the final protocol.
   - Update README or contributor links only where current names/routes are referenced.
   - Search for removed modules, events, fields, routes, envelopes, and APIs.
   - Run focused and complete quality checks; correct all failures.
   Deliverable: Documentation and repository checks describe and validate one final event protocol
   with no transitional names or paths.
   Verify: Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, and
   `git diff --check`; search for `canonical_events`, `PersistedEventTypes`, `request_ids`,
   `status_updated`, `model_switch`, `shell_command`, `/shell`, `broadcast_serialized`, and nested
   command `data` envelopes, allowing only migration fixtures/documented legacy decoding where
   explicitly required.

## Sequencing

Milestones are strictly dependency ordered. Milestone 1 is a behavior-preserving prefactor.
Milestone 2 establishes final public module and command names. Milestone 3 installs the final
storage/emission boundary required by all later event removals. Milestones 4 and 5 remove independent
status/shell protocol surfaces. Milestone 6 changes assistant identity and TUI reconciliation as one
vertical slice. Milestone 7 can only target the schema after all final event shapes are fixed.
Milestone 8 validates the complete system.

## Non-goals

- A standalone pure TUI reducer or broader presentation-state rewrite.
- Authentication, TLS, remote-client negotiation, or command acknowledgement/idempotency.
- Persistence or cross-client broadcast of direct shell output.
- Immediate branch refresh after local shell commands or arbitrary external workspace changes.
- Compatibility with logs or clients created only by the intermediate 034 implementation.
- Merging provider, loop, and public event layers into one type system.

## Plan Status

| Phase | Status |
| --- | --- |
| Objective and requirements | Approved |
| Technical design | Approved |
| Milestones | Approved |
| Independent review | Complete — two formal passes plus external audit; all findings resolved |
| Implementation | Ready to start |

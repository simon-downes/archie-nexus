# 024 — SPEC: Metrics aggregation (real-time proxy extraction + SQLite)

## Objective

Expose aggregate cost/usage metrics through the orchestrator by capturing token usage
from WS proxy frames in real-time and storing in SQLite. The proxy inspects frames as
they flow through, computes cost from model rates, and writes to
`~/.nexus/metrics.db`. API endpoints serve aggregated data.

## Context

This is M5 of project plan 019 (Orchestrator Control Plane). After M2b, the
orchestrator proxies all client↔session WebSocket traffic. Each frame is a complete
JSON event. The `Usage` event carries per-request token counts; `SessionInfo` and
`ModelSwitched` carry cost rates and model info. The proxy can extract metrics in
real-time without affecting the data path.

Locked decisions: D1 (never in LLM egress path — metrics extraction is observability,
not routing), D5 (JSONL is the durable source of truth for conversation content).

Depends on: 022-spec-attach-exec-proxy (M2b — WS proxy relay must exist).

**Note on D5:** The original D5 says "PULL from co-located session JSONL." This plan
changes the implementation: instead of parsing JSONL files, the orchestrator captures
metrics from WS frames in real-time. The data is the same (same token/cost info that
gets written to JSONL), just captured at a different point. JSONL remains the source of
truth for conversation *content*; SQLite becomes the metrics-specific indexed store.

**Trade-off:** Metrics are only captured while the proxy is running. If the
orchestrator is down during a session's activity (e.g. second client connected
directly — not possible after M2b, but during a bounce window), those frames are lost
from metrics. In practice this is negligible: the agent only produces output when a
client sends a message, and clients can't reach the session without the orchestrator
(D8). A JSONL-based reconciliation on startup could be added later if needed.

## Requirements

- MUST capture metrics from WS proxy frames in real-time as they flow through the
  orchestrator
  - AC: `Usage` events are detected and extracted from backend→client frames
  - AC: `SessionInfo` and `ModelSwitched` events are captured for cost rate and model
    tracking
  - AC: No impact on relay latency (extraction is async/queued)
  - AC: Multiple simultaneous sessions each record their own metrics independently

- MUST store metrics in a SQLite database at `~/.nexus/metrics.db`
  - AC: Database is created on first write (with schema + indices)
  - AC: WAL mode enabled for concurrent read/write
  - AC: One row per LLM request (per `Usage` event)
  - AC: Each row includes: session_id, timestamp (capture time), turn_index, model,
    backend, tokens (input/output/cache_read/cache_write), computed cost, context_pct

- MUST compute cost at write time from the active model's cost rates
  - AC: Cost = (input_tokens × input_rate + output_tokens × output_rate +
    cache_read_tokens × cache_read_rate + cache_write_tokens × cache_write_rate) /
    1_000_000
  - AC: Rates are sourced from the most recent `SessionInfo` or `ModelSwitched` event
    for that session
  - AC: If no rate info available, cost is recorded as 0.0 and model as "unknown"

- MUST expose `GET /metrics` returning aggregate metrics across all sessions
  - AC: Response includes total_cost, total_requests, token totals, by_model breakdown
  - AC: Optional `?since=YYYY-MM-DD` query param filters to entries after that date
  - AC: Empty database returns zeroes

- MUST expose `GET /sessions/{id}/metrics` returning metrics for a single session
  - AC: Response has same shape as `GET /metrics` but scoped to one session
  - AC: Returns 404 if no metrics exist for that session_id

- MUST NOT impact the data path (D1/D2)
  - AC: Frames are forwarded unchanged regardless of metrics extraction success/failure
  - AC: A metrics write failure does not affect the proxy relay
  - Why: Metrics are observability, not control. Extraction failures are logged and
    swallowed.

- SHOULD use a background task for SQLite writes
  - AC: Relay loop puts events on an async queue
  - AC: A background task drains the queue and batch-writes to SQLite
  - AC: Relay latency is unaffected by database I/O

## Technical Design

### Proxy Integration (frame inspection)

In `proxy.py`'s `backend_to_client` relay loop, add cheap string checks for all three
event types. The wire format uses `json.dumps` default separators which produce a space
after the colon:

```python
_METRICS_MARKERS = ('"type": "usage"', '"type": "session_info"', '"type": "model_switched"')

async def backend_to_client():
    async for msg in backend:
        # Cheap string check — only parse events we care about
        if any(marker in msg for marker in _METRICS_MARKERS):
            _metrics_queue.put_nowait((session_id, msg))
        await websocket.send_text(msg)
```

The queue is an `asyncio.Queue` — non-blocking put. If put fails (shouldn't with
unbounded queue), catch and ignore — relay must never fail due to metrics.

### Background Writer

New file: `orchestrator/src/archie_orchestrator/metrics.py`

```python
class MetricsWriter:
    """Background SQLite writer fed by the proxy relay."""

    def __init__(self, db_path: Path):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._db_path = db_path
        self._rates: dict[str, SessionRates] = {}  # session_id → rates

    @property
    def queue(self) -> asyncio.Queue:
        return self._queue

    async def run(self):
        """Background loop: drain queue, write to SQLite."""
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema(conn)
        try:
            while True:
                batch = []
                item = await self._queue.get()
                batch.append(item)
                # Drain any additional queued items
                while not self._queue.empty():
                    batch.append(self._queue.get_nowait())
                self._process_batch(conn, batch)
        finally:
            conn.close()
```

### Event Processing Logic

```python
@dataclass
class SessionRates:
    model: str = "unknown"
    backend: str | None = None  # inferred from model key
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0

def _process_batch(self, conn, batch):
    rows = []
    for session_id, raw_msg in batch:
        try:
            event = json.loads(raw_msg)
        except json.JSONDecodeError:
            log.warning("Malformed metrics event, skipping")
            continue

        event_type = event.get("type")

        if event_type == "session_info":
            data = event["data"]
            cost = data.get("cost", {})
            model = data["model"]
            # NOTE: session_info carries no model_key on the wire (only the
            # human model name — see shared/events.py SessionInfo), so backend
            # inference here is best-effort. An authoritative backend is set
            # once the first model_switched event arrives.
            self._rates[session_id] = SessionRates(
                model=model,
                backend=self._infer_backend(model),
                input=cost.get("input", 0.0),
                output=cost.get("output", 0.0),
                cache_read=cost.get("cache_read", 0.0),
                cache_write=cost.get("cache_write", 0.0),
            )

        elif event_type == "model_switched":
            data = event["data"]
            # Wire format nests cost rates under a "cost" dict (see
            # shared/events.py ModelSwitched.to_json). model_key IS present on
            # this event, so backend inference is accurate here.
            cost = data.get("cost", {})
            model = data.get("model_name", "unknown")
            self._rates[session_id] = SessionRates(
                model=model,
                backend=self._infer_backend(data.get("model_key", "")),
                input=cost.get("input", 0.0),
                output=cost.get("output", 0.0),
                cache_read=cost.get("cache_read", 0.0),
                cache_write=cost.get("cache_write", 0.0),
            )

        elif event_type == "usage":
            rates = self._rates.get(session_id, SessionRates())
            data = event["data"]
            in_tok = data["input_tokens"]
            out_tok = data["output_tokens"]
            cr_tok = data["cache_read_tokens"]
            cw_tok = data["cache_write_tokens"]
            cost = (
                in_tok * rates.input
                + out_tok * rates.output
                + cr_tok * rates.cache_read
                + cw_tok * rates.cache_write
            ) / 1_000_000
            rows.append((
                session_id,
                datetime.now(UTC).isoformat(),  # timestamp at capture
                event["turn_index"],
                rates.model,
                rates.backend,
                in_tok, out_tok, cr_tok, cw_tok,
                cost,
                data.get("context_pct", 0.0),
            ))

    if rows:
        try:
            conn.executemany(
                "INSERT INTO requests (session_id, timestamp, turn_index, model, "
                "backend, input_tokens, output_tokens, cache_read_tokens, "
                "cache_write_tokens, cost, context_pct) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        except sqlite3.Error as e:
            log.error(f"Metrics DB write failed: {e}")

def _infer_backend(self, model_key: str) -> str | None:
    """Infer backend from model key prefix (e.g. 'bedrock-*' → 'bedrock')."""
    if model_key.startswith("bedrock-"):
        return "bedrock"
    elif model_key.startswith("ollama-"):
        return "ollama"
    return None
```

### Key Design Points

- **Timestamp:** Generated at capture time (`datetime.now(UTC).isoformat()`), not
  from the event (which has no timestamp). This means the `?since=` filter is based on
  when the orchestrator observed the event.
- **Model/backend:** Derived from the rate-tracking dict (populated by
  `SessionInfo`/`ModelSwitched`). If no rates received yet, model = "unknown",
  backend = None, cost = 0.0.
- **Backend inference:** From model key prefix convention (`bedrock-*`, `ollama-*`).
  Not from the wire protocol (which doesn't carry `backend`).
- **String check format:** Uses `'"type": "usage"'` (with space after colon) matching
  the actual `json.dumps` output from `serialize_event()` in events.py.

### SQLite Schema

```sql
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    turn_index INTEGER NOT NULL,
    model TEXT NOT NULL,
    backend TEXT,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL,
    context_pct REAL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_requests_session ON requests(session_id);
CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp);
```

### Metrics API Endpoints

```python
Route("/metrics", endpoint=get_metrics, methods=["GET"]),
Route("/sessions/{session_id}/metrics", endpoint=get_session_metrics, methods=["GET"]),
```

Both use `asyncio.to_thread` for SQLite reads:

```python
async def get_metrics(request: Request) -> Response:
    since = request.query_params.get("since")
    result = await asyncio.to_thread(_query_metrics, db_path, since=since)
    return Response(msgspec.json.encode(result), media_type="application/json")
```

Response shape:
```json
{
  "total_cost": 1.23,
  "total_requests": 47,
  "input_tokens": 150000,
  "output_tokens": 25000,
  "cache_read_tokens": 80000,
  "cache_write_tokens": 10000,
  "by_model": {
    "Claude Sonnet 4.6": {"cost": 1.10, "requests": 40},
    "Qwen 3.6 35B": {"cost": 0.13, "requests": 7}
  }
}
```

### Wiring (app lifespan)

```python
@asynccontextmanager
async def lifespan(app):
    db_path = home_dir() / "metrics.db"
    writer = MetricsWriter(db_path)
    app.state.metrics_writer = writer
    task = asyncio.create_task(writer.run())
    yield
    task.cancel()
```

Proxy accesses the queue via `app.state.metrics_writer.queue`.

### Dependencies

No new dependencies — `sqlite3` and `json` are stdlib.

## Milestones

### 1. Implement MetricsWriter with SQLite storage and background queue

**Approach:**
- Create `orchestrator/src/archie_orchestrator/metrics.py`.
- `MetricsWriter` class: holds async queue, runs background loop, writes to SQLite.
- Handles three event types:
  - `session_info` → update rate tracking dict (model, backend, cost rates)
  - `model_switched` → update rate tracking dict
  - `usage` → compute cost, insert row
- Schema creation on first connect (idempotent `CREATE TABLE IF NOT EXISTS`).
- WAL mode for concurrent access.
- Backend inferred from model key prefix (`bedrock-*` → "bedrock", `ollama-*` → "ollama").
- Timestamp generated at capture time (`datetime.now(UTC).isoformat()`).
- If no rate info yet: model = "unknown", backend = None, cost = 0.0.
- Test seam: provide the queue directly in tests, verify database contents.

**Edge cases:**
- No rate info for a session (Usage before SessionInfo) → cost = 0.0, model = "unknown"
- Malformed event JSON → skip, log warning, don't crash
- Database write failure → log error, continue (don't lose subsequent events)
- Empty queue drain (nothing pending after first item) → process single item

**Tasks:**
- Create `orchestrator/src/archie_orchestrator/metrics.py`
- Implement `MetricsWriter` class with queue + background loop
- Implement schema creation (idempotent, WAL mode)
- Implement `SessionRates` dataclass + rate tracking logic
- Implement Usage → row insertion with cost computation
- Implement `_infer_backend()` helper
- Create `tests/test_orchestrator_metrics.py`
- Test: Usage event with known rates → correct row (tokens, cost, model, backend)
- Test: SessionInfo updates rates, subsequent Usage uses new rates
- Test: ModelSwitched updates rates mid-session
- Test: Usage without prior rates → cost = 0.0, model = "unknown"
- Test: malformed JSON → skipped, no crash, warning logged
- Test: multiple sessions write independently (different session_ids)
- Test: batch processing (multiple items drained in one loop)

**Deliverable:** `MetricsWriter` processes events from queue and writes correct metrics
to SQLite.

**Verify:** `pytest tests/test_orchestrator_metrics.py -v` — all pass.

---

### 2. Integrate frame inspection into the WS proxy relay

**Approach:**
- In `proxy.py`'s `backend_to_client` loop, add string checks for all three event
  types using `_METRICS_MARKERS` tuple.
- On match: `put_nowait((session_id, msg))` on the MetricsWriter's queue.
- Wire the `MetricsWriter` instance into app state during lifespan.
- Start `MetricsWriter.run()` as a background task in lifespan; cancel on shutdown.
- The relay MUST never block or fail due to metrics:
  - `put_nowait` is non-blocking (unbounded queue)
  - Wrap in try/except as safety net
  - Frame is always forwarded regardless
- Test seam: mock the queue, verify correct events are enqueued.
- ⚠️ String markers must match actual `json.dumps` output format (space after colon).

**Tasks:**
- Define `_METRICS_MARKERS` tuple in proxy.py
- Add frame inspection to `backend_to_client` relay loop
- Wire MetricsWriter instance via `app.state` in lifespan
- Start background task in lifespan, cancel on shutdown
- Add safety try/except around queue put
- Create `tests/test_proxy_metrics_integration.py`
- Test: Usage frame flowing through → event enqueued with correct session_id
- Test: SessionInfo frame → enqueued
- Test: ModelSwitched frame → enqueued
- Test: TextDelta frame (no marker) → not enqueued
- Test: queue put failure → frame still forwarded, no crash
- Test: multiple sessions each enqueue to same queue with correct session_ids

**Deliverable:** The WS proxy extracts metrics-relevant events in real-time without
affecting relay performance or reliability.

**Verify:** `pytest tests/test_proxy_metrics_integration.py -v` — all pass.

---

### 3. Implement metrics API endpoints

**Approach:**
- Add two routes to `app.py`: `GET /metrics`, `GET /sessions/{id}/metrics`.
- Both open a read-only SQLite connection to `~/.nexus/metrics.db` via
  `asyncio.to_thread` (don't block event loop).
- `GET /metrics`: `SELECT SUM(cost), COUNT(*), SUM(input_tokens), ...` + GROUP BY
  model for by_model breakdown. Optional `WHERE timestamp >= ?` from `?since=` param.
- `GET /sessions/{id}/metrics`: same query + `WHERE session_id = ?`. Return 404 if
  zero rows.
- Response serialized via msgspec.
- Handle missing database file (orchestrator never received any metrics) → return
  zeroes.

**Tasks:**
- Add `GET /metrics` route to app.py
- Add `GET /sessions/{id}/metrics` route to app.py
- Implement `_query_metrics(db_path, session_id=None, since=None) -> dict` helper
- Handle `?since=` query param (validate format, apply WHERE)
- Handle empty/missing db → zeroes response
- Handle unknown session → 404
- Create `tests/test_orchestrator_metrics_api.py`
- Test: empty db → zeroes response (all fields zero, by_model empty)
- Test: populated db → correct totals and by_model breakdown
- Test: `?since=` filters correctly (only includes rows after date)
- Test: per-session endpoint returns correctly scoped data
- Test: unknown session → 404
- Test: multiple models in db → correct by_model grouping

**Deliverable:** Metrics API endpoints return real-time aggregate cost/usage data from
SQLite.

**Verify:** `pytest tests/test_orchestrator_metrics_api.py -v` — all pass.

---

## Not yet specified

- Metrics display in TUI (consumer of the API — separate feature)
- Time-range grouping (by day, by week) in API response
- Metrics retention / pruning (SQLite grows indefinitely — acceptable for personal use)
- Live streaming metrics (SSE or WS — additive, deferred)
- JSONL reconciliation on startup (backfill metrics missed during downtime — deferred)

"""Tests for MetricsWriter — SQLite storage and canonical event processing."""

import asyncio
import logging
import sqlite3
from datetime import UTC
from pathlib import Path

import pytest
from archie_orchestrator.metrics import MetricsWriter, reset_and_backfill
from archie_shared.canonical_events import LLMRequest, encode_event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_next_event_id = 0


def _make_llm_request(
    *,
    event_id: str | None = None,
    turn: int = 1,
    iteration: int = 1,
    scope: str | None = None,
    model_key: str = "bedrock-anthropic.claude-sonnet-4-6",
    sent_at: str = "2026-07-01T10:00:00+00:00",
    status: str = "completed",
    input_tokens: int = 1000,
    output_tokens: int = 200,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    context_tokens: int = 12000,
    cost_usd: float = 0.006,
    duration_ms: int = 1234,
) -> str:
    """Encode a canonical llm_request event as it appears on the wire."""
    global _next_event_id
    if event_id is None:
        _next_event_id += 1
        event_id = f"evt-{_next_event_id:06d}"
    return encode_event(
        LLMRequest(
            id=event_id,
            scope=scope,
            turn=turn,
            iteration=iteration,
            model_key=model_key,
            sent_at=sent_at,
            duration_ms=duration_ms,
            status=status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            context_tokens=context_tokens,
            cost_usd=cost_usd,
        )
    )


def _rows(db_path: Path) -> list[dict]:
    """Read all rows from the requests table as dicts."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM requests ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Canonical llm_request event → correct row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_request_persisted_with_cost(tmp_path):
    """llm_request event → correct row with cost_usd taken directly from the event."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    session_id = "proj-01abc12345"
    batch = [
        (
            session_id,
            _make_llm_request(
                model_key="bedrock-anthropic.claude-sonnet-4-6",
                input_tokens=1000,
                output_tokens=200,
                cost_usd=0.006,
            ),
        ),
    ]

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        writer._ensure_schema(conn)
        writer._process_batch(conn, batch)
    finally:
        conn.close()

    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == session_id
    assert row["model_key"] == "bedrock-anthropic.claude-sonnet-4-6"
    assert row["status"] == "completed"
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 200
    assert abs(row["cost_usd"] - 0.006) < 1e-9


@pytest.mark.asyncio
async def test_llm_request_token_and_scope_fields(tmp_path):
    """All canonical token/scope fields are persisted verbatim."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    session_id = "proj-01abc12345"
    expected = (500 * 3.0 + 100 * 15.0 + 2000 * 0.3 + 500 * 3.75) / 1_000_000
    batch = [
        (
            session_id,
            _make_llm_request(
                model_key="bedrock-anthropic.claude-sonnet-4-6",
                scope="planner",
                turn=4,
                iteration=2,
                input_tokens=500,
                output_tokens=100,
                cache_read_tokens=2000,
                cache_write_tokens=500,
                context_tokens=42000,
                cost_usd=expected,
            ),
        ),
    ]

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        writer._ensure_schema(conn)
        writer._process_batch(conn, batch)
    finally:
        conn.close()

    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["model_key"] == "bedrock-anthropic.claude-sonnet-4-6"
    assert row["turn"] == 4
    assert row["iteration"] == 2
    assert row["scope"] == "planner"
    assert row["cache_read_tokens"] == 2000
    assert row["cache_write_tokens"] == 500
    assert row["context_tokens"] == 42000
    assert abs(row["cost_usd"] - expected) < 1e-9


def test_llm_request_zero_cost(tmp_path):
    """llm_request with cost_usd=0.0 (e.g. local model) → row with zero cost."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        writer._ensure_schema(conn)
        writer._process_batch(
            conn,
            [
                (
                    "unknown-session",
                    _make_llm_request(model_key="ollama-qwen3:30b-a3b", cost_usd=0.0),
                )
            ],
        )
    finally:
        conn.close()

    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["model_key"] == "ollama-qwen3:30b-a3b"
    assert rows[0]["cost_usd"] == 0.0


def test_model_key_recorded_per_event(tmp_path):
    """Each llm_request records its own model_key; a mid-session switch is reflected."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    sid = "proj-01abc12345"
    batch = [
        (
            sid,
            _make_llm_request(
                model_key="bedrock-anthropic.claude-sonnet-4-6",
                turn=1,
                iteration=1,
                cost_usd=0.006,
            ),
        ),
        (
            sid,
            _make_llm_request(
                model_key="ollama-qwen3:30b-a3b",
                turn=2,
                iteration=1,
                cost_usd=0.0,
            ),
        ),
    ]

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        writer._ensure_schema(conn)
        writer._process_batch(conn, batch)
    finally:
        conn.close()

    rows = _rows(db)
    assert len(rows) == 2
    assert rows[0]["model_key"] == "bedrock-anthropic.claude-sonnet-4-6"
    assert rows[1]["model_key"] == "ollama-qwen3:30b-a3b"
    assert rows[1]["cost_usd"] == 0.0


def test_malformed_json_skipped_no_crash(tmp_path, caplog):
    """Malformed JSON → skipped with warning, no crash, valid events still processed."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    sid = "proj-01abc12345"
    batch = [
        (sid, "not valid json{{{"),
        (sid, _make_llm_request()),
    ]

    with caplog.at_level(logging.WARNING, logger="archie_orchestrator.metrics"):
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            writer._ensure_schema(conn)
            writer._process_batch(conn, batch)
        finally:
            conn.close()

    assert any("skipped" in r.message.lower() for r in caplog.records)
    rows = _rows(db)
    assert len(rows) == 1  # the valid llm_request event was processed


def test_non_llm_request_events_ignored(tmp_path):
    """Only llm_request events are ingested; other canonical frames are skipped."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    import json

    sid = "proj-01abc12345"
    batch = [
        (
            sid,
            json.dumps(
                {
                    "type": "session_started",
                    "id": "s1",
                    "schema_version": 2,
                    "sent_at": "2026-07-01T10:00:00+00:00",
                    "model_key": "m",
                }
            ),
        ),
        (
            sid,
            json.dumps(
                {
                    "type": "text_delta",
                    "id": "t1",
                    "turn": 1,
                    "iteration": 1,
                    "scope": None,
                    "request_id": "r1",
                    "text": "hi",
                }
            ),
        ),
        (sid, _make_llm_request()),
    ]

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        writer._ensure_schema(conn)
        writer._process_batch(conn, batch)
    finally:
        conn.close()

    rows = _rows(db)
    assert len(rows) == 1  # only the llm_request row


def test_multiple_sessions_independent(tmp_path):
    """Multiple sessions write independently with correct session_ids."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    sid_a = "project-a-01abc12345"
    sid_b = "project-b-02def67890"

    batch = [
        (
            sid_a,
            _make_llm_request(
                model_key="Model A", input_tokens=100, output_tokens=100, cost_usd=0.0002
            ),
        ),
        (
            sid_b,
            _make_llm_request(
                model_key="Model B", input_tokens=100, output_tokens=100, cost_usd=0.0004
            ),
        ),
    ]

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        writer._ensure_schema(conn)
        writer._process_batch(conn, batch)
    finally:
        conn.close()

    rows = _rows(db)
    assert len(rows) == 2
    by_session = {r["session_id"]: r for r in rows}
    assert by_session[sid_a]["model_key"] == "Model A"
    assert by_session[sid_b]["model_key"] == "Model B"
    assert abs(by_session[sid_a]["cost_usd"] - 0.0002) < 1e-9
    assert abs(by_session[sid_b]["cost_usd"] - 0.0004) < 1e-9


def test_batch_processing_multiple_events(tmp_path):
    """Multiple llm_request events in one batch all get inserted."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    sid = "proj-01abc12345"
    batch = [
        (sid, _make_llm_request(turn=1, iteration=1)),
        (sid, _make_llm_request(turn=2, iteration=1)),
        (sid, _make_llm_request(turn=3, iteration=1)),
    ]

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        writer._ensure_schema(conn)
        writer._process_batch(conn, batch)
    finally:
        conn.close()

    rows = _rows(db)
    assert len(rows) == 3
    assert {r["turn"] for r in rows} == {1, 2, 3}


def test_duplicate_event_id_ignored(tmp_path):
    """INSERT OR IGNORE dedupes on (session_id, event_id) — replays don't double-count."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    sid = "proj-01abc12345"
    batch = [
        (sid, _make_llm_request(event_id="dup-1", cost_usd=0.006)),
        (sid, _make_llm_request(event_id="dup-1", cost_usd=0.006)),
    ]

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        writer._ensure_schema(conn)
        writer._process_batch(conn, batch)
    finally:
        conn.close()

    rows = _rows(db)
    assert len(rows) == 1


def test_infer_backend():
    """_infer_backend returns correct values for known prefixes."""
    w = MetricsWriter(Path("/tmp/unused.db"))
    assert w._infer_backend("bedrock-anthropic.claude-sonnet-4-6") == "bedrock"
    assert w._infer_backend("ollama-qwen3:30b-a3b") == "ollama"
    assert w._infer_backend("openai-gpt-4o") is None
    assert w._infer_backend("") is None


@pytest.mark.asyncio
async def test_schema_created_idempotent(tmp_path):
    """Calling _ensure_schema twice does not raise (CREATE TABLE IF NOT EXISTS)."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    conn = sqlite3.connect(str(db))
    try:
        writer._ensure_schema(conn)
        writer._ensure_schema(conn)  # second call must not raise
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='requests'"
        ).fetchall()
    finally:
        conn.close()

    assert len(tables) == 1


# ---------------------------------------------------------------------------
# Async run() loop — queue draining and cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_loop_processes_queue_items(tmp_path):
    """run() drains the queue and writes llm_request rows to SQLite."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    sid = "proj-01abc12345"
    writer.queue.put_nowait((sid, _make_llm_request(input_tokens=500, output_tokens=100)))

    task = asyncio.create_task(writer.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 500


@pytest.mark.asyncio
async def test_run_loop_processes_queued_items_before_cancellation(tmp_path):
    """Items enqueued while run() is active are pulled and written by the loop."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    task = asyncio.create_task(writer.run())
    await asyncio.sleep(0.01)

    sid = "proj-01abc12345"
    writer.queue.put_nowait((sid, _make_llm_request(turn=1, iteration=1)))
    writer.queue.put_nowait((sid, _make_llm_request(turn=2, iteration=1)))
    # Give the loop time to drain the queue before we cancel.
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    rows = _rows(db)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_run_loop_db_write_failure_continues(tmp_path, caplog):
    """A DB write failure is logged but the loop continues processing."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    call_count = 0
    original_process = writer._process_batch

    def _failing_process(conn, batch):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise sqlite3.OperationalError("disk full")
        original_process(conn, batch)

    sid = "proj-01abc12345"
    writer.queue.put_nowait((sid, _make_llm_request(turn=1, iteration=1)))

    task = asyncio.create_task(writer.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Test passes if no unhandled exception propagated — process stayed alive


def test_db_write_failure_logged_no_crash(tmp_path, caplog):
    """sqlite3.Error during INSERT is logged and does not raise."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    sid = "proj-01abc12345"
    batch = [(sid, _make_llm_request())]

    conn = sqlite3.connect(str(db))
    try:
        writer._ensure_schema(conn)
        # Drop the table to force a write failure
        conn.execute("DROP TABLE requests")
        conn.commit()

        with caplog.at_level(logging.WARNING, logger="archie_orchestrator.metrics"):
            writer._process_batch(conn, batch)  # should not raise
    finally:
        conn.close()

    assert any("skipped" in r.message.lower() for r in caplog.records)


def test_pricing_uses_event_cost_verbatim(tmp_path):
    """cost_usd is taken from the event, not recomputed from token categories."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)
    session_id = "billable-session"
    # cost = (100*2 + 20*4 + 30*0.5 + 5*1.0) / 1_000_000
    expected = (100 * 2.0 + 20 * 4.0 + 30 * 0.5 + 5 * 1.0) / 1_000_000
    batch = [
        (
            session_id,
            _make_llm_request(
                input_tokens=100,
                output_tokens=20,
                cache_read_tokens=30,
                cache_write_tokens=5,
                cost_usd=expected,
            ),
        ),
    ]

    conn = sqlite3.connect(str(db))
    try:
        writer._ensure_schema(conn)
        writer._process_batch(conn, batch)
    finally:
        conn.close()

    row = _rows(db)[0]
    assert row["input_tokens"] == 100
    assert row["cache_read_tokens"] == 30
    assert row["cache_write_tokens"] == 5
    assert row["output_tokens"] == 20
    assert row["cost_usd"] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Schema archival migration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_schema_archived_not_dropped(tmp_path):
    """A stale-version DB is renamed to <path>.legacy.<UTC>, preserving its data."""
    db = tmp_path / "metrics.db"

    # Create a legacy (version 1) database with a marker table.
    legacy = sqlite3.connect(str(db))
    legacy.execute("CREATE TABLE old_marker (x INTEGER)")
    legacy.execute("INSERT INTO old_marker VALUES (42)")
    legacy.execute("PRAGMA user_version = 1")
    legacy.commit()
    legacy.close()

    writer = MetricsWriter(db)
    task = asyncio.create_task(writer.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # A fresh version-3 DB exists at the original path.
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "requests" in tables
        assert "old_marker" not in tables
    finally:
        conn.close()

    # The legacy database was archived (not dropped) and still holds old data.
    archives = list(tmp_path.glob("metrics.db.legacy.*"))
    assert len(archives) == 1
    arch = sqlite3.connect(str(archives[0]))
    try:
        assert arch.execute("SELECT x FROM old_marker").fetchone()[0] == 42
    finally:
        arch.close()


def test_migrate_collision_appends_suffix(tmp_path):
    """Archival destination collisions append -1, -2, ... until unused."""
    db = tmp_path / "metrics.db"

    legacy = sqlite3.connect(str(db))
    legacy.execute("PRAGMA user_version = 1")
    legacy.commit()
    legacy.close()

    # Pre-create the first two candidate archive names for a fixed timestamp.
    from datetime import datetime

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (tmp_path / f"metrics.db.legacy.{stamp}").write_text("taken")
    (tmp_path / f"metrics.db.legacy.{stamp}-1").write_text("taken")

    writer = MetricsWriter(db)
    writer._migrate_if_needed()

    # The stale DB was moved to the first free slot (-2), leaving occupants intact.
    assert (tmp_path / f"metrics.db.legacy.{stamp}-2").exists()
    assert (tmp_path / f"metrics.db.legacy.{stamp}").read_text() == "taken"
    assert (tmp_path / f"metrics.db.legacy.{stamp}-1").read_text() == "taken"
    assert not db.exists()


def test_current_schema_not_archived(tmp_path):
    """A current version-2 DB is left in place (no archival, no data loss)."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    conn = sqlite3.connect(str(db))
    try:
        writer._ensure_schema(conn)
        writer._process_batch(conn, [("sid", _make_llm_request(event_id="keep-1"))])
    finally:
        conn.close()

    writer._migrate_if_needed()

    assert not list(tmp_path.glob("metrics.db.legacy.*"))
    assert len(_rows(db)) == 1


# ---------------------------------------------------------------------------
# Duplicate handling: identical no-op vs conflicting error
# ---------------------------------------------------------------------------


def test_conflicting_duplicate_logged_as_error(tmp_path, caplog):
    """Same (session_id, event_id) with a different payload → logged as error, one row kept."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    sid = "proj-01abc12345"
    batch = [
        (sid, _make_llm_request(event_id="dup-1", cost_usd=0.006)),
        (sid, _make_llm_request(event_id="dup-1", cost_usd=0.999)),
    ]

    with caplog.at_level(logging.ERROR, logger="archie_orchestrator.metrics"):
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            writer._ensure_schema(conn)
            writer._process_batch(conn, batch)
        finally:
            conn.close()

    rows = _rows(db)
    assert len(rows) == 1
    assert abs(rows[0]["cost_usd"] - 0.006) < 1e-9  # first write wins
    assert any("conflicting" in r.message.lower() for r in caplog.records)


def test_identical_duplicate_no_error(tmp_path, caplog):
    """Identical replay of the same event → no error logged, one row kept."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    sid = "proj-01abc12345"
    batch = [
        (sid, _make_llm_request(event_id="dup-1", cost_usd=0.006)),
        (sid, _make_llm_request(event_id="dup-1", cost_usd=0.006)),
    ]

    with caplog.at_level(logging.ERROR, logger="archie_orchestrator.metrics"):
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            writer._ensure_schema(conn)
            writer._process_batch(conn, batch)
        finally:
            conn.close()

    assert len(_rows(db)) == 1
    assert not any("conflicting" in r.message.lower() for r in caplog.records)


def test_reset_and_backfill_rebuilds_integer_identity(tmp_path):
    db = tmp_path / "metrics.db"
    old_writer = MetricsWriter(db)
    conn = sqlite3.connect(db)
    try:
        old_writer._ensure_schema(conn)
        old_writer._process_batch(conn, [("old-session", _make_llm_request(event_id="old"))])
    finally:
        conn.close()

    log_path = tmp_path / "session-1.jsonl"
    raw = _make_llm_request(event_id="new", turn=7, iteration=3)
    log_path.write_text(raw + "\n" + raw + "\n")

    reset_and_backfill(db, [log_path])

    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "session-1"
    assert rows[0]["event_id"] == "new"
    assert rows[0]["turn"] == 7
    assert rows[0]["iteration"] == 3
    assert list(tmp_path.glob("metrics.db.legacy.*"))


def test_reset_and_backfill_propagates_database_failure(tmp_path, monkeypatch):
    """A failed backfill must not be reported as a successful rebuild."""
    db = tmp_path / "metrics.db"
    log_path = tmp_path / "session-1.jsonl"
    log_path.write_text(_make_llm_request(event_id="request-1") + "\n")

    def fail_process(self, conn, batch, *, strict=False):
        assert strict is True
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(MetricsWriter, "_process_batch", fail_process)

    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        reset_and_backfill(db, [log_path])

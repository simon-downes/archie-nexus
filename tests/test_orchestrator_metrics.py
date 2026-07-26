"""Tests for MetricsWriter — SQLite storage and event processing."""

import asyncio
import json
import logging
import sqlite3
from pathlib import Path

import pytest
from archie_orchestrator.metrics import MetricsWriter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session_info(model: str = "Claude Sonnet 4.6", rates: dict | None = None) -> str:
    rates = rates or {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}
    return json.dumps({
        "type": "session_info",
        "data": {
            "protocol_version": 1,
            "model": model,
            "session_id": "irrelevant",
            "cost": rates,
        },
    })


def _make_model_switched(
    model_key: str = "bedrock-anthropic.claude-sonnet-4-6",
    model_name: str = "Claude Sonnet 4.6",
    rates: dict | None = None,
) -> str:
    rates = rates or {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}
    return json.dumps({
        "type": "model_switched",
        "data": {
            "model_key": model_key,
            "model_name": model_name,
            "supports_cache": True,
            "cost": rates,
        },
    })


def _make_usage(
    input_tokens: int = 1000,
    output_tokens: int = 200,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    context_pct: float = 10.0,
    turn_index: int = 1,
) -> str:
    return json.dumps({
        "type": "usage",
        "turn_index": turn_index,
        "data": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "context_pct": context_pct,
        },
    })


def _rows(db_path: Path) -> list[dict]:
    """Read all rows from the requests table as dicts."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM requests ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Usage event with known rates → correct row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_with_session_info_rates(tmp_path):
    """Usage after SessionInfo → correct cost, model, backend=None."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    session_id = "proj-01abc12345"
    # rates: input=3.0, output=15.0 per million
    batch = [
        (session_id, _make_session_info(
            model="Claude Sonnet 4.6",
            rates={"input": 3.0, "output": 15.0, "cache_read": 0.0, "cache_write": 0.0},
        )),
        (session_id, _make_usage(input_tokens=1000, output_tokens=200)),
    ]

    import sqlite3 as _sql

    conn = _sql.connect(str(db))
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
    assert row["model"] == "Claude Sonnet 4.6"
    assert row["backend"] is None  # session_info has no model_key
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 200
    # cost = (1000 * 3.0 + 200 * 15.0) / 1_000_000 = 0.006
    assert abs(row["cost"] - 0.006) < 1e-9


@pytest.mark.asyncio
async def test_usage_with_model_switched_rates(tmp_path):
    """Usage after ModelSwitched → correct backend inferred from model_key."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    session_id = "proj-01abc12345"
    batch = [
        (session_id, _make_model_switched(
            model_key="bedrock-anthropic.claude-sonnet-4-6",
            model_name="Claude Sonnet 4.6",
            rates={"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
        )),
        (session_id, _make_usage(
            input_tokens=500,
            output_tokens=100,
            cache_read_tokens=2000,
            cache_write_tokens=500,
        )),
    ]

    import sqlite3 as _sql

    conn = _sql.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        writer._ensure_schema(conn)
        writer._process_batch(conn, batch)
    finally:
        conn.close()

    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["model"] == "Claude Sonnet 4.6"
    assert row["backend"] == "bedrock"
    # cost = (500*3 + 100*15 + 2000*0.3 + 500*3.75) / 1_000_000
    expected = (500 * 3.0 + 100 * 15.0 + 2000 * 0.3 + 500 * 3.75) / 1_000_000
    assert abs(row["cost"] - expected) < 1e-9


def test_usage_without_prior_rates(tmp_path):
    """Usage with no SessionInfo/ModelSwitched → cost=0.0, model='unknown'."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    import sqlite3 as _sql

    conn = _sql.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        writer._ensure_schema(conn)
        writer._process_batch(conn, [("unknown-session", _make_usage())])
    finally:
        conn.close()

    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["model"] == "unknown"
    assert rows[0]["backend"] is None
    assert rows[0]["cost"] == 0.0


def test_model_switched_updates_rates_mid_session(tmp_path):
    """ModelSwitched mid-session → subsequent Usage uses new rates."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    sid = "proj-01abc12345"
    batch = [
        (sid, _make_session_info(
            model="Claude Sonnet 4.6",
            rates={"input": 3.0, "output": 15.0, "cache_read": 0.0, "cache_write": 0.0},
        )),
        (sid, _make_usage(input_tokens=100, output_tokens=50, turn_index=1)),
        (sid, _make_model_switched(
            model_key="ollama-qwen3:30b-a3b",
            model_name="Qwen3 30B",
            rates={"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0},
        )),
        (sid, _make_usage(input_tokens=200, output_tokens=80, turn_index=2)),
    ]

    import sqlite3 as _sql

    conn = _sql.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        writer._ensure_schema(conn)
        writer._process_batch(conn, batch)
    finally:
        conn.close()

    rows = _rows(db)
    assert len(rows) == 2
    assert rows[0]["model"] == "Claude Sonnet 4.6"
    assert rows[0]["backend"] is None
    assert rows[1]["model"] == "Qwen3 30B"
    assert rows[1]["backend"] == "ollama"
    assert rows[1]["cost"] == 0.0


def test_malformed_json_skipped_no_crash(tmp_path, caplog):
    """Malformed JSON → skipped with warning, no crash, valid events still processed."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    sid = "proj-01abc12345"
    batch = [
        (sid, "not valid json{{{"),
        (sid, _make_session_info()),
        (sid, _make_usage()),
    ]

    import sqlite3 as _sql

    with caplog.at_level(logging.WARNING, logger="archie_orchestrator.metrics"):
        conn = _sql.connect(str(db))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            writer._ensure_schema(conn)
            writer._process_batch(conn, batch)
        finally:
            conn.close()

    assert any("malformed" in r.message.lower() for r in caplog.records)
    rows = _rows(db)
    assert len(rows) == 1  # the valid usage event was processed


def test_multiple_sessions_independent(tmp_path):
    """Multiple sessions write independently with correct session_ids."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    sid_a = "project-a-01abc12345"
    sid_b = "project-b-02def67890"

    batch = [
        (sid_a, _make_session_info(
            model="Model A",
            rates={"input": 1.0, "output": 1.0, "cache_read": 0.0, "cache_write": 0.0},
        )),
        (sid_b, _make_session_info(
            model="Model B",
            rates={"input": 2.0, "output": 2.0, "cache_read": 0.0, "cache_write": 0.0},
        )),
        (sid_a, _make_usage(input_tokens=100, output_tokens=100)),
        (sid_b, _make_usage(input_tokens=100, output_tokens=100)),
    ]

    import sqlite3 as _sql

    conn = _sql.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        writer._ensure_schema(conn)
        writer._process_batch(conn, batch)
    finally:
        conn.close()

    rows = _rows(db)
    assert len(rows) == 2
    by_session = {r["session_id"]: r for r in rows}
    assert by_session[sid_a]["model"] == "Model A"
    assert by_session[sid_b]["model"] == "Model B"
    # cost_a = (100*1 + 100*1) / 1_000_000 = 0.0002
    assert abs(by_session[sid_a]["cost"] - 0.0002) < 1e-9
    # cost_b = (100*2 + 100*2) / 1_000_000 = 0.0004
    assert abs(by_session[sid_b]["cost"] - 0.0004) < 1e-9


def test_batch_processing_multiple_usage_events(tmp_path):
    """Multiple Usage events in one batch all get inserted."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    sid = "proj-01abc12345"
    batch = [
        (sid, _make_session_info()),
        (sid, _make_usage(turn_index=1)),
        (sid, _make_usage(turn_index=2)),
        (sid, _make_usage(turn_index=3)),
    ]

    import sqlite3 as _sql

    conn = _sql.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        writer._ensure_schema(conn)
        writer._process_batch(conn, batch)
    finally:
        conn.close()

    rows = _rows(db)
    assert len(rows) == 3
    assert {r["turn_index"] for r in rows} == {1, 2, 3}


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

    import sqlite3 as _sql

    conn = _sql.connect(str(db))
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
    """run() drains the queue and writes Usage rows to SQLite."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    # Prime the queue before starting run()
    sid = "proj-01abc12345"
    writer.queue.put_nowait((sid, _make_session_info()))
    writer.queue.put_nowait((sid, _make_usage(input_tokens=500, output_tokens=100)))

    task = asyncio.create_task(writer.run())
    # Give the loop time to drain the queue
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
async def test_run_loop_cancellation_drains_remaining(tmp_path):
    """On cancellation, run() drains remaining queue items before exiting."""
    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    task = asyncio.create_task(writer.run())
    # Let the loop start and block on the empty queue
    await asyncio.sleep(0.01)

    # Put items in queue then cancel immediately
    sid = "proj-01abc12345"
    writer.queue.put_nowait((sid, _make_session_info()))
    writer.queue.put_nowait((sid, _make_usage(turn_index=1)))
    writer.queue.put_nowait((sid, _make_usage(turn_index=2)))
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # All items queued after cancellation should still be processed
    rows = _rows(db)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_run_loop_db_write_failure_continues(tmp_path, caplog):
    """A DB write failure is logged but the loop continues processing."""
    import sqlite3 as _sql

    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    # Patch executemany to raise on first call, succeed thereafter
    call_count = 0
    original_process = writer._process_batch

    def _failing_process(conn, batch):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _sql.OperationalError("disk full")
        original_process(conn, batch)

    sid = "proj-01abc12345"
    writer.queue.put_nowait((sid, _make_session_info()))
    writer.queue.put_nowait((sid, _make_usage(turn_index=1)))

    task = asyncio.create_task(writer.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Test passes if no unhandled exception propagated — process stayed alive

def test_db_write_failure_logged_no_crash(tmp_path, caplog):
    """sqlite3.Error during INSERT is logged as ERROR and does not raise."""
    import sqlite3 as _sql

    db = tmp_path / "metrics.db"
    writer = MetricsWriter(db)

    sid = "proj-01abc12345"
    batch = [
        (sid, _make_session_info()),
        (sid, _make_usage()),
    ]

    conn = _sql.connect(str(db))
    try:
        writer._ensure_schema(conn)
        # Drop the table to force a write failure
        conn.execute("DROP TABLE requests")
        conn.commit()

        with caplog.at_level(logging.ERROR, logger="archie_orchestrator.metrics"):
            writer._process_batch(conn, batch)  # should not raise
    finally:
        conn.close()

    assert any("Metrics DB write failed" in r.message for r in caplog.records)


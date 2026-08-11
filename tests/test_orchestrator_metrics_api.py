"""Tests for GET /metrics and GET /sessions/{id}/metrics endpoints."""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from archie_orchestrator.app import _query_metrics, app
from archie_shared.schemas import NexusConfig
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _app_state():
    app.state.config = NexusConfig()
    yield


_SEED_TABLE = """CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, event_id TEXT NOT NULL,
    timestamp TEXT NOT NULL, turn_iteration TEXT NOT NULL, scope TEXT, model_key TEXT NOT NULL,
    status TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL, cache_write_tokens INTEGER NOT NULL,
    context_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL, duration_ms INTEGER NOT NULL,
    UNIQUE (session_id, event_id))"""

_next_event_id = 0


def _seed_db(db_path: Path, rows: list[dict]) -> None:
    """Insert schema-v2 canonical rows into the requests table for testing."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_SEED_TABLE)
    conn.execute(f"PRAGMA user_version = 2")
    conn.executemany(
        "INSERT INTO requests (session_id, event_id, timestamp, turn_iteration, scope, "
        "model_key, status, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_tokens, context_tokens, cost_usd, duration_ms) "
        "VALUES (:session_id, :event_id, :timestamp, :turn_iteration, :scope, "
        ":model_key, :status, :input_tokens, :output_tokens, :cache_read_tokens, "
        ":cache_write_tokens, :context_tokens, :cost_usd, :duration_ms)",
        rows,
    )
    conn.commit()
    conn.close()


def _row(
    session_id: str = "proj-01abc12345",
    timestamp: str = "2026-07-01T10:00:00+00:00",
    turn_iteration: str = "1.1",
    scope: str | None = None,
    model: str = "Claude Sonnet 4.6",
    status: str = "completed",
    input_tokens: int = 1000,
    output_tokens: int = 200,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    context_tokens: int = 12000,
    cost: float = 0.006,
    duration_ms: int = 1234,
) -> dict:
    global _next_event_id
    _next_event_id += 1
    return {
        "session_id": session_id,
        "event_id": f"evt-{_next_event_id:06d}",
        "timestamp": timestamp,
        "turn_iteration": turn_iteration,
        "scope": scope,
        "model_key": model,
        "status": status,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "context_tokens": context_tokens,
        "cost_usd": cost,
        "duration_ms": duration_ms,
    }


# ---------------------------------------------------------------------------
# _query_metrics unit tests (synchronous)
# ---------------------------------------------------------------------------


def test_empty_db_returns_zeroes(tmp_path):
    """Missing database file → all-zero response."""
    result = _query_metrics(tmp_path / "nonexistent.db")
    assert result["total_cost"] == 0.0
    assert result["total_requests"] == 0
    assert result["by_model"] == {}


def test_populated_db_correct_totals(tmp_path):
    """Populated database → correct totals."""
    db = tmp_path / "metrics.db"
    _seed_db(db, [
        _row(input_tokens=1000, output_tokens=200, cost=0.006),
        _row(input_tokens=500, output_tokens=100, cost=0.003),
    ])

    result = _query_metrics(db)
    assert result["total_requests"] == 2
    assert abs(result["total_cost"] - 0.009) < 1e-9
    assert result["input_tokens"] == 1500
    assert result["output_tokens"] == 300


def test_by_model_breakdown(tmp_path):
    """Multiple models produce correct by_model dict."""
    db = tmp_path / "metrics.db"
    _seed_db(db, [
        _row(model="Claude Sonnet 4.6", cost=0.006),
        _row(model="Claude Sonnet 4.6", cost=0.003),
        _row(model="Qwen3 30B", cost=0.001),
    ])

    result = _query_metrics(db)
    assert len(result["by_model"]) == 2
    assert result["by_model"]["Claude Sonnet 4.6"]["requests"] == 2
    assert abs(result["by_model"]["Claude Sonnet 4.6"]["cost"] - 0.009) < 1e-9
    assert result["by_model"]["Qwen3 30B"]["requests"] == 1


def test_since_filter(tmp_path):
    """?since= filters to rows on or after the date."""
    db = tmp_path / "metrics.db"
    _seed_db(db, [
        _row(timestamp="2026-06-01T10:00:00+00:00", cost=0.001),
        _row(timestamp="2026-07-01T10:00:00+00:00", cost=0.002),
        _row(timestamp="2026-07-15T10:00:00+00:00", cost=0.003),
    ])

    result = _query_metrics(db, since="2026-07-01")
    assert result["total_requests"] == 2
    assert abs(result["total_cost"] - 0.005) < 1e-9


def test_per_session_scoped(tmp_path):
    """session_id filter returns only that session's rows."""
    db = tmp_path / "metrics.db"
    _seed_db(db, [
        _row(session_id="session-a", cost=0.010),
        _row(session_id="session-b", cost=0.020),
        _row(session_id="session-a", cost=0.005),
    ])

    result = _query_metrics(db, session_id="session-a")
    assert result["total_requests"] == 2
    assert abs(result["total_cost"] - 0.015) < 1e-9


def test_unknown_session_raises_key_error(tmp_path):
    """Querying a session with no rows raises KeyError."""
    db = tmp_path / "metrics.db"
    _seed_db(db, [_row(session_id="session-a")])

    with pytest.raises(KeyError):
        _query_metrics(db, session_id="nonexistent-session")


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_metrics_empty_db(tmp_path):
    """GET /metrics with missing database → 200 with zero response."""
    with patch("archie_orchestrator.app.home_dir", return_value=tmp_path):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/metrics")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_cost"] == 0.0
    assert body["total_requests"] == 0
    assert body["by_model"] == {}


@pytest.mark.asyncio
async def test_get_metrics_populated(tmp_path):
    """GET /metrics with data → correct aggregates."""
    db = tmp_path / "metrics.db"
    _seed_db(db, [
        _row(cost=0.010, model="Model A"),
        _row(cost=0.005, model="Model B"),
    ])

    with patch("archie_orchestrator.app.home_dir", return_value=tmp_path):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/metrics")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_requests"] == 2
    assert abs(body["total_cost"] - 0.015) < 1e-9
    assert "Model A" in body["by_model"]
    assert "Model B" in body["by_model"]


@pytest.mark.asyncio
async def test_get_metrics_since_param(tmp_path):
    """GET /metrics?since= filters correctly."""
    db = tmp_path / "metrics.db"
    _seed_db(db, [
        _row(timestamp="2026-06-01T10:00:00+00:00", cost=0.001),
        _row(timestamp="2026-07-01T10:00:00+00:00", cost=0.002),
    ])

    with patch("archie_orchestrator.app.home_dir", return_value=tmp_path):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/metrics", params={"since": "2026-07-01"})

    assert resp.status_code == 200
    assert resp.json()["total_requests"] == 1


@pytest.mark.asyncio
async def test_get_session_metrics(tmp_path):
    """GET /sessions/{id}/metrics → correct scoped response."""
    db = tmp_path / "metrics.db"
    sid = "proj-01abc12345"
    _seed_db(db, [
        _row(session_id=sid, cost=0.010),
        _row(session_id="other-session", cost=0.999),
    ])

    with patch("archie_orchestrator.app.home_dir", return_value=tmp_path):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/sessions/{sid}/metrics")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_requests"] == 1
    assert abs(body["total_cost"] - 0.010) < 1e-9


@pytest.mark.asyncio
async def test_get_session_metrics_not_found(tmp_path):
    """GET /sessions/{id}/metrics with no data → 404."""
    db = tmp_path / "metrics.db"
    _seed_db(db, [_row(session_id="other-session")])

    with patch("archie_orchestrator.app.home_dir", return_value=tmp_path):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/sessions/nonexistent/metrics")

    assert resp.status_code == 404
    assert "No metrics found" in resp.json()["error"]

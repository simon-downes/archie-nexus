"""Tests for MetricsIndex: tailing session logs into the v4 metrics index."""

from __future__ import annotations

import logging
import sqlite3

import pytest
from archie_orchestrator import metrics
from archie_orchestrator.metrics import SCHEMA_VERSION, MetricsIndex, reset_and_backfill
from archie_shared.events import encode_event
from archie_shared.session.log import session_accounting

SID = "proj-01k0000000"
MARKER = "zebra-marker-7"


def _index(home) -> MetricsIndex:
    return MetricsIndex(home / "metrics.db", home / "sessions")


def _rows(home, sql, params=()):
    conn = sqlite3.connect(home / "metrics.db")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def _count(home, table, session_id=None):
    sql = f"SELECT COUNT(*) AS n FROM {table}"
    params = ()
    if session_id:
        sql += " WHERE session_id = ?"
        params = (session_id,)
    return _rows(home, sql, params)[0]["n"]


def _full_session(w):
    """A root turn with a task launching two children, a skill, an error and a compaction."""
    return [
        w.session_started(),
        w.user(f"Plan the work\nsecond line {MARKER}", turn=1),
        w.llm(
            event_id="r1",
            turn=1,
            iteration=1,
            cost_usd=0.10,
            sent_at="2026-07-01T10:00:00+00:00",
            duration_ms=2000,
            context_tokens=1000,
            input_tokens=5,
            cache_read_tokens=900,
            cache_write_tokens=95,
        ),
        w.tool_call(
            "task",
            {
                "tasks": [
                    {"agent": "default", "prompt": MARKER},
                    {"agent": "research", "prompt": "x"},
                ]
            },
            tool_use_id="tu-task",
            request_id="r1",
        ),
        w.tool_call("skill", {"name": "build-it"}, tool_use_id="tu-skill", request_id="r1"),
        w.tool_call(
            "shell", {"command": f"echo {MARKER}"}, tool_use_id="tu-shell", request_id="r1"
        ),
        w.llm(
            event_id="c0",
            scope="tu-task",
            index=0,
            cost_usd=0.02,
            sent_at="2026-07-01T10:00:03+00:00",
        ),
        w.turn_complete(scope="tu-task", index=0),
        w.llm(
            event_id="c1",
            scope="tu-task",
            index=1,
            cost_usd=0.03,
            status="error",
            error="Throttled",
            sent_at="2026-07-01T10:00:04+00:00",
        ),
        w.turn_error("child failed", scope="tu-task", index=1),
        w.tool_result(f"done {MARKER}", tool_use_id="tu-task", duration_ms=5000),
        w.tool_result("skill body " + MARKER, tool_use_id="tu-skill", duration_ms=0),
        w.tool_result(
            f"[exit: 1]\ncommand failed {MARKER}\nmore",
            tool_use_id="tu-shell",
            is_error=True,
            duration_ms=40,
        ),
        w.assistant(f"All done {MARKER}", turn=1),
        w.turn_complete(turn=1),
        *w.compaction(turn=2),
        w.user(f"again {MARKER}", turn=2),
        w.llm(
            event_id="r2",
            turn=2,
            cost_usd=0.05,
            sent_at="2026-07-02T08:00:00+00:00",
            context_tokens=1500,
        ),
        w.turn_error("provider down", turn=2),
    ]


def test_full_session_indexes_every_table(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, _full_session(w))
    stats = _index(tmp_path).sync()

    assert stats.files_changed == 1 and stats.errors == 0
    session = _rows(tmp_path, "SELECT * FROM sessions")[0]
    assert session["workspace"] == "proj"
    assert session["title"] == "Plan the work"
    assert session["initial_model"] == "m1"
    assert session["started_at"] == "2026-07-01T09:59:00+00:00"
    assert session["last_activity_at"].startswith("2026-07-02T08:00:00.1")

    assert _count(tmp_path, "requests") == 4
    req = _rows(tmp_path, "SELECT * FROM requests WHERE event_id = 'c1'")[0]
    assert (req["status"], req["error"], req["subagent_index"], req["purpose"]) == (
        "error",
        "Throttled",
        1,
        "conversation",
    )

    turns = _rows(tmp_path, "SELECT turn, status, error, started_at FROM turns ORDER BY turn")
    assert turns == [
        {
            "turn": 1,
            "status": "completed",
            "error": None,
            "started_at": "2026-07-01T10:00:00+00:00",
        },
        {
            "turn": 2,
            "status": "error",
            "error": "provider down",
            "started_at": "2026-07-02T08:00:00+00:00",
        },
    ]

    tools = {r["name"]: r for r in _rows(tmp_path, "SELECT * FROM tool_calls")}
    assert tools["skill"]["skill"] == "build-it"
    assert tools["skill"]["duration_ms"] is None  # 0 means not recorded (D27)
    assert tools["shell"]["is_error"] == 1
    assert tools["shell"]["error_summary"] == f"[exit: 1] command failed {MARKER}"
    assert tools["shell"]["timestamp"] == "2026-07-01T10:00:00+00:00"
    assert tools["shell"]["model_key"] == "m1"
    assert tools["task"]["duration_ms"] == 5000

    subagents = _rows(
        tmp_path,
        "SELECT agent, subagent_index, status, error, parent_scope "
        "FROM subagents ORDER BY subagent_index",
    )
    assert subagents == [
        {
            "agent": "default",
            "subagent_index": 0,
            "status": "completed",
            "error": None,
            "parent_scope": None,
        },
        {
            "agent": "research",
            "subagent_index": 1,
            "status": "error",
            "error": "child failed",
            "parent_scope": None,
        },
    ]

    compaction = _rows(tmp_path, "SELECT * FROM compactions")[0]
    assert (
        compaction["status"],
        compaction["tokens_before"],
        compaction["tokens_after"],
        compaction["timestamp"],
    ) == ("success", 1000, 100, "2026-07-01T10:05:00+00:00")

    total = _rows(tmp_path, "SELECT SUM(cost_usd) AS c FROM requests")[0]["c"]
    assert round(total, 6) == session_accounting(path)["total_cost"]


def test_index_holds_no_content_beyond_title_and_error_summary(session_logs, tmp_path):
    w = session_logs
    w.write(SID, _full_session(w))
    _index(tmp_path).sync()
    conn = sqlite3.connect(tmp_path / "metrics.db")
    try:
        hits = []
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            for col in cols:
                for (value,) in conn.execute(f"SELECT {col} FROM {table}"):
                    if isinstance(value, str) and MARKER in value:
                        hits.append((table, col, len(value)))
    finally:
        conn.close()
    assert [(t, c) for t, c, _ in hits] == [("tool_calls", "error_summary")]
    assert all(n <= metrics.ERROR_SUMMARY_CHARS for _, _, n in hits)


def test_sync_is_incremental_and_idempotent(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, [w.user("hi"), w.llm(cost_usd=0.01)])
    index = _index(tmp_path)
    index.sync()
    second = index.sync()
    assert second.files_changed == 0
    assert _count(tmp_path, "requests") == 1

    with path.open("a") as out:
        out.write(encode_event(w.llm(cost_usd=0.02)) + "\n")
    third = index.sync()
    assert third.events == 1
    assert _count(tmp_path, "requests") == 2


def test_partial_last_line_is_deferred(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, [w.llm(cost_usd=0.01)])
    line = encode_event(w.llm(cost_usd=0.02))
    with path.open("a") as out:
        out.write(line[:20])
    index = _index(tmp_path)
    index.sync()
    assert _count(tmp_path, "requests") == 1
    with path.open("a") as out:
        out.write(line[20:] + "\n")
    index.sync()
    assert _count(tmp_path, "requests") == 2


def test_rewritten_log_is_reingested(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, [w.user("first"), w.llm(event_id="old", cost_usd=1.0)])
    index = _index(tmp_path)
    index.sync()
    path.unlink()
    w.write(SID, [w.user("replacement"), w.llm(event_id="new", cost_usd=2.0)])
    stats = index.sync()
    assert stats.reset == 1
    assert [r["event_id"] for r in _rows(tmp_path, "SELECT event_id FROM requests")] == ["new"]
    assert _rows(tmp_path, "SELECT title FROM sessions")[0]["title"] == "replacement"


def test_shorter_log_is_reingested(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, [w.user("first"), w.llm(event_id="a"), w.llm(event_id="b")])
    index = _index(tmp_path)
    index.sync()
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(lines[:2]))
    index.sync()
    assert [r["event_id"] for r in _rows(tmp_path, "SELECT event_id FROM requests")] == ["a"]


def test_invalid_lines_are_counted_and_skipped(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, [w.llm(cost_usd=0.01)])
    with path.open("a") as out:
        out.write("{not json\n")
        out.write(encode_event(w.llm(cost_usd=0.02)) + "\n")
    stats = _index(tmp_path).sync()
    assert stats.invalid_lines == 1
    assert _count(tmp_path, "requests") == 2
    assert _rows(tmp_path, "SELECT invalid_lines FROM ingest_files")[0]["invalid_lines"] == 1


def test_unreadable_file_is_recorded_and_sync_continues(session_logs, tmp_path):
    w = session_logs
    w.write("good-01k0000000", [w.llm(cost_usd=0.01)])
    (w.dir / "bad-01k0000000.jsonl").mkdir()  # opening it raises IsADirectoryError
    stats = _index(tmp_path).sync()
    assert stats.errors == 1
    assert _count(tmp_path, "requests") == 1
    row = _rows(tmp_path, "SELECT error FROM ingest_files WHERE session_id = 'bad-01k0000000'")
    assert row and row[0]["error"]


def test_conflicting_duplicate_request_logs_error(session_logs, tmp_path, caplog):
    w = session_logs
    path = w.write(SID, [w.llm(event_id="dup", cost_usd=0.01)])
    with path.open("a") as out:
        out.write(encode_event(w.llm(event_id="dup", cost_usd=0.99)) + "\n")
    with caplog.at_level(logging.ERROR, logger="archie_orchestrator.metrics"):
        _index(tmp_path).sync()
    assert _rows(tmp_path, "SELECT cost_usd FROM requests")[0]["cost_usd"] == 0.01
    assert "conflicting duplicate" in caplog.text


def test_task_with_json_string_tasks_and_missing_tasks(session_logs, tmp_path):
    w = session_logs
    w.write(
        SID,
        [
            w.llm(event_id="r1"),
            w.tool_call(
                "task",
                {"tasks": '[{"agent": "research", "prompt": "p"}, {"prompt": "q"}]'},
                tool_use_id="tu-a",
                request_id="r1",
            ),
            w.tool_call("task", {"source": "x"}, tool_use_id="tu-b", request_id="r1"),
        ],
    )
    _index(tmp_path).sync()
    agents = [
        r["agent"]
        for r in _rows(tmp_path, "SELECT agent FROM subagents ORDER BY scope, subagent_index")
    ]
    assert agents == ["research", "unknown"]


def test_stale_schema_is_archived_and_rebuilt(session_logs, tmp_path):
    w = session_logs
    w.write(SID, [w.llm(cost_usd=0.01)])
    db = tmp_path / "metrics.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE requests (id INTEGER)")
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()

    _index(tmp_path).sync()

    legacy = list(tmp_path.glob("metrics.db.legacy.*"))
    assert len([p for p in legacy if not p.name.endswith(("-wal", "-shm"))]) == 1
    assert metrics.schema_version(db) == SCHEMA_VERSION
    assert _count(tmp_path, "requests") == 1


def test_current_schema_not_archived(session_logs, tmp_path):
    w = session_logs
    w.write(SID, [w.llm()])
    index = _index(tmp_path)
    index.sync()
    index.sync()
    assert not list(tmp_path.glob("metrics.db.legacy.*"))


def test_archive_checkpoints_wal_and_leaves_no_side_files(tmp_path):
    db = tmp_path / "metrics.db"
    holder = sqlite3.connect(db)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("CREATE TABLE t (v INTEGER)")
    holder.execute("INSERT INTO t VALUES (42)")
    holder.commit()
    assert (tmp_path / "metrics.db-wal").exists()

    dest = metrics._archive_database(db)
    holder.close()

    assert not db.exists()
    assert not (tmp_path / "metrics.db-wal").exists()
    assert not (tmp_path / "metrics.db-shm").exists()
    check = sqlite3.connect(dest)
    try:
        assert check.execute("SELECT v FROM t").fetchone() == (42,)
    finally:
        check.close()


def test_invalid_utf8_line_is_counted_as_invalid(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, [w.llm()])
    with path.open("ab") as out:
        out.write(b"\xff\xfe\n")
    stats = _index(tmp_path).sync()
    assert stats.invalid_lines == 1 and stats.errors == 0


def test_archive_collision_appends_suffix(tmp_path, monkeypatch):
    db = tmp_path / "metrics.db"
    db.write_bytes(b"")
    first = metrics._archive_database(db)
    db.write_bytes(b"")
    second = metrics._archive_database(db)
    assert first != second and second.name.startswith(first.name)


def test_sync_without_wait_returns_none_when_locked(session_logs, tmp_path):
    index = _index(tmp_path)
    with index._locked(wait=True):
        assert index.sync(wait=False) is None
        assert index.try_lock() is False
    assert index.try_lock() is True


def test_sqlite_error_abandons_sync_without_raising(session_logs, tmp_path, monkeypatch):
    w = session_logs
    w.write(SID, [w.llm()])

    def boom(self, conn, path, stats):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(MetricsIndex, "_sync_file", boom)
    stats = _index(tmp_path).sync()
    assert stats.errors == 1


def test_reset_and_backfill_archives_and_rebuilds(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, [w.llm(event_id="a", cost_usd=0.5)])
    _index(tmp_path).sync()

    stats = reset_and_backfill(tmp_path / "metrics.db", [path])

    assert stats.files_changed == 1
    assert list(tmp_path.glob("metrics.db.legacy.*"))
    assert [r["event_id"] for r in _rows(tmp_path, "SELECT event_id FROM requests")] == ["a"]


def test_reset_and_backfill_propagates_failure(session_logs, tmp_path, monkeypatch):
    w = session_logs
    path = w.write(SID, [w.llm()])

    def boom(self, conn, path, stats):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(MetricsIndex, "_sync_file", boom)
    with pytest.raises(sqlite3.OperationalError):
        reset_and_backfill(tmp_path / "metrics.db", [path])


def test_reset_and_backfill_counts_invalid_lines_without_raising(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, [w.llm()])
    with path.open("a") as out:
        out.write("[]\n")
    stats = reset_and_backfill(tmp_path / "metrics.db", [path])
    assert stats.invalid_lines == 1


def test_deleted_log_keeps_rows(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, [w.llm(cost_usd=0.5)])
    index = _index(tmp_path)
    index.sync()
    path.unlink()
    index.sync()
    assert _count(tmp_path, "requests") == 1


def test_request_without_user_message_creates_turn(session_logs, tmp_path):
    w = session_logs
    w.write(SID, [w.llm(turn=3)])
    _index(tmp_path).sync()
    assert _rows(tmp_path, "SELECT turn, status FROM turns") == [{"turn": 3, "status": "open"}]


def test_session_without_session_started_uses_ulid_start(session_logs, tmp_path):
    w = session_logs
    w.write("proj-01m5x8nn9a", [w.llm()])
    _index(tmp_path).sync()
    started = _rows(tmp_path, "SELECT started_at FROM sessions")[0]["started_at"]
    assert started.startswith("2026-10-26")


@pytest.mark.asyncio
async def test_background_loop_indexes_appended_requests(session_logs, tmp_path, monkeypatch):
    """A request appended with no client attached is indexed by the orchestrator loop (R8)."""
    import asyncio

    from archie_orchestrator import app as app_module

    w = session_logs
    path = w.write(SID, [w.llm(cost_usd=0.01)])
    monkeypatch.setattr(app_module, "METRICS_SYNC_INTERVAL", 0.01)
    task = asyncio.create_task(app_module._metrics_sync_loop(_index(tmp_path)))
    try:
        for _ in range(200):
            await asyncio.sleep(0.01)
            if (tmp_path / "metrics.db").exists() and _count(tmp_path, "requests") == 1:
                break
        with path.open("a") as out:
            out.write(encode_event(w.llm(cost_usd=0.02)) + "\n")
        for _ in range(200):
            await asyncio.sleep(0.01)
            if _count(tmp_path, "requests") == 2:
                break
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert _count(tmp_path, "requests") == 2

"""Tests for MetricsIndex: tailing session logs into the ``metrics_*`` tables of archie.db."""

from __future__ import annotations

import logging
import sqlite3

import pytest
from archie_orchestrator import metrics
from archie_orchestrator.metrics import METRICS_VERSION, MetricsIndex, reset_and_backfill
from archie_shared.events import encode_event
from archie_shared.session.log import session_accounting

from tests.session_fakes import migrated_store, record, sessions_rows

SID = "proj-01k0000000"
MARKER = "zebra-marker-7"


def _index(home) -> MetricsIndex:
    return MetricsIndex(home / "archie.db", home / "sessions")


def _rows(home, sql, params=()):
    conn = sqlite3.connect(home / "archie.db")
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

    assert stats.files_changed == 1 and stats.errors == 0 and stats.skipped == 0
    # The session row is the one-off import's (082 D7); the fold doesn't write it.
    session = _rows(tmp_path, "SELECT * FROM sessions")[0]
    assert (session["workspace"], session["name"]) == ("proj", f"id:{SID}")
    assert session["started_at"] == "2026-07-01T09:59:00+00:00"

    assert _count(tmp_path, "metrics_requests") == 4
    req = _rows(tmp_path, "SELECT * FROM metrics_requests WHERE event_id = 'c1'")[0]
    assert (req["status"], req["error"], req["subagent_index"], req["purpose"]) == (
        "error",
        "Throttled",
        1,
        "conversation",
    )

    turns = _rows(
        tmp_path, "SELECT turn, status, error, started_at FROM metrics_turns ORDER BY turn"
    )
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

    tools = {r["name"]: r for r in _rows(tmp_path, "SELECT * FROM metrics_tool_calls")}
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
        "FROM metrics_subagents ORDER BY subagent_index",
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

    compaction = _rows(tmp_path, "SELECT * FROM metrics_compactions")[0]
    assert (
        compaction["status"],
        compaction["tokens_before"],
        compaction["tokens_after"],
        compaction["timestamp"],
    ) == ("success", 1000, 100, "2026-07-01T10:05:00+00:00")

    total = _rows(tmp_path, "SELECT SUM(cost_usd) AS c FROM metrics_requests")[0]["c"]
    assert round(total, 6) == session_accounting(path)["total_cost"]


def test_index_holds_no_content_beyond_error_summary(session_logs, tmp_path):
    w = session_logs
    w.write(SID, _full_session(w))
    _index(tmp_path).sync()
    conn = sqlite3.connect(tmp_path / "archie.db")
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
    assert [(t, c) for t, c, _ in hits] == [("metrics_tool_calls", "error_summary")]
    assert all(n <= metrics.ERROR_SUMMARY_CHARS for _, _, n in hits)


def test_sync_is_incremental_and_idempotent(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, [w.user("hi"), w.llm(cost_usd=0.01)])
    index = _index(tmp_path)
    index.sync()
    second = index.sync()
    assert second.files_changed == 0
    assert _count(tmp_path, "metrics_requests") == 1

    with path.open("a") as out:
        out.write(encode_event(w.llm(cost_usd=0.02)) + "\n")
    third = index.sync()
    assert third.events == 1
    assert _count(tmp_path, "metrics_requests") == 2


def test_partial_last_line_is_deferred(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, [w.llm(cost_usd=0.01)])
    line = encode_event(w.llm(cost_usd=0.02))
    with path.open("a") as out:
        out.write(line[:20])
    index = _index(tmp_path)
    index.sync()
    assert _count(tmp_path, "metrics_requests") == 1
    with path.open("a") as out:
        out.write(line[20:] + "\n")
    index.sync()
    assert _count(tmp_path, "metrics_requests") == 2


def test_rewritten_log_is_reingested(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, [w.user("first"), w.llm(event_id="old", cost_usd=1.0)])
    index = _index(tmp_path)
    index.sync()
    path.unlink()
    w.write(SID, [w.user("replacement"), w.llm(event_id="new", cost_usd=2.0)])
    stats = index.sync()
    assert stats.reset == 1
    assert [r["event_id"] for r in _rows(tmp_path, "SELECT event_id FROM metrics_requests")] == [
        "new"
    ]


def test_shorter_log_is_reingested(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, [w.user("first"), w.llm(event_id="a"), w.llm(event_id="b")])
    index = _index(tmp_path)
    index.sync()
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(lines[:2]))
    index.sync()
    assert [r["event_id"] for r in _rows(tmp_path, "SELECT event_id FROM metrics_requests")] == [
        "a"
    ]


def test_invalid_lines_are_counted_and_skipped(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, [w.llm(cost_usd=0.01)])
    with path.open("a") as out:
        out.write("{not json\n")
        out.write(encode_event(w.llm(cost_usd=0.02)) + "\n")
    stats = _index(tmp_path).sync()
    assert stats.invalid_lines == 1
    assert _count(tmp_path, "metrics_requests") == 2
    row = _rows(tmp_path, "SELECT invalid_lines FROM metrics_ingest_files")[0]
    assert row["invalid_lines"] == 1


def test_unreadable_file_is_recorded_and_sync_continues(session_logs, tmp_path):
    w = session_logs
    w.write("good-01k0000000", [w.llm(cost_usd=0.01)])
    (w.dir / "bad-01k0000000.jsonl").mkdir()  # opening it raises IsADirectoryError
    # The import can't read it either; give it a record so the sync tries to.
    migrated_store(tmp_path).save(record("bad-01k0000000", status="stopped"))
    stats = _index(tmp_path).sync()
    assert stats.errors == 1
    assert _count(tmp_path, "metrics_requests") == 1
    row = _rows(
        tmp_path, "SELECT error, size FROM metrics_ingest_files WHERE session_id = 'bad-01k0000000'"
    )
    assert row and row[0]["error"] and row[0]["error"] != metrics.NO_SESSION_RECORD
    assert row[0]["size"] == -1


def test_conflicting_duplicate_request_logs_error(session_logs, tmp_path, caplog):
    w = session_logs
    path = w.write(SID, [w.llm(event_id="dup", cost_usd=0.01)])
    with path.open("a") as out:
        out.write(encode_event(w.llm(event_id="dup", cost_usd=0.99)) + "\n")
    with caplog.at_level(logging.ERROR, logger="archie_orchestrator.metrics"):
        _index(tmp_path).sync()
    assert _rows(tmp_path, "SELECT cost_usd FROM metrics_requests")[0]["cost_usd"] == 0.01
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
        for r in _rows(
            tmp_path, "SELECT agent FROM metrics_subagents ORDER BY scope, subagent_index"
        )
    ]
    assert agents == ["research", "unknown"]


def test_metrics_version_bump_rebuilds_metrics_and_keeps_sessions(
    session_logs, tmp_path, monkeypatch
):
    w = session_logs
    w.write(SID, [w.user("hi"), w.llm(event_id="a", cost_usd=0.01)])
    index = _index(tmp_path)
    index.sync()
    conn = sqlite3.connect(tmp_path / "archie.db")
    conn.execute("CREATE TABLE metrics_obsolete (v INTEGER)")
    conn.commit()
    conn.close()
    before = sessions_rows(tmp_path / "archie.db")

    monkeypatch.setattr(metrics, "METRICS_VERSION", METRICS_VERSION + 1)
    assert not metrics.is_current(tmp_path / "archie.db")
    stats = index.sync()

    assert stats.files_changed == 1  # refilled from the log
    assert metrics.schema_version(tmp_path / "archie.db") == METRICS_VERSION + 1
    assert [r["event_id"] for r in _rows(tmp_path, "SELECT event_id FROM metrics_requests")] == [
        "a"
    ]
    tables = {r["name"] for r in _rows(tmp_path, "SELECT name FROM sqlite_master")}
    assert "metrics_obsolete" not in tables
    assert sessions_rows(tmp_path / "archie.db") == before
    assert not list(tmp_path.glob("archie.db.legacy*"))


def test_missing_metrics_version_rebuilds_metrics_tables(session_logs, tmp_path):
    w = session_logs
    w.write(SID, [w.llm(event_id="a")])
    index = _index(tmp_path)
    index.sync()
    conn = sqlite3.connect(tmp_path / "archie.db")
    conn.execute("DELETE FROM meta WHERE key = 'metrics_version'")
    conn.commit()
    conn.close()
    assert metrics.schema_version(tmp_path / "archie.db") is None
    assert index.sync().files_changed == 1
    assert metrics.is_current(tmp_path / "archie.db")
    assert _count(tmp_path, "metrics_requests") == 1


def test_current_metrics_schema_is_not_rebuilt(session_logs, tmp_path):
    w = session_logs
    w.write(SID, [w.llm()])
    index = _index(tmp_path)
    index.sync()
    assert index.sync().files_changed == 0
    assert metrics.schema_version(tmp_path / "archie.db") == METRICS_VERSION


def test_version_lives_in_meta_not_user_version(session_logs, tmp_path):
    """``PRAGMA user_version`` is the canonical schema's (082 D5)."""
    w = session_logs
    w.write(SID, [w.llm()])
    _index(tmp_path).sync()
    assert _rows(tmp_path, "SELECT value FROM meta WHERE key = 'metrics_version'") == [
        {"value": str(METRICS_VERSION)}
    ]
    assert _rows(tmp_path, "PRAGMA user_version")[0]["user_version"] == 3


def test_no_old_metrics_tables(session_logs, tmp_path):
    w = session_logs
    w.write(SID, _full_session(w))
    _index(tmp_path).sync()
    tables = {
        r["name"] for r in _rows(tmp_path, "SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert tables - {"sqlite_sequence"} == {
        "sessions",
        "meta",
        "metrics_ingest_files",
        "metrics_requests",
        "metrics_turns",
        "metrics_tool_calls",
        "metrics_compactions",
        "metrics_subagents",
    }
    columns = {r["name"] for r in _rows(tmp_path, "PRAGMA table_info(sessions)")}
    assert "backend" in columns and "title" not in columns and "initial_model" not in columns


def test_sync_and_reset_never_write_sessions(session_logs, tmp_path):
    """Migrate first; the metrics sync and the rebuild leave every row as it was."""
    w = session_logs
    path = w.write(SID, _full_session(w))
    store = migrated_store(tmp_path)
    store.save(
        record(
            "live-01k0000001",
            status="working",
            name="Named",
            name_source="user",
            unseen=True,
            model_key="m9",
            last_activity_at="2026-07-09T10:00:00+00:00",
        )
    )
    live = w.write("live-01k0000001", [w.user("go"), w.llm(event_id="l1")])
    before = sessions_rows(tmp_path / "archie.db")

    stats = _index(tmp_path).sync()
    assert stats.files_changed == 2
    assert sessions_rows(tmp_path / "archie.db") == before

    stats = reset_and_backfill(tmp_path / "archie.db", [path, live])
    assert stats.files_changed == 2
    assert sessions_rows(tmp_path / "archie.db") == before


def test_log_without_session_record_is_skipped(session_logs, tmp_path):
    """A log written after the import has no row: skipped, never inserted (082 D8)."""
    w = session_logs
    w.write(SID, [w.llm(event_id="a")])
    store = migrated_store(tmp_path)
    late = w.write("late-01k0000001", [w.user("hi"), w.llm(event_id="b")])
    before = sessions_rows(tmp_path / "archie.db")

    stats = _index(tmp_path).sync(strict=True)

    assert (stats.skipped, stats.errors, stats.files_changed) == (1, 0, 1)
    assert sessions_rows(tmp_path / "archie.db") == before
    assert _count(tmp_path, "metrics_requests", "late-01k0000001") == 0
    assert _count(tmp_path, "metrics_turns", "late-01k0000001") == 0
    ingest = _rows(
        tmp_path, "SELECT size, error FROM metrics_ingest_files WHERE session_id = ?", (late.stem,)
    )
    assert ingest == [{"size": -1, "error": "no session record"}]

    # The rebuild skips it too, without raising.
    stats = reset_and_backfill(tmp_path / "archie.db", [late])
    assert (stats.skipped, stats.errors) == (1, 0)
    assert sessions_rows(tmp_path / "archie.db") == before

    # Once something else records the session, the next sync indexes the log.
    store.save(record(late.stem, status="stopped"))
    stats = _index(tmp_path).sync()
    assert stats.skipped == 0
    assert _count(tmp_path, "metrics_requests", late.stem) == 1
    assert _rows(
        tmp_path, "SELECT error FROM metrics_ingest_files WHERE session_id = ?", (late.stem,)
    ) == [{"error": None}]


def test_import_reads_the_database_directory(session_logs, tmp_path):
    """An index over another log directory still imports ``<db dir>/sessions`` (D8)."""
    w = session_logs
    w.write(SID, [w.llm(event_id="a")])
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "x-01k0000002.jsonl").write_bytes((w.dir / f"{SID}.jsonl").read_bytes())
    stats = MetricsIndex(tmp_path / "archie.db", other).sync()
    assert stats.skipped == 1
    assert [r["session_id"] for r in sessions_rows(tmp_path / "archie.db")] == [SID]


def test_old_metrics_db_is_left_alone(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, [w.llm()])
    old = tmp_path / "metrics.db"
    old.write_bytes(b"old index")
    _index(tmp_path).sync()
    reset_and_backfill(tmp_path / "archie.db", [path])
    assert old.read_bytes() == b"old index"
    assert sorted(p.name for p in tmp_path.glob("metrics.db*")) == ["metrics.db"]


def test_invalid_utf8_line_is_counted_as_invalid(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, [w.llm()])
    with path.open("ab") as out:
        out.write(b"\xff\xfe\n")
    stats = _index(tmp_path).sync()
    assert stats.invalid_lines == 1 and stats.errors == 0


def test_sync_without_wait_returns_none_when_locked(session_logs, tmp_path):
    index = _index(tmp_path)
    migrated_store(tmp_path)  # the orchestrator migrates before any sync (D13)
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


def test_reset_and_backfill_rebuilds_metrics(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, [w.llm(event_id="a", cost_usd=0.5)])
    _index(tmp_path).sync()

    stats = reset_and_backfill(tmp_path / "archie.db", [path])

    assert stats.files_changed == 1
    assert not list(tmp_path.glob("archie.db.legacy*"))
    assert [r["event_id"] for r in _rows(tmp_path, "SELECT event_id FROM metrics_requests")] == [
        "a"
    ]


def test_reset_and_backfill_on_a_new_home_imports_first(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, [w.llm(event_id="a")])
    stats = reset_and_backfill(tmp_path / "archie.db", [path])
    assert (stats.files_changed, stats.skipped) == (1, 0)
    assert [r["session_id"] for r in sessions_rows(tmp_path / "archie.db")] == [SID]


def test_reset_and_backfill_propagates_failure(session_logs, tmp_path, monkeypatch):
    w = session_logs
    path = w.write(SID, [w.llm()])

    def boom(self, conn, path, stats):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(MetricsIndex, "_sync_file", boom)
    with pytest.raises(sqlite3.OperationalError):
        reset_and_backfill(tmp_path / "archie.db", [path])


def test_reset_and_backfill_counts_invalid_lines_without_raising(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, [w.llm()])
    with path.open("a") as out:
        out.write("[]\n")
    stats = reset_and_backfill(tmp_path / "archie.db", [path])
    assert stats.invalid_lines == 1


def test_deleted_log_keeps_rows(session_logs, tmp_path):
    w = session_logs
    path = w.write(SID, [w.llm(cost_usd=0.5)])
    index = _index(tmp_path)
    index.sync()
    path.unlink()
    index.sync()
    assert _count(tmp_path, "metrics_requests") == 1


def test_request_without_user_message_creates_turn(session_logs, tmp_path):
    w = session_logs
    w.write(SID, [w.llm(turn=3)])
    _index(tmp_path).sync()
    assert _rows(tmp_path, "SELECT turn, status FROM metrics_turns") == [
        {"turn": 3, "status": "open"}
    ]


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
            if (
                metrics.is_current(tmp_path / "archie.db")
                and _count(tmp_path, "metrics_requests") == 1
            ):
                break
        with path.open("a") as out:
            out.write(encode_event(w.llm(cost_usd=0.02)) + "\n")
        for _ in range(200):
            await asyncio.sleep(0.01)
            if _count(tmp_path, "metrics_requests") == 2:
                break
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert _count(tmp_path, "metrics_requests") == 2


def test_integrity_error_skips_only_that_file(session_logs, tmp_path, monkeypatch):
    w = session_logs
    w.write("a-01k0000000", [w.llm(cost_usd=0.01)])
    w.write("b-01k0000000", [w.llm(cost_usd=0.02)])
    real = MetricsIndex._sync_file

    def flaky(self, conn, path, stats):
        if path.stem == "a-01k0000000":
            raise sqlite3.IntegrityError("NOT NULL constraint failed")
        return real(self, conn, path, stats)

    monkeypatch.setattr(MetricsIndex, "_sync_file", flaky)
    stats = _index(tmp_path).sync()
    assert stats.errors == 1
    rows = _rows(tmp_path, "SELECT session_id FROM metrics_requests")
    assert [r["session_id"] for r in rows] == ["b-01k0000000"]
    assert _rows(
        tmp_path, "SELECT error FROM metrics_ingest_files WHERE session_id = 'a-01k0000000'"
    )


def test_reader_connect_does_not_recreate_missing_db(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        metrics.connect(tmp_path / "archie.db")
    assert not (tmp_path / "archie.db").exists()


def test_turn_request_after_tool_calls_backfills_timestamp(session_logs, tmp_path):
    """Kiro writes the turn's llm_request after its tool calls (077 D13)."""
    w = session_logs
    w.write(
        "kiro-01k0000000",
        [
            w.session_started(model_key="kiro/gpt-5.6-luna"),
            w.user("go", turn=1),
            w.tool_call("shell", {"command": "ls"}, tool_use_id="k1", request_id="seg-0"),
            w.tool_result("ok", tool_use_id="k1"),
            w.tool_call(
                "read", {"path": "x"}, tool_use_id="k2", request_id="c-seg", scope="k1", index=0
            ),
            w.llm(
                event_id="turn-req",
                model_key="kiro/gpt-5.6-luna",
                iteration=0,
                sent_at="2026-10-01T10:00:00+00:00",
                credits=0.5,
            ),
            w.turn_complete(turn=1),
        ],
    )
    w.write(
        "native-01k0000000",
        [
            w.user("go", turn=1),
            w.llm(event_id="n1", sent_at="2026-07-01T09:00:00+00:00"),
            w.tool_call("shell", {}, tool_use_id="n-tc", request_id="n1"),
            w.llm(event_id="n2", iteration=2, sent_at="2026-07-01T09:05:00+00:00", model_key="m2"),
        ],
    )
    _index(tmp_path).sync()
    rows = _rows(
        tmp_path,
        "SELECT session_id, tool_use_id, timestamp, model_key FROM metrics_tool_calls ORDER BY 2",
    )
    assert rows == [
        {
            "session_id": "kiro-01k0000000",
            "tool_use_id": "k1",
            "timestamp": "2026-10-01T10:00:00+00:00",
            "model_key": "kiro/gpt-5.6-luna",
        },
        {
            "session_id": "kiro-01k0000000",
            "tool_use_id": "k2",
            "timestamp": "2026-10-01T10:00:00+00:00",
            "model_key": "kiro/gpt-5.6-luna",
        },
        {
            "session_id": "native-01k0000000",
            "tool_use_id": "n-tc",
            "timestamp": "2026-07-01T09:00:00+00:00",
            "model_key": "m1",
        },
    ]

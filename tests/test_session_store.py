"""Tests for archie_orchestrator.store — archie.db, migrations and session rows (082)."""

import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from archie_orchestrator import store as store_module
from archie_orchestrator.store import (
    SessionStore,
    db_path,
    id_start_time,
    import_log,
    migrate,
    schema_version,
)
from archie_shared.events import PendingQuestion, QuestionOption
from archie_shared.workspaces import SessionLocation

from tests.session_fakes import record

SID = "proj-01kx77jkz8"
MTIME = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)


def _touch(path, when: datetime = MTIME):
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))


@pytest.fixture
def store(tmp_path) -> SessionStore:
    s = SessionStore(db_path(tmp_path))
    s.migrate()
    return s


def _full_record(session_id: str = SID, **fields):
    values = {
        "location": SessionLocation(
            repo="app", worktree="app/.git/wt/x", branch="feat/x", commit="a" * 40, missing=True
        ),
        "workspace": "proj",
        "backend": "kiro",
        "name": "My session",
        "name_source": "user",
        "error": "boom",
        "question": PendingQuestion(
            id="tu1", question="Which?", options=[QuestionOption("a", "first")], recommended=0
        ),
        "unseen": True,
        "model_key": "kiro/auto",
        "started_at": "2026-07-01T09:00:00+00:00",
        "last_activity_at": "2026-07-01T09:05:00+00:00",
    }
    values.update(fields)
    return record(session_id, status="blocked", **values)


# ---------------------------------------------------------------------------
# Database and migrations
# ---------------------------------------------------------------------------


def test_db_path_is_archie_db_in_home(tmp_path):
    assert db_path(tmp_path) == tmp_path / "archie.db"


def test_migrate_creates_schema_wal_and_lock(tmp_path):
    db = db_path(tmp_path)
    assert schema_version(db) is None
    assert migrate(db) == 3
    assert schema_version(db) == 3
    assert (tmp_path / "archie.db.lock").exists()
    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        columns = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert {"sessions", "meta"} <= tables
    assert {"idx_sessions_status", "idx_sessions_started_at"} <= indexes
    assert columns == list(store_module.COLUMNS)  # 2 dropped name_fixed, 3 added location
    assert mode == "wal"


def test_migrate_is_idempotent(tmp_path):
    db = db_path(tmp_path)
    migrate(db)
    SessionStore(db).insert(record(SID))
    assert migrate(db) == 3
    assert [r.session_id for r in SessionStore(db).list()] == [SID]


def test_migrate_rejects_a_newer_schema(tmp_path):
    db = db_path(tmp_path)
    migrate(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(RuntimeError, match="schema version 99"):
        migrate(db)


def test_migration_2_prefixes_names_and_drops_name_fixed(tmp_path, monkeypatch):
    db = db_path(tmp_path)
    monkeypatch.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:1])
    assert migrate(db) == 1
    conn = sqlite3.connect(db)
    with conn:
        for session_id, fixed in (("proj-01kx77jkz8", 1), ("proj-01kx77jkz9", 0)):
            conn.execute(
                "INSERT INTO sessions (session_id, workspace, backend, name, name_fixed, "
                "status, started_at) VALUES (?, 'proj', 'native', ?, ?, 'stopped', "
                "'2026-07-01T10:00:00+00:00')",
                (session_id, "Named" if fixed else session_id, fixed),
            )
    conn.close()
    monkeypatch.undo()

    assert migrate(db) == 3

    rows = {r.session_id: r for r in SessionStore(db).list(include_ended=True)}
    assert (rows["proj-01kx77jkz8"].name, rows["proj-01kx77jkz8"].name_source) == (
        "Named",
        "user",
    )
    assert (rows["proj-01kx77jkz9"].name, rows["proj-01kx77jkz9"].name_source) == (
        "proj-01kx77jkz9",
        "id",
    )
    conn = sqlite3.connect(db)
    try:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
        stored = conn.execute("SELECT name FROM sessions ORDER BY session_id").fetchall()
    finally:
        conn.close()
    assert "name_fixed" not in columns and "name_source" not in columns
    assert stored == [("user:Named",), ("id:proj-01kx77jkz9",)]


def test_migration_3_adds_a_null_location_to_existing_rows(tmp_path, monkeypatch):
    db = db_path(tmp_path)
    monkeypatch.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:2])
    assert migrate(db) == 2
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO sessions (session_id, workspace, backend, name, status, started_at) "
            "VALUES (?, 'proj', 'native', ?, 'stopped', '2026-07-01T10:00:00+00:00')",
            (SID, f"id:{SID}"),
        )
    conn.close()
    monkeypatch.undo()

    assert migrate(db) == 3

    [row] = SessionStore(db).list(include_ended=True)
    assert (row.session_id, row.status, row.location) == (SID, "stopped", None)
    conn = sqlite3.connect(db)
    try:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
        stored = conn.execute("SELECT location FROM sessions").fetchall()
    finally:
        conn.close()
    assert columns[-1] == "location"
    assert stored == [(None,)]


def test_migration_1_alone_imports_logs_into_the_version_1_table(session_logs, tmp_path):
    w = session_logs
    w.write("alpha-01kx77jkz8", [w.user("hello")])
    db = db_path(tmp_path)
    with closing(store_module.connect(db, create=True)) as conn:
        store_module._migration_1(conn, tmp_path / "sessions")
        columns = tuple(r[1] for r in conn.execute("PRAGMA table_info(sessions)"))
        rows = conn.execute("SELECT session_id, name_fixed FROM sessions").fetchall()
    assert columns == store_module._V1_COLUMNS
    assert [tuple(r) for r in rows] == [("alpha-01kx77jkz8", 0)]


@pytest.mark.parametrize(
    ("name", "source", "stored"),
    [
        (SID, "id", f"id:{SID}"),
        ("Review PR 26", "user", "user:Review PR 26"),
        ("Workspaces API", "llm", "llm:Workspaces API"),
        ("llm: my notes", "user", "user:llm: my notes"),  # only the first prefix counts
    ],
)
def test_names_are_stored_with_their_source_prefix(store, name, source, stored):
    store.insert(record(SID, name=name, name_source=source))

    with closing(sqlite3.connect(store.db)) as conn:
        assert conn.execute("SELECT name FROM sessions").fetchone() == (stored,)
    got = store.get(SID)
    assert (got.name, got.name_source) == (name, source)


@pytest.mark.parametrize(
    ("stored", "expected"),
    [(SID, (SID, "id")), ("Old name", ("Old name", "user")), ("x:y", ("x:y", "user"))],
)
def test_names_without_a_known_prefix_still_read(store, stored, expected):
    store.insert(record(SID))
    with closing(sqlite3.connect(store.db)) as conn, conn:
        conn.execute("UPDATE sessions SET name = ?", (stored,))
    got = store.get(SID)
    assert (got.name, got.name_source) == expected


def test_update_keeps_the_stored_half_of_a_name(store):
    store.insert(record(SID, name="Mine", name_source="user"))
    renamed = store.update(SID, name="Renamed")
    assert (renamed.name, renamed.name_source) == ("Renamed", "user")
    resourced = store.update(SID, name_source="llm")
    assert (resourced.name, resourced.name_source) == ("Renamed", "llm")
    with pytest.raises(KeyError):
        store.update("proj-01kx77jkz9", name="Nobody")


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    def broken(conn, sessions_dir):
        conn.execute("CREATE TABLE sessions (x TEXT)")
        raise sqlite3.OperationalError("boom")

    monkeypatch.setattr(store_module, "MIGRATIONS", (broken,))
    db = db_path(tmp_path)
    with pytest.raises(sqlite3.OperationalError):
        migrate(db)
    assert schema_version(db) == 0
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='sessions'").fetchall() == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Historical import (D6, D7)
# ---------------------------------------------------------------------------


def test_migration_imports_each_log_once(session_logs, tmp_path):
    w = session_logs
    native = "alpha-01kx77jkz8"
    bare = "beta-01kx77jkz9"
    kiro = "gamma-01kx77jkza"
    paths = [
        w.write(
            native,
            [
                w.session_started(model_key="m-start", sent_at="2026-07-01T09:59:00+00:00"),
                w.user("hello"),
                w.llm(model_key="m-first"),
                w.llm(model_key="m-last", iteration=2),
                w.llm(model_key="m-child", scope="tc-1", index=0),
            ],
        ),
        w.write(bare, [w.user("hi")]),
        w.write(kiro, [w.user("hi"), w.llm(model_key="kiro/claude-sonnet-4.5")]),
    ]
    for path in paths:
        _touch(path)

    assert migrate(db_path(tmp_path)) == 3  # migration 1 imports, then 2 and 3 run on the rows
    store = SessionStore(db_path(tmp_path))
    mtime = MTIME.isoformat()

    rows = {r.session_id: r for r in store.list(include_ended=True)}
    assert set(rows) == {native, bare, kiro}
    assert store.list() == []  # every imported row has ended

    a = rows[native]
    assert (a.workspace, a.backend, a.name, a.name_source) == ("alpha", "native", native, "id")
    assert (a.status, a.error, a.question, a.unseen) == ("stopped", None, None, False)
    assert a.started_at == "2026-07-01T09:59:00+00:00"
    assert a.model_key == "m-last"  # last root request, not the subagent's
    assert a.last_activity_at == a.ended_at == mtime
    assert a.container_name is None

    b = rows[bare]
    assert b.started_at == id_start_time(bare) is not None
    assert b.model_key is None
    assert b.backend == "native"
    assert b.last_activity_at == b.ended_at == mtime

    c = rows[kiro]
    assert c.backend == "kiro"
    assert c.model_key == "kiro/claude-sonnet-4.5"

    # A log written after the migration never gets a row from it.
    w.write("delta-01kx77jkzb", [w.user("late")])
    migrate(db_path(tmp_path))
    assert store.get("delta-01kx77jkzb") is None


def test_import_falls_back_to_mtime_and_session_started_model(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    path = sessions / "noulid.jsonl"
    path.write_text(
        '{"type": "session_started", "id": "s", "schema_version": 1, "model_key": "m0"}\n'
        "not json\n"
        '["not", "an", "object"]\n'
        '{"type": "user_message", "legacy_field": true}\n'
    )
    _touch(path)

    imported = import_log(path)

    assert imported is not None
    assert imported.started_at == MTIME.isoformat()  # no sent_at and no ULID suffix
    assert imported.model_key == "m0"
    assert imported.workspace == "noulid"


def test_import_log_unreadable_returns_none(tmp_path):
    assert import_log(tmp_path / "missing-01kx77jkz8.jsonl") is None


def test_migration_without_sessions_dir(tmp_path):
    migrate(db_path(tmp_path))
    assert SessionStore(db_path(tmp_path)).list(include_ended=True) == []


# ---------------------------------------------------------------------------
# SessionStore
# ---------------------------------------------------------------------------


def test_insert_get_round_trips_every_field(store):
    original = _full_record()
    store.insert(original)
    loaded = store.get(SID)
    assert loaded == original
    assert loaded.container_name == f"archie-{SID}"


@pytest.mark.parametrize(
    "location", [None, SessionLocation(repo=""), SessionLocation(repo="lib", commit="b" * 40)]
)
def test_location_round_trips_through_save_and_update(store, location):
    store.insert(record(SID))
    assert store.get(SID).location is None
    store.save(record(SID, location=location))
    assert store.get(SID).location == location
    moved = SessionLocation(repo="other", branch="main")
    assert store.update(SID, location=moved).location == moved


def test_an_unreadable_stored_location_reads_as_none(store, caplog):
    store.insert(record(SID))
    conn = sqlite3.connect(store.db)
    with conn:
        conn.execute("UPDATE sessions SET location = '{not json'")
    conn.close()
    assert store.get(SID).location is None
    assert "unreadable location" in caplog.text


def test_ended_record_round_trips_without_container(store):
    ended = record(SID, status="failed", error="no image", ended_at="2026-07-01T10:00:00+00:00")
    store.insert(ended)
    loaded = store.get(SID)
    assert loaded == ended
    assert loaded.container_name is None


def test_insert_duplicate_raises(store):
    store.insert(record(SID))
    with pytest.raises(sqlite3.IntegrityError):
        store.insert(record(SID))


def test_save_inserts_then_replaces(store):
    store.save(record(SID, status="starting"))
    store.save(_full_record())
    assert store.get(SID) == _full_record()


def test_get_unknown_returns_none(store):
    assert store.get("nope-01kx77jkz8") is None


def test_list_filters_ended_and_orders_newest_first(store):
    store.insert(record("a-01kx77jkz1", started_at="2026-07-01T01:00:00+00:00"))
    store.insert(record("b-01kx77jkz2", status="working", started_at="2026-07-01T03:00:00+00:00"))
    for status, hour in (("stopped", 2), ("exited", 4), ("failed", 5)):
        store.insert(
            record(
                f"{status}-01kx77jkz{hour}",
                status=status,
                started_at=f"2026-07-01T0{hour}:00:00+00:00",
                ended_at="2026-07-01T06:00:00+00:00",
            )
        )
    assert [r.session_id for r in store.list()] == ["b-01kx77jkz2", "a-01kx77jkz1"]
    assert [r.session_id for r in store.list(include_ended=True)] == [
        "failed-01kx77jkz5",
        "exited-01kx77jkz4",
        "b-01kx77jkz2",
        "stopped-01kx77jkz2",
        "a-01kx77jkz1",
    ]


def test_update_changes_fields_and_returns_record(store):
    store.insert(record(SID))
    question = PendingQuestion(id="q", question="?", options=[])
    updated = store.update(SID, name="Renamed", name_source="user", unseen=True, question=question)
    assert (updated.name, updated.name_source, updated.unseen) == ("Renamed", "user", True)
    assert updated.question == question
    assert store.get(SID) == updated
    assert store.update(SID, question=None).question is None


def test_update_rejects_unknown_fields_and_ids(store):
    store.insert(record(SID))
    with pytest.raises(ValueError, match="container_name"):
        store.update(SID, container_name="x")
    with pytest.raises(KeyError):
        store.update("nope-01kx77jkz8", name="x")


def test_reader_does_not_create_a_missing_database(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        SessionStore(db_path(tmp_path)).get(SID)
    assert not db_path(tmp_path).exists()

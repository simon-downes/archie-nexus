"""Canonical orchestrator datastore: ``<ARCHIE_HOME_DIR>/archie.db``.

The ``sessions`` table holds one record per session (see ``SessionDescriptor``). It is
never rebuilt: once a session ends its row stops changing, so it is the historical
record too. The schema is versioned with ``PRAGMA user_version`` and changed only by the
ordered migrations in :data:`MIGRATIONS`, which run under an exclusive ``flock`` on
``archie.db.lock``. Migration 1 imports every existing session log once as a ``stopped``
row; migration 2 moves a name's source into the name itself and drops ``name_fixed``.

A stored ``name`` is ``<source>:<name>`` (``id:proj-01kx77jkz8``, ``user:Review PR 26``,
``llm:Workspaces API``), so queries show where a name came from. Only this module knows
the prefix: records and the API carry ``name`` and ``name_source`` separately.

Everything here is synchronous ``sqlite3`` with one short-lived connection per call;
async callers use ``asyncio.to_thread``.
"""

from __future__ import annotations

import fcntl
import json
import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

import msgspec
from archie_shared.events import PendingQuestion
from archie_shared.kiro import KIRO_MODEL_PREFIX
from archie_shared.session import SessionDescriptor, split_id

from archie_orchestrator.session_index import session_start

log = logging.getLogger(__name__)

DB_NAME = "archie.db"
LOCK_SUFFIX = ".lock"

# Every column of ``sessions``; ``container_name`` is computed, never stored.
COLUMNS = (
    "session_id",
    "workspace",
    "backend",
    "name",
    "status",
    "error",
    "question",
    "unseen",
    "model_key",
    "started_at",
    "last_activity_at",
    "ended_at",
)
_ENDED_SQL = "('stopped', 'exited', 'failed')"


def db_path(home: Path) -> Path:
    """Return the database path under an Archie home directory."""
    return Path(home) / DB_NAME


def connect(db: Path, *, create: bool = False) -> sqlite3.Connection:
    """Open the database with WAL, a busy timeout and ``sqlite3.Row`` rows.

    Readers pass ``create=False`` (the default), so a missing database raises
    ``sqlite3.OperationalError`` instead of being created empty.
    """
    uri = f"{Path(db).resolve().as_uri()}?mode={'rwc' if create else 'rw'}"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def _locked(db: Path) -> Iterator[None]:
    """Hold the exclusive ``archie.db.lock`` flock for the duration of the block."""
    lock_path = Path(db).with_name(Path(db).name + LOCK_SUFFIX)
    with open(lock_path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat()


def now_iso() -> str:
    """The current time as an ISO 8601 UTC string, the format of every stored time."""
    return _iso(datetime.now(UTC))


# ---------------------------------------------------------------------------
# Historical import
# ---------------------------------------------------------------------------


def id_start_time(session_id: str) -> str | None:
    """The session's start decoded from its ID's ULID suffix, or None."""
    start = session_start(session_id)
    return _iso(start) if start is not None else None


def import_log(path: Path) -> SessionDescriptor | None:
    """Build a ``stopped`` record for one session log (D7), or None if it can't be read.

    Lines are scanned as plain JSON by ``type`` so logs in older formats still import.
    ``started_at`` is the ``session_started`` time, else the ID's ULID time, else the
    log's mtime; ``last_activity_at`` and ``ended_at`` are the log's mtime; ``model_key``
    is the last root request's model, else the ``session_started`` model.
    """
    session_id = path.stem
    try:
        mtime = _iso(datetime.fromtimestamp(path.stat().st_mtime, UTC))
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Cannot read session log %s: %s", path, exc)
        return None
    started_at: str | None = None
    started_model: str | None = None
    request_model: str | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        kind = data.get("type")
        if kind == "session_started":
            if started_at is None and isinstance(data.get("sent_at"), str):
                started_at = data["sent_at"]
            if started_model is None and isinstance(data.get("model_key"), str):
                started_model = data["model_key"]
        elif kind == "llm_request" and data.get("scope") is None:
            if isinstance(data.get("model_key"), str):
                request_model = data["model_key"]
    model_key = request_model or started_model
    workspace, _ = split_id(session_id)
    return SessionDescriptor(
        session_id=session_id,
        workspace=workspace,
        backend="kiro" if (model_key or "").startswith(KIRO_MODEL_PREFIX) else "native",
        name=session_id,
        status="stopped",
        started_at=started_at or id_start_time(session_id) or mtime,
        model_key=model_key,
        last_activity_at=mtime,
        ended_at=mtime,
    )


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


# Migration 1 writes the version 1 table, so it keeps that version's columns and plain
# names, spelled out: ``COLUMNS`` follows the current schema (migration 2 dropped
# ``name_fixed`` and prefixed the names).
_V1_COLUMNS = (
    "session_id",
    "workspace",
    "backend",
    "name",
    "name_fixed",
    "status",
    "error",
    "question",
    "unseen",
    "model_key",
    "started_at",
    "last_activity_at",
    "ended_at",
)
_V1_INSERT_SQL = (
    f"INSERT INTO sessions ({', '.join(_V1_COLUMNS)}) VALUES ({', '.join('?' * len(_V1_COLUMNS))})"
)


def _v1_row_values(record: SessionDescriptor) -> tuple:
    return tuple(
        0 if column == "name_fixed" else _column_value(column, getattr(record, column))
        for column in _V1_COLUMNS
    )


NAME_SOURCES = ("id", "llm", "user")
"""The prefixes of a stored ``name``, one per ``NameSource``."""


def encode_name(source: str, name: str) -> str:
    """The stored form of a name: ``<source>:<name>``."""
    return f"{source}:{name}"


def decode_name(stored: str, session_id: str) -> tuple[str, str]:
    """``(name, source)`` from a stored name.

    A value without a known prefix wasn't written by this version (an orchestrator from
    before migration 2 still running after it): it is the session ID or a user's name.
    """
    source, sep, name = stored.partition(":")
    if sep and source in NAME_SOURCES:
        return name, source
    return stored, "id" if stored == session_id else "user"


def _migration_1(conn: sqlite3.Connection, sessions_dir: Path) -> None:
    """Create ``sessions`` and ``meta``, then import every existing session log."""
    conn.execute(
        """CREATE TABLE sessions (
 session_id TEXT PRIMARY KEY, workspace TEXT NOT NULL, backend TEXT NOT NULL,
 name TEXT NOT NULL, name_fixed INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
 error TEXT, question TEXT, unseen INTEGER NOT NULL DEFAULT 0, model_key TEXT,
 started_at TEXT NOT NULL, last_activity_at TEXT, ended_at TEXT)"""
    )
    conn.execute("CREATE INDEX idx_sessions_status ON sessions(status)")
    conn.execute("CREATE INDEX idx_sessions_started_at ON sessions(started_at)")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    imported = 0
    for path in sorted(sessions_dir.glob("*.jsonl")) if sessions_dir.is_dir() else ():
        record = import_log(path)
        if record is None:
            continue
        conn.execute(_V1_INSERT_SQL, _v1_row_values(record))
        imported += 1
    log.info("Imported %d session logs into %s", imported, DB_NAME)


def _migration_2(conn: sqlite3.Connection, sessions_dir: Path) -> None:
    """Prefix each name with its source (``user:`` when ``name_fixed``, else ``id:``)
    and drop ``name_fixed``.

    An orchestrator from before this change that is still running when ``archie stats``
    migrates the database can't write sessions until it restarts.
    """
    del sessions_dir
    conn.execute(
        "UPDATE sessions SET name = CASE WHEN name_fixed = 1 THEN 'user:' || name "
        "ELSE 'id:' || session_id END"
    )
    conn.execute("ALTER TABLE sessions DROP COLUMN name_fixed")


MIGRATIONS: tuple[Callable[[sqlite3.Connection, Path], None], ...] = (_migration_1, _migration_2)
"""Canonical schema migrations; migration N brings ``user_version`` to N."""


def schema_version(db: Path) -> int | None:
    """Return the database's ``user_version``, or None when there is no database file."""
    if not Path(db).exists():
        return None
    with closing(sqlite3.connect(str(db))) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(db: Path, sessions_dir: Path | None = None) -> int:
    """Create or upgrade the canonical schema, each migration in its own transaction.

    Runs under the ``archie.db.lock`` flock, so concurrent callers (the orchestrator,
    ``archie stats``) migrate once. Callers must not already hold that lock.

    Args:
        db: Database path (``<home>/archie.db``).
        sessions_dir: Logs imported by migration 1; defaults to ``<db dir>/sessions``.

    Returns:
        The schema version after migrating.

    Raises:
        RuntimeError: If the database is newer than this code supports.
        sqlite3.Error: If a migration fails (it is rolled back).
    """
    db = Path(db)
    sessions_dir = sessions_dir if sessions_dir is not None else db.parent / "sessions"
    db.parent.mkdir(parents=True, exist_ok=True)
    with _locked(db), closing(connect(db, create=True)) as conn:
        conn.isolation_level = None  # explicit transactions below
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            raise RuntimeError(
                f"{db} has schema version {version}; this Archie supports up to "
                f"{len(MIGRATIONS)}. Update Archie."
            )
        for target in range(version + 1, len(MIGRATIONS) + 1):
            conn.execute("BEGIN IMMEDIATE")
            try:
                MIGRATIONS[target - 1](conn, sessions_dir)
                conn.execute(f"PRAGMA user_version = {target}")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            log.info("Migrated %s to schema version %d", db, target)
            version = target
        return version


# ---------------------------------------------------------------------------
# Session rows
# ---------------------------------------------------------------------------

_INSERT_SQL = (
    f"INSERT INTO sessions ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})"
)
_SAVE_SQL = (
    _INSERT_SQL
    + " ON CONFLICT(session_id) DO UPDATE SET "
    + ", ".join(f"{column} = excluded.{column}" for column in COLUMNS[1:])
)


def _column_value(column: str, value: object) -> object:
    if column == "unseen":
        return int(bool(value))
    if column == "question" and value is not None:
        return msgspec.json.encode(value).decode("utf-8")
    return value


def _row_values(record: SessionDescriptor) -> tuple:
    return tuple(
        encode_name(record.name_source, record.name)
        if column == "name"
        else _column_value(column, getattr(record, column))
        for column in COLUMNS
    )


def _from_row(row: sqlite3.Row) -> SessionDescriptor:
    question = None
    if row["question"]:
        try:
            question = msgspec.json.decode(row["question"], type=PendingQuestion)
        except (msgspec.DecodeError, msgspec.ValidationError):
            log.warning("Ignoring an unreadable question for session %s", row["session_id"])
    name, name_source = decode_name(row["name"], row["session_id"])
    return SessionDescriptor(
        session_id=row["session_id"],
        workspace=row["workspace"],
        backend=row["backend"],
        name=name,
        status=row["status"],
        started_at=row["started_at"],
        name_source=name_source,
        error=row["error"],
        question=question,
        unseen=bool(row["unseen"]),
        model_key=row["model_key"],
        last_activity_at=row["last_activity_at"],
        ended_at=row["ended_at"],
    )


class SessionStore:
    """Reads and writes ``sessions`` rows; the database must already be migrated.

    Each call opens and closes its own connection, so one store can be used from
    worker threads.
    """

    def __init__(self, db: Path) -> None:
        self.db = Path(db)

    def migrate(self, sessions_dir: Path | None = None) -> int:
        """Run :func:`migrate` on this store's database."""
        return migrate(self.db, sessions_dir)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with closing(connect(self.db)) as conn, conn:
            yield conn

    def insert(self, record: SessionDescriptor) -> None:
        """Insert a new row.

        Raises:
            sqlite3.IntegrityError: If a row with that session ID exists.
        """
        with self._conn() as conn:
            conn.execute(_INSERT_SQL, _row_values(record))

    def save(self, record: SessionDescriptor) -> None:
        """Write a whole record, inserting it if it has no row yet."""
        with self._conn() as conn:
            conn.execute(_SAVE_SQL, _row_values(record))

    def delete(self, session_id: str) -> None:
        """Delete one row (a stopped session that left no log, D19a); a missing row is fine."""
        with self._conn() as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def get(self, session_id: str) -> SessionDescriptor | None:
        """Return one record, or None if there is no row."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return _from_row(row) if row is not None else None

    def list(self, include_ended: bool = False) -> list[SessionDescriptor]:
        """Return records newest first; ended ones only when ``include_ended``."""
        where = "" if include_ended else f"WHERE status NOT IN {_ENDED_SQL} "
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM sessions {where}ORDER BY started_at DESC, session_id DESC"
            ).fetchall()
        return [_from_row(row) for row in rows]

    def last_started_by_workspace(self) -> dict[str, str]:
        """The newest ``started_at`` of each workspace that has a session row."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT workspace, MAX(started_at) FROM sessions GROUP BY workspace"
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def set_model_name(self, session_id: str, name: str) -> SessionDescriptor | None:
        """Set a model-written name unless the user named the session (plan 083 D17).

        Returns:
            The updated record, or None when there is no row or the user's name stands.
        """
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE sessions SET name = ? "
                "WHERE session_id = ? AND substr(name, 1, 5) != 'user:'",
                (encode_name("llm", name), session_id),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return _from_row(row) if row is not None else None

    def update(self, session_id: str, **fields: object) -> SessionDescriptor:
        """Change some fields of one row and return the updated record.

        ``name`` and ``name_source`` share the ``name`` column: giving one keeps the
        stored value of the other.

        Raises:
            KeyError: If there is no row for ``session_id``.
            ValueError: If a field is not a record field stored in ``sessions``.
        """
        unknown = set(fields) - {*COLUMNS[1:], "name_source"}
        if unknown:
            raise ValueError(f"Unknown session fields: {', '.join(sorted(unknown))}")
        with self._conn() as conn:
            if "name" in fields or "name_source" in fields:
                stored = conn.execute(
                    "SELECT name FROM sessions WHERE session_id = ?", (session_id,)
                ).fetchone()
                if stored is None:
                    raise KeyError(session_id)
                name, source = decode_name(stored["name"], session_id)
                source = str(fields.pop("name_source", source))
                fields["name"] = encode_name(source, str(fields.get("name", name)))
            if fields:
                assignments = ", ".join(f"{column} = ?" for column in fields)
                values = [_column_value(column, value) for column, value in fields.items()]
                conn.execute(
                    f"UPDATE sessions SET {assignments} WHERE session_id = ?",
                    (*values, session_id),
                )
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return _from_row(row)

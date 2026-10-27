"""SQLite metrics index built by tailing the canonical session logs.

``<ARCHIE_HOME_DIR>/metrics.db`` is a derived, metadata-only index of every
session log under ``<ARCHIE_HOME_DIR>/sessions``. :class:`MetricsIndex` reads
each log from a stored byte offset, decodes canonical events, and folds them
into the tables described in ``docs/architecture.md`` (sessions, requests,
turns, tool calls, compactions, subagents). The logs remain the source of
truth: the index can always be archived and rebuilt from them.

Every schema check, archive and sync runs under an exclusive ``flock`` on
``metrics.db.lock`` so the orchestrator's background loop, web requests and
``archie stats`` never write concurrently or race on an archive.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import msgspec
from archie_shared.events import (
    CompactionComplete,
    CompactionStart,
    ContextCompacted,
    LLMRequest,
    SessionStarted,
    ToolCall,
    ToolResult,
    TurnComplete,
    TurnError,
    TurnInterrupted,
    UserMessage,
    decode_event,
)
from archie_shared.session import split_id

from archie_orchestrator.session_index import session_start

log = logging.getLogger(__name__)

SCHEMA_VERSION = 4
TITLE_CHARS = 120
ERROR_SUMMARY_CHARS = 200
ERROR_CHARS = 500

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS ingest_files (
 session_id TEXT PRIMARY KEY, offset INTEGER NOT NULL, size INTEGER NOT NULL,
 mtime_ns INTEGER NOT NULL, head_sha TEXT NOT NULL, invalid_lines INTEGER NOT NULL DEFAULT 0,
 error TEXT)""",
    """CREATE TABLE IF NOT EXISTS sessions (
 session_id TEXT PRIMARY KEY, workspace TEXT NOT NULL, started_at TEXT, initial_model TEXT,
 title TEXT, last_activity_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS requests (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, event_id TEXT NOT NULL,
 timestamp TEXT NOT NULL, turn INTEGER NOT NULL, iteration INTEGER NOT NULL, scope TEXT,
 model_key TEXT NOT NULL, status TEXT NOT NULL, input_tokens INTEGER NOT NULL,
 output_tokens INTEGER NOT NULL, cache_read_tokens INTEGER NOT NULL,
 cache_write_tokens INTEGER NOT NULL, context_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL,
 duration_ms INTEGER NOT NULL, stop_reason TEXT, error TEXT, subagent_index INTEGER,
 purpose TEXT NOT NULL, UNIQUE (session_id, event_id))""",
    """CREATE TABLE IF NOT EXISTS turns (
 session_id TEXT NOT NULL, turn INTEGER NOT NULL, status TEXT NOT NULL, stop_reason TEXT,
 error TEXT, started_at TEXT, ended_at TEXT, PRIMARY KEY (session_id, turn))""",
    """CREATE TABLE IF NOT EXISTS tool_calls (
 session_id TEXT NOT NULL, tool_use_id TEXT NOT NULL, event_id TEXT NOT NULL,
 turn INTEGER NOT NULL, iteration INTEGER NOT NULL, scope TEXT, subagent_index INTEGER,
 request_id TEXT NOT NULL, timestamp TEXT, model_key TEXT, name TEXT NOT NULL, skill TEXT,
 is_error INTEGER, duration_ms INTEGER, result_bytes INTEGER, result_lines INTEGER,
 error_summary TEXT, UNIQUE (session_id, tool_use_id))""",
    """CREATE TABLE IF NOT EXISTS compactions (
 session_id TEXT NOT NULL, request_id TEXT NOT NULL, turn INTEGER NOT NULL, scope TEXT,
 reason TEXT NOT NULL, model_key TEXT, timestamp TEXT, status TEXT, tokens_before INTEGER,
 tokens_after INTEGER, cost_saved_usd REAL, duration_ms INTEGER, error TEXT,
 UNIQUE (session_id, request_id))""",
    """CREATE TABLE IF NOT EXISTS subagents (
 session_id TEXT NOT NULL, scope TEXT NOT NULL, subagent_index INTEGER NOT NULL,
 agent TEXT NOT NULL, parent_scope TEXT, turn INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'open', error TEXT,
 PRIMARY KEY (session_id, scope, subagent_index))""",
    "CREATE INDEX IF NOT EXISTS idx_requests_session ON requests(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model_key)",
    "CREATE INDEX IF NOT EXISTS idx_tool_calls_name ON tool_calls(name)",
    "CREATE INDEX IF NOT EXISTS idx_tool_calls_timestamp ON tool_calls(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at)",
)
_SESSION_TABLES = ("requests", "turns", "tool_calls", "compactions", "subagents", "sessions")
_REQUEST_COLUMNS = (
    "timestamp",
    "turn",
    "iteration",
    "scope",
    "model_key",
    "status",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "context_tokens",
    "cost_usd",
    "duration_ms",
    "stop_reason",
    "error",
    "subagent_index",
    "purpose",
)


@dataclass
class SyncStats:
    """What one sync did; ``files_changed`` counts logs that were read."""

    files_seen: int = 0
    files_changed: int = 0
    events: int = 0
    invalid_lines: int = 0
    errors: int = 0
    reset: int = 0


def connect(db_path: Path, *, create: bool = False) -> sqlite3.Connection:
    """Open the index with WAL, a busy timeout and ``sqlite3.Row`` rows.

    Readers pass ``create=False`` (the default), so a database archived since the
    last sync raises ``sqlite3.OperationalError`` instead of being recreated empty.
    """
    uri = f"{Path(db_path).resolve().as_uri()}?mode={'rwc' if create else 'rw'}"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def schema_version(db_path: Path) -> int | None:
    """Return the DB's ``user_version``, or None when there is no database file."""
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def is_current(db_path: Path) -> bool:
    """True when the index exists with the current schema (safe to query)."""
    try:
        return schema_version(db_path) == SCHEMA_VERSION
    except sqlite3.Error:
        return False


def _archive_database(db_path: Path) -> Path | None:
    """Rename the DB (and its WAL/SHM files) to ``<name>.legacy.<UTC>[-N]``."""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("Metrics WAL checkpoint before archive failed: %s", exc)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = db_path.with_name(f"{db_path.name}.legacy.{stamp}")
    destination = base
    suffix = 0
    while destination.exists():
        suffix += 1
        destination = base.with_name(f"{base.name}-{suffix}")
    db_path.rename(destination)
    for extra in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + extra)
        if side.exists():
            side.rename(destination.with_name(destination.name + extra))
    return destination


def _clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _first_line(text: str, limit: int) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return _clip(line, limit)
    return None


_EXIT_LINE = re.compile(r"^\[exit: -?\d+\]$")


def _error_summary(text: str) -> str | None:
    """First line of an error result; a bare ``[exit: N]`` header keeps the next line too."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    if _EXIT_LINE.match(lines[0]) and len(lines) > 1:
        return _clip(f"{lines[0]} {lines[1]}", ERROR_SUMMARY_CHARS)
    return _clip(lines[0], ERROR_SUMMARY_CHARS)


def _end_time(sent_at: str, duration_ms: int) -> str:
    try:
        start = datetime.fromisoformat(sent_at)
    except ValueError:
        return sent_at
    return (start + timedelta(milliseconds=duration_ms)).isoformat()


def _task_agents(tool_input: dict) -> list[str] | None:
    """Agent names from a ``task`` input's ``tasks`` (a list or a JSON-encoded list)."""
    tasks = tool_input.get("tasks")
    if isinstance(tasks, str):
        try:
            tasks = json.loads(tasks)
        except ValueError:
            return None
    if not isinstance(tasks, list):
        return None
    return [
        item["agent"]
        if isinstance(item, dict) and isinstance(item.get("agent"), str)
        else "unknown"
        for item in tasks
    ]


class _Folder:
    """Fold one session's events into the index tables on one connection."""

    def __init__(self, conn: sqlite3.Connection, session_id: str) -> None:
        self.conn = conn
        self.session_id = session_id
        self.request_times: dict[str, tuple[str, str]] = {}
        self._ensure_session()

    def _ensure_session(self) -> None:
        workspace, _ = split_id(self.session_id)
        start = session_start(self.session_id)
        self.conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, workspace, started_at) VALUES (?,?,?)",
            (self.session_id, workspace, start.isoformat() if start else None),
        )

    def _request_info(self, request_id: str) -> tuple[str | None, str | None]:
        """Return ``(sent_at, model_key)`` for a request, from memory or the DB."""
        info = self.request_times.get(request_id)
        if info is not None:
            return info
        row = self.conn.execute(
            "SELECT timestamp, model_key FROM requests WHERE session_id = ? AND event_id = ?",
            (self.session_id, request_id),
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def _touch_turn(self, turn: int, when: str | None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO turns (session_id, turn, status) VALUES (?, ?, 'open')",
            (self.session_id, turn),
        )
        if when is not None:
            self.conn.execute(
                "UPDATE turns SET started_at = CASE WHEN started_at IS NULL OR started_at > ?1 "
                "THEN ?1 ELSE started_at END, ended_at = CASE WHEN ended_at IS NULL OR "
                "ended_at < ?2 THEN ?2 ELSE ended_at END WHERE session_id = ?3 AND turn = ?4",
                (when, when, self.session_id, turn),
            )

    def apply(self, event) -> None:  # noqa: C901 — one branch per event type
        sid = self.session_id
        if isinstance(event, SessionStarted):
            self.conn.execute(
                "UPDATE sessions SET started_at = ?, initial_model = ? WHERE session_id = ?",
                (event.sent_at, event.model_key, sid),
            )
        elif isinstance(event, UserMessage):
            if event.scope is None:
                self._touch_turn(event.turn, None)
                title = _first_line(event.content, TITLE_CHARS)
                if title:
                    self.conn.execute(
                        "UPDATE sessions SET title = ? WHERE session_id = ? AND title IS NULL",
                        (title, sid),
                    )
        elif isinstance(event, LLMRequest):
            self._apply_request(event)
        elif isinstance(event, ToolCall):
            self._apply_tool_call(event)
        elif isinstance(event, ToolResult):
            duration = event.duration_ms or None
            summary = _error_summary(event.content) if event.is_error else None
            self.conn.execute(
                "UPDATE tool_calls SET is_error = ?, duration_ms = ?, result_bytes = ?, "
                "result_lines = ?, error_summary = ? WHERE session_id = ? AND tool_use_id = ?",
                (
                    int(event.is_error),
                    duration,
                    event.result_bytes,
                    event.result_lines,
                    summary,
                    sid,
                    event.tool_use_id,
                ),
            )
        elif isinstance(event, (TurnComplete, TurnError, TurnInterrupted)):
            self._apply_turn_end(event)
        elif isinstance(event, CompactionStart):
            self.conn.execute(
                "INSERT OR IGNORE INTO compactions (session_id, request_id, turn, scope, reason, "
                "model_key, timestamp, tokens_before) VALUES (?,?,?,?,?,?,?,?)",
                (
                    sid,
                    event.request_id,
                    event.turn,
                    event.scope,
                    event.reason,
                    event.model_key,
                    event.sent_at,
                    event.tokens_before,
                ),
            )
        elif isinstance(event, CompactionComplete):
            self.conn.execute(
                "INSERT INTO compactions (session_id, request_id, turn, scope, reason, status, "
                "tokens_before, tokens_after, cost_saved_usd, duration_ms, error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (session_id, request_id) DO UPDATE "
                "SET status = excluded.status, tokens_before = excluded.tokens_before, "
                "tokens_after = excluded.tokens_after, cost_saved_usd = excluded.cost_saved_usd, "
                "duration_ms = excluded.duration_ms, error = excluded.error",
                (
                    sid,
                    event.request_id,
                    event.turn,
                    event.scope,
                    event.reason,
                    event.status,
                    event.tokens_before,
                    event.tokens_after,
                    event.cost_saved_usd,
                    event.duration_ms,
                    _clip(event.error, ERROR_CHARS),
                ),
            )
        elif isinstance(event, ContextCompacted):
            self.conn.execute(
                "INSERT OR IGNORE INTO compactions (session_id, request_id, turn, scope, reason, "
                "model_key, status, tokens_before, tokens_after, duration_ms) "
                "VALUES (?,?,?,?,?,?,'success',?,?,?)",
                (
                    sid,
                    event.request_id,
                    event.turn,
                    event.scope,
                    event.reason,
                    event.model_key,
                    event.tokens_before,
                    event.tokens_after,
                    event.duration_ms,
                ),
            )
        # Other events (iteration_start, assistant_message) carry nothing the index stores.

    def _apply_request(self, event: LLMRequest) -> None:
        sid = self.session_id
        values = (
            event.sent_at,
            event.turn,
            event.iteration,
            event.scope,
            event.model_key,
            event.status,
            event.input_tokens,
            event.output_tokens,
            event.cache_read_tokens,
            event.cache_write_tokens,
            event.context_tokens,
            event.cost_usd,
            event.duration_ms,
            event.stop_reason,
            _clip(event.error, ERROR_CHARS),
            event.subagent_index,
            event.purpose,
        )
        cursor = self.conn.execute(
            f"INSERT OR IGNORE INTO requests (session_id, event_id, {','.join(_REQUEST_COLUMNS)}) "
            f"VALUES ({','.join('?' * (len(_REQUEST_COLUMNS) + 2))})",
            (sid, event.id, *values),
        )
        if cursor.rowcount == 0:
            existing = self.conn.execute(
                f"SELECT {','.join(_REQUEST_COLUMNS)} FROM requests "
                "WHERE session_id = ? AND event_id = ?",
                (sid, event.id),
            ).fetchone()
            if existing is not None and tuple(existing) != values:
                log.error("Metrics conflicting duplicate request %s in %s", event.id, sid)
            return
        self.request_times[event.id] = (event.sent_at, event.model_key)
        end = _end_time(event.sent_at, event.duration_ms)
        self.conn.execute(
            "UPDATE sessions SET last_activity_at = ?1 WHERE session_id = ?2 AND "
            "(last_activity_at IS NULL OR last_activity_at < ?1)",
            (end, sid),
        )
        self._touch_turn(event.turn, event.sent_at)
        if event.scope is None and event.purpose == "conversation":
            # Kiro writes one llm_request per turn after the turn's tool calls, whose
            # segment request ids never match it; give those rows the turn's request
            # time and model. Native rows already resolved at insert and are untouched.
            self.conn.execute(
                "UPDATE tool_calls SET timestamp = ?, model_key = ? "
                "WHERE session_id = ? AND turn = ? AND timestamp IS NULL",
                (event.sent_at, event.model_key, sid, event.turn),
            )

    def _apply_tool_call(self, event: ToolCall) -> None:
        sent_at, model_key = self._request_info(event.request_id)
        skill = None
        if event.name == "skill" and isinstance(event.input.get("name"), str):
            skill = event.input["name"]
        self.conn.execute(
            "INSERT OR IGNORE INTO tool_calls (session_id, tool_use_id, event_id, turn, "
            "iteration, scope, subagent_index, request_id, timestamp, model_key, name, skill) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.session_id,
                event.tool_use_id,
                event.id,
                event.turn,
                event.iteration,
                event.scope,
                event.subagent_index,
                event.request_id,
                sent_at,
                model_key,
                event.name,
                skill,
            ),
        )
        if event.name == "task":
            agents = _task_agents(event.input)
            for index, agent in enumerate(agents or []):
                self.conn.execute(
                    "INSERT OR IGNORE INTO subagents (session_id, scope, subagent_index, agent, "
                    "parent_scope, turn) VALUES (?,?,?,?,?,?)",
                    (self.session_id, event.tool_use_id, index, agent, event.scope, event.turn),
                )

    def _apply_turn_end(self, event) -> None:
        if isinstance(event, TurnComplete):
            status, stop, error = "completed", event.stop_reason, None
        elif isinstance(event, TurnError):
            status, stop, error = "error", None, _clip(event.message, ERROR_CHARS)
        else:
            status, stop, error = "interrupted", None, None
        if event.scope is None:
            self._touch_turn(event.turn, None)
            self.conn.execute(
                "UPDATE turns SET status = ?, stop_reason = ?, error = ? "
                "WHERE session_id = ? AND turn = ?",
                (status, stop, error, self.session_id, event.turn),
            )
        elif event.subagent_index is not None:
            self.conn.execute(
                "UPDATE subagents SET status = ?, error = ? "
                "WHERE session_id = ? AND scope = ? AND subagent_index = ?",
                (status, error, self.session_id, event.scope, event.subagent_index),
            )


class MetricsIndex:
    """Incrementally index session logs into ``metrics.db``."""

    def __init__(self, db_path: Path, sessions_dir: Path) -> None:
        self.db_path = Path(db_path)
        self.sessions_dir = Path(sessions_dir)
        self.lock_path = self.db_path.with_name(self.db_path.name + ".lock")

    @contextmanager
    def _locked(self, wait: bool) -> Iterator[bool]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def try_lock(self) -> bool:
        """Return True when no other process or thread holds the sync lock."""
        with self._locked(wait=False) as acquired:
            return acquired

    def _open_current(self) -> sqlite3.Connection:
        """Archive a stale-schema DB, then open (and create) the current schema."""
        version = schema_version(self.db_path)
        if version is not None and version != SCHEMA_VERSION:
            dest = _archive_database(self.db_path)
            log.info(
                "Metrics database schema v%s != v%s; archived to %s", version, SCHEMA_VERSION, dest
            )
        conn = connect(self.db_path, create=True)
        for statement in _SCHEMA:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        return conn

    def sync(
        self, paths: Iterable[Path] | None = None, *, strict: bool = False, wait: bool = True
    ) -> SyncStats | None:
        """Index new log content. Returns None when ``wait=False`` and the lock is held.

        ``paths`` defaults to every ``*.jsonl`` in the sessions directory. In
        strict mode a file or SQLite error raises instead of being logged.
        """
        with self._locked(wait) as acquired:
            if not acquired:
                return None
            return self._sync_locked(paths, strict=strict)

    def reset_and_backfill(self, paths: Iterable[Path]) -> SyncStats:
        """Archive the current DB and rebuild it from ``paths``, raising on any failure."""
        with self._locked(wait=True):
            archived = _archive_database(self.db_path)
            if archived is not None:
                log.info("Archived metrics database to %s", archived)
            stats = self._sync_locked(paths, strict=True)
            assert stats is not None
            return stats

    def _sync_locked(self, paths: Iterable[Path] | None, *, strict: bool) -> SyncStats:
        stats = SyncStats()
        if paths is None:
            try:
                paths = sorted(self.sessions_dir.glob("*.jsonl"))
            except OSError:
                paths = []
        conn = self._open_current()
        try:
            for path in paths:
                stats.files_seen += 1
                try:
                    self._sync_file(conn, Path(path), stats)
                except sqlite3.OperationalError:
                    # Locked, disk or I/O trouble: the next sync retries from here.
                    conn.rollback()
                    if strict:
                        raise
                    log.warning("Metrics index sync abandoned at %s", path, exc_info=True)
                    stats.errors += 1
                    break
                except (OSError, sqlite3.Error) as exc:
                    # A problem with this file: record it and index the rest.
                    conn.rollback()
                    if strict:
                        raise
                    stats.errors += 1
                    log.warning("Metrics index skipped %s: %s", path, exc)
                    self._record_error(conn, Path(path), str(exc))
        finally:
            conn.close()
        return stats

    def _record_error(self, conn: sqlite3.Connection, path: Path, error: str) -> None:
        try:
            conn.execute(
                "INSERT INTO ingest_files (session_id, offset, size, mtime_ns, head_sha, error) "
                "VALUES (?, 0, -1, -1, '', ?) ON CONFLICT (session_id) DO UPDATE SET error = ?",
                (path.stem, error, error),
            )
            conn.commit()
        except sqlite3.Error:
            conn.rollback()

    def _sync_file(self, conn: sqlite3.Connection, path: Path, stats: SyncStats) -> None:
        session_id = path.stem
        stat = path.stat()
        row = conn.execute(
            "SELECT offset, size, mtime_ns, head_sha, invalid_lines FROM ingest_files "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is not None and row["size"] == stat.st_size and row["mtime_ns"] == stat.st_mtime_ns:
            return
        stats.files_changed += 1
        with path.open("rb") as stream:
            head = stream.readline()
            head_sha = hashlib.sha256(head).hexdigest()
            offset = row["offset"] if row is not None else 0
            invalid = row["invalid_lines"] if row is not None else 0
            if row is not None and (stat.st_size < offset or row["head_sha"] != head_sha):
                for table in _SESSION_TABLES:
                    conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
                offset, invalid = 0, 0
                stats.reset += 1
            stream.seek(offset)
            data = stream.read()
        end = data.rfind(b"\n") + 1
        folder = _Folder(conn, session_id)
        for raw in data[:end].splitlines():
            if not raw.strip():
                continue
            try:
                event = decode_event(raw, persisted=True)
            except (ValueError, TypeError, msgspec.DecodeError, msgspec.ValidationError):
                invalid += 1
                stats.invalid_lines += 1
                continue
            folder.apply(event)
            stats.events += 1
        conn.execute(
            "INSERT INTO ingest_files (session_id, offset, size, mtime_ns, head_sha, "
            "invalid_lines, error) VALUES (?,?,?,?,?,?,NULL) ON CONFLICT (session_id) DO UPDATE "
            "SET offset = excluded.offset, size = excluded.size, mtime_ns = excluded.mtime_ns, "
            "head_sha = excluded.head_sha, invalid_lines = excluded.invalid_lines, error = NULL",
            (session_id, offset + end, stat.st_size, stat.st_mtime_ns, head_sha, invalid),
        )
        conn.commit()


def reset_and_backfill(db_path: Path, session_log_paths: Iterable[Path]) -> SyncStats:
    """Archive the metrics index and rebuild it from ``session_log_paths``.

    Used by ``archie migrate-sessions``. Logs are only read, so a failure
    raises and a later run can archive the partial index and retry.
    """
    db_path = Path(db_path)
    return MetricsIndex(db_path, db_path.parent / "sessions").reset_and_backfill(
        list(session_log_paths)
    )


def default_index() -> MetricsIndex:
    """The index for ``<home_dir()>/metrics.db`` over ``<home_dir()>/sessions``."""
    from archie_shared.config import home_dir

    home = home_dir()
    return MetricsIndex(home / "metrics.db", home / "sessions")

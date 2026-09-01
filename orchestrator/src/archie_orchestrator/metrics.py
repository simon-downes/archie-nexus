"""Idempotent SQLite persistence for canonical LLM request events."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA_VERSION = 3
_CREATE_TABLE = """CREATE TABLE IF NOT EXISTS requests (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, event_id TEXT NOT NULL,
 timestamp TEXT NOT NULL, turn INTEGER NOT NULL, iteration INTEGER NOT NULL, scope TEXT, model_key TEXT NOT NULL,
 status TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
 cache_read_tokens INTEGER NOT NULL, cache_write_tokens INTEGER NOT NULL, context_tokens INTEGER NOT NULL,
 cost_usd REAL NOT NULL, duration_ms INTEGER NOT NULL, UNIQUE (session_id, event_id))"""
_QUEUE_MAXSIZE = 10_000


class MetricsWriter:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)

    @property
    def queue(self) -> asyncio.Queue[tuple[str, str]]:
        return self._queue

    async def run(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate_if_needed()
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn = self._ensure_schema(conn)
            while True:
                session_id, raw = await self._queue.get()
                self._process_batch(conn, [(session_id, raw)])
        except asyncio.CancelledError:
            raise
        finally:
            conn.close()

    def _migrate_if_needed(self) -> None:
        """Archive a stale-schema database before it is opened.

        If the database file exists with a ``user_version`` other than the
        current schema version, rename it to ``<path>.legacy.<UTC>`` so a fresh
        version-3 database is created in its place. On a naming collision,
        append ``-1``, ``-2``, ... until an unused destination is found. The
        legacy data is preserved rather than dropped.
        """
        if not self._db_path.exists():
            return
        probe = sqlite3.connect(str(self._db_path))
        try:
            version = probe.execute("PRAGMA user_version").fetchone()[0]
        finally:
            probe.close()
        if version == _SCHEMA_VERSION:
            return
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        base = self._db_path.with_name(f"{self._db_path.name}.legacy.{stamp}")
        dest = base
        suffix = 0
        while dest.exists():
            suffix += 1
            dest = base.with_name(f"{base.name}-{suffix}")
        self._db_path.rename(dest)
        log.info(
            "Metrics database schema v%s != v%s; archived to %s",
            version,
            _SCHEMA_VERSION,
            dest,
        )

    def _ensure_schema(self, conn: sqlite3.Connection) -> sqlite3.Connection:
        conn.execute(_CREATE_TABLE)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_session ON requests(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model_key)")
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.commit()
        return conn

    @staticmethod
    def _infer_backend(model_key: str) -> str | None:
        if model_key.startswith("bedrock-"):
            return "bedrock"
        if model_key.startswith("ollama-"):
            return "ollama"
        return None

    _MATERIAL_COLUMNS = (
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
    )

    def _process_batch(self, conn: sqlite3.Connection, batch: list[tuple[str, str]]) -> None:
        for session_id, raw in batch:
            try:
                event = json.loads(raw)
                if not isinstance(event, dict):
                    log.warning("Metrics request skipped: expected a JSON object")
                    continue
                if event.get("type") != "llm_request":
                    continue
                values = (
                    event["sent_at"],
                    event["turn"],
                    event["iteration"],
                    event.get("scope"),
                    event["model_key"],
                    event["status"],
                    event["input_tokens"],
                    event["output_tokens"],
                    event["cache_read_tokens"],
                    event["cache_write_tokens"],
                    event["context_tokens"],
                    event["cost_usd"],
                    event["duration_ms"],
                )
                existing = conn.execute(
                    f"SELECT {','.join(self._MATERIAL_COLUMNS)} FROM requests "
                    "WHERE session_id = ? AND event_id = ?",
                    (session_id, event["id"]),
                ).fetchone()
                if existing is not None:
                    # Identical duplicate → no-op; conflicting duplicate → error.
                    if tuple(existing) != values:
                        log.error(
                            "Metrics conflicting duplicate for (%s, %s): stored=%s incoming=%s",
                            session_id,
                            event["id"],
                            tuple(existing),
                            values,
                        )
                    continue
                conn.execute(
                    """INSERT INTO requests
                    (session_id,event_id,timestamp,turn,iteration,scope,model_key,status,input_tokens,
                     output_tokens,cache_read_tokens,cache_write_tokens,context_tokens,cost_usd,duration_ms)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (session_id, event["id"], *values),
                )
            except (json.JSONDecodeError, KeyError, TypeError, sqlite3.Error) as exc:
                log.warning("Metrics request skipped: %s", exc)
        conn.commit()


def _archive_database(db_path: Path) -> Path | None:
    """Archive an existing metrics database without overwriting a prior archive."""
    if not db_path.exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = db_path.with_name(f"{db_path.name}.legacy.{stamp}")
    destination = base
    suffix = 0
    while destination.exists():
        suffix += 1
        destination = base.with_name(f"{base.name}-{suffix}")
    db_path.rename(destination)
    return destination


def reset_and_backfill(db_path: Path, session_log_paths: Iterable[Path]) -> None:
    """Create a fresh metrics index and rebuild it from migrated session logs.

    The existing database is archived before the new schema is created. Logs
    are read only, so a backfill failure leaves them intact and a later run can
    archive the partial index and retry safely.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    archived = _archive_database(db_path)
    if archived is not None:
        log.info("Archived metrics database to %s", archived)

    writer = MetricsWriter(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        writer._ensure_schema(conn)
        for path in session_log_paths:
            path = Path(path)
            session_id = path.stem
            batch = [(session_id, raw) for raw in path.read_text(encoding="utf-8").splitlines()]
            writer._process_batch(conn, batch)
    finally:
        conn.close()

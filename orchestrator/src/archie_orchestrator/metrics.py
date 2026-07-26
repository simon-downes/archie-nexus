"""Metrics aggregation for the orchestrator.

Captures token usage and cost from WS proxy frames in real-time.
Stores one row per LLM request in a SQLite database at ~/.nexus/metrics.db.

Architecture:
- The WS proxy puts (session_id, raw_frame) tuples onto MetricsWriter.queue.
- MetricsWriter.run() drains the queue in a background asyncio task.
- SQLite writes use WAL mode and batch where possible.
- Extraction failures never affect the proxy relay.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL schema
# ---------------------------------------------------------------------------

_CREATE_TABLE = """
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
)
"""

_CREATE_IDX_SESSION = (
    "CREATE INDEX IF NOT EXISTS idx_requests_session ON requests(session_id)"
)
_CREATE_IDX_TIMESTAMP = (
    "CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp)"
)

_INSERT = (
    "INSERT INTO requests "
    "(session_id, timestamp, turn_index, model, backend, "
    "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
    "cost, context_pct) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


# ---------------------------------------------------------------------------
# Rate tracking
# ---------------------------------------------------------------------------


@dataclass
class SessionRates:
    """Most recent model and cost rates for a session."""

    model: str = "unknown"
    backend: str | None = None
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0


# ---------------------------------------------------------------------------
# MetricsWriter
# ---------------------------------------------------------------------------


class MetricsWriter:
    """Background SQLite writer fed by the WS proxy relay.

    Usage:
        writer = MetricsWriter(db_path)
        task = asyncio.create_task(writer.run())
        # proxy puts (session_id, raw_msg) onto writer.queue
        task.cancel()
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._rates: dict[str, SessionRates] = {}

    @property
    def queue(self) -> asyncio.Queue[tuple[str, str]]:
        """The async queue that the proxy relay puts events onto."""
        return self._queue

    async def run(self) -> None:
        """Background loop: drain queue, batch-write to SQLite.

        Runs until cancelled. Opens a single SQLite connection for the lifetime
        of the task. Ensures schema on startup.
        """
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            self._ensure_schema(conn)
            while True:
                # Block until at least one item is available
                item = await self._queue.get()
                batch = [item]
                # Drain any additional queued items (non-blocking)
                while not self._queue.empty():
                    batch.append(self._queue.get_nowait())
                self._process_batch(conn, batch)
        except asyncio.CancelledError:
            # Drain remaining items before exit
            remaining = []
            while not self._queue.empty():
                remaining.append(self._queue.get_nowait())
            if remaining:
                self._process_batch(conn, remaining)
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_IDX_SESSION)
        conn.execute(_CREATE_IDX_TIMESTAMP)
        conn.commit()

    def _process_batch(
        self, conn: sqlite3.Connection, batch: list[tuple[str, str]]
    ) -> None:
        """Parse and process a batch of (session_id, raw_msg) tuples."""
        rows: list[tuple] = []
        for session_id, raw_msg in batch:
            try:
                event = json.loads(raw_msg)
            except json.JSONDecodeError:
                log.warning("Metrics: malformed event JSON — skipping")
                continue

            event_type = event.get("type")

            if event_type == "session_info":
                self._handle_session_info(session_id, event)

            elif event_type == "model_switched":
                self._handle_model_switched(session_id, event)

            elif event_type == "usage":
                row = self._build_usage_row(session_id, event)
                if row is not None:
                    rows.append(row)

        if rows:
            try:
                conn.executemany(_INSERT, rows)
                conn.commit()
            except sqlite3.Error as exc:
                log.error("Metrics DB write failed: %s", exc)

    def _handle_session_info(self, session_id: str, event: dict) -> None:
        data = event.get("data", {})
        cost = data.get("cost", {})
        model = data.get("model", "unknown")
        self._rates[session_id] = SessionRates(
            model=model,
            # session_info carries no model_key, so backend inference is best-effort
            # from the human model name. Will be overridden by model_switched if received.
            backend=self._infer_backend(model),
            input=cost.get("input", 0.0),
            output=cost.get("output", 0.0),
            cache_read=cost.get("cache_read", 0.0),
            cache_write=cost.get("cache_write", 0.0),
        )

    def _handle_model_switched(self, session_id: str, event: dict) -> None:
        data = event.get("data", {})
        cost = data.get("cost", {})
        model_name = data.get("model_name", "unknown")
        model_key = data.get("model_key", "")
        self._rates[session_id] = SessionRates(
            model=model_name,
            backend=self._infer_backend(model_key),
            input=cost.get("input", 0.0),
            output=cost.get("output", 0.0),
            cache_read=cost.get("cache_read", 0.0),
            cache_write=cost.get("cache_write", 0.0),
        )

    def _build_usage_row(
        self, session_id: str, event: dict
    ) -> tuple | None:
        rates = self._rates.get(session_id, SessionRates())
        data = event.get("data", {})
        try:
            in_tok = int(data["input_tokens"])
            out_tok = int(data["output_tokens"])
            cr_tok = int(data["cache_read_tokens"])
            cw_tok = int(data["cache_write_tokens"])
        except (KeyError, TypeError, ValueError):
            log.warning("Metrics: Usage event missing token fields — skipping")
            return None

        cost = (
            in_tok * rates.input
            + out_tok * rates.output
            + cr_tok * rates.cache_read
            + cw_tok * rates.cache_write
        ) / 1_000_000

        turn_index = event.get("turn_index", 0)
        context_pct = data.get("context_pct", 0.0)
        timestamp = datetime.now(UTC).isoformat()

        return (
            session_id,
            timestamp,
            turn_index,
            rates.model,
            rates.backend,
            in_tok,
            out_tok,
            cr_tok,
            cw_tok,
            cost,
            context_pct,
        )

    @staticmethod
    def _infer_backend(model_key: str) -> str | None:
        """Infer provider backend from model key prefix convention."""
        if model_key.startswith("bedrock-"):
            return "bedrock"
        if model_key.startswith("ollama-"):
            return "ollama"
        return None

"""Metrics aggregation and SQLite persistence for usage wire events."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from archie_shared.models import CostConfig, calculate_cost, sanitize_billable_usage

log = logging.getLogger(__name__)

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
_CREATE_IDX_SESSION = "CREATE INDEX IF NOT EXISTS idx_requests_session ON requests(session_id)"
_CREATE_IDX_TIMESTAMP = "CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp)"
_INSERT = "INSERT INTO requests (session_id, timestamp, turn_index, model, backend, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost, context_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
_QUEUE_MAXSIZE = 10_000
_MAX_BATCH = 500


@dataclass
class SessionRates:
    model: str = "unknown"
    backend: str | None = None
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0

    def as_cost(self) -> CostConfig:
        return CostConfig(
            input=self.input,
            output=self.output,
            cache_read=self.cache_read,
            cache_write=self.cache_write,
        )


class MetricsWriter:
    """Background SQLite writer fed by the WebSocket proxy relay."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._rates: dict[str, SessionRates] = {}

    @property
    def queue(self) -> asyncio.Queue[tuple[str, str]]:
        return self._queue

    async def run(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            self._ensure_schema(conn)
            while True:
                item = await self._queue.get()
                batch = [item]
                while len(batch) < _MAX_BATCH and not self._queue.empty():
                    batch.append(self._queue.get_nowait())
                self._process_batch(conn, batch)
        except asyncio.CancelledError:
            remaining = []
            while not self._queue.empty():
                remaining.append(self._queue.get_nowait())
            if remaining:
                self._process_batch(conn, remaining)
            raise
        finally:
            conn.close()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_IDX_SESSION)
        conn.execute(_CREATE_IDX_TIMESTAMP)
        conn.commit()

    def _process_batch(self, conn: sqlite3.Connection, batch: list[tuple[str, str]]) -> None:
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
        written = 0
        for row in rows:
            try:
                conn.execute(_INSERT, row)
                written += 1
            except sqlite3.Error as exc:
                log.error("Metrics DB row write failed: %s", exc)
        if written:
            try:
                conn.commit()
            except sqlite3.Error as exc:
                log.error("Metrics DB commit failed: %s", exc)

    def _handle_session_info(self, session_id: str, event: dict) -> None:
        data = event.get("data", {})
        cost = data.get("cost", {})
        model = data.get("model", "unknown")
        self._rates[session_id] = SessionRates(
            model=model,
            backend=self._infer_backend(model),
            input=cost.get("input", 0.0),
            output=cost.get("output", 0.0),
            cache_read=cost.get("cache_read", 0.0),
            cache_write=cost.get("cache_write", 0.0),
        )

    def _handle_model_switched(self, session_id: str, event: dict) -> None:
        data = event.get("data", {})
        cost = data.get("cost", {})
        self._rates[session_id] = SessionRates(
            model=data.get("model_name", "unknown"),
            backend=self._infer_backend(data.get("model_key", "")),
            input=cost.get("input", 0.0),
            output=cost.get("output", 0.0),
            cache_read=cost.get("cache_read", 0.0),
            cache_write=cost.get("cache_write", 0.0),
        )

    def _build_usage_row(self, session_id: str, event: dict) -> tuple | None:
        rates = self._rates.get(session_id, SessionRates())
        data = event.get("data", {})
        try:
            in_tok, out_tok, cr_tok, cw_tok = sanitize_billable_usage(
                data["input_tokens"],
                data["output_tokens"],
                data["cache_read_tokens"],
                data["cache_write_tokens"],
            )
        except (KeyError, TypeError, ValueError):
            log.warning("Metrics: Usage event missing token fields — skipping")
            return None
        cost = calculate_cost(rates.as_cost(), in_tok, out_tok, cr_tok, cw_tok)
        try:
            turn_index = int(event.get("turn_index", 0))
        except (TypeError, ValueError):
            turn_index = 0
        try:
            context_pct = float(data.get("context_pct", 0.0))
        except (TypeError, ValueError):
            context_pct = 0.0
        return (
            session_id,
            datetime.now(UTC).isoformat(),
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
        if model_key.startswith("bedrock-"):
            return "bedrock"
        if model_key.startswith("ollama-"):
            return "ollama"
        return None

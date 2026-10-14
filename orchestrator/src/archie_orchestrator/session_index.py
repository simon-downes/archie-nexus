"""Summaries of persisted session logs for the session log viewer.

Reads ``<home_dir()>/sessions/*.jsonl`` with :class:`SessionLog` and reduces each
log to a :class:`SessionSummary`: workspace, start time, models by turn count,
usage totals from ``llm_request`` (the only accounting source), and the text that
free-text search matches (user and assistant messages, session ID, workspace).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from archie_shared.config import home_dir
from archie_shared.events import AssistantMessage, LLMRequest, UserMessage
from archie_shared.session import split_id
from archie_shared.session.accounting import UsageTotals
from archie_shared.session.log import EventIdConflict, LogAppendError, SessionLog

log = logging.getLogger(__name__)

_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"
_TIMESTAMP_CHARS = 10


def sessions_dir() -> Path:
    """Return the directory holding persisted session logs."""
    return home_dir() / "sessions"


def session_start(session_id: str) -> datetime | None:
    """Decode the session start from the 10-char ULID timestamp suffix of an ID."""
    _, suffix = split_id(session_id)
    if len(suffix) != _TIMESTAMP_CHARS:
        return None
    millis = 0
    for char in suffix.lower():
        value = _CROCKFORD.find(char)
        if value < 0:
            return None
        millis = millis * 32 + value
    try:
        return datetime.fromtimestamp(millis / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class SessionSummary:
    """One row of the session list; see the plan's Data model for field meanings."""

    session_id: str
    workspace: str
    started_at: datetime | None
    models: tuple[tuple[str, int], ...]
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    cost_usd: float
    search_text: str
    mtime: float
    error: str | None = None


def summarize_events(session_id: str, events, *, mtime: float = 0.0) -> SessionSummary:
    """Reduce canonical events to a summary (shared by the list and detail pages)."""
    workspace, _ = split_id(session_id)
    turns_by_model: dict[str, set[tuple]] = {}
    usage = UsageTotals()
    texts = [session_id, workspace]
    for event in events:
        if isinstance(event, LLMRequest):
            usage.add(event)
            if event.purpose == "conversation":
                turns_by_model.setdefault(event.model_key, set()).add(
                    (event.scope, event.subagent_index, event.turn)
                )
        elif isinstance(event, (UserMessage, AssistantMessage)) and event.content:
            texts.append(event.content)
    models = tuple(
        sorted(
            ((key, len(turns)) for key, turns in turns_by_model.items()),
            key=lambda item: (-item[1], item[0]),
        )
    )
    return SessionSummary(
        session_id=session_id,
        workspace=workspace,
        started_at=session_start(session_id),
        models=models,
        input_tokens=usage.input_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=round(usage.cost_usd, 6),
        search_text="\n".join(texts).lower(),
        mtime=mtime,
    )


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def read_events(path: Path) -> tuple[list, str | None]:
    """Read a log's valid events, or ``([], reason)`` when the file cannot be read.

    ``SessionLog`` skips invalid lines itself; this also covers conflicting
    duplicate IDs and files that are not valid UTF-8.
    """
    try:
        return SessionLog(path).read(), None
    except (EventIdConflict, LogAppendError, UnicodeDecodeError) as exc:
        log.warning("Unreadable session log %s: %s", path, exc)
        return [], str(exc)


def summarize_log(path: Path) -> SessionSummary:
    """Summarise one log file; an unreadable log yields a summary with ``error`` set."""
    events, error = read_events(path)
    summary = summarize_events(path.stem, events, mtime=_mtime(path))
    return replace(summary, error=error) if error else summary


def sort_key(summary: SessionSummary) -> tuple:
    """Newest first; logs without a decodable start sort after dated ones, by mtime."""
    if summary.started_at is not None:
        return (0, -summary.started_at.timestamp(), summary.session_id)
    return (1, -summary.mtime, summary.session_id)


class SessionIndex:
    """Cache summaries per file, re-reading a file only when its mtime or size changes."""

    def __init__(self) -> None:
        self._cache: dict[Path, tuple[tuple[int, int], SessionSummary]] = {}
        self._lock = threading.Lock()

    def summaries(self, directory: Path) -> list[SessionSummary]:
        """Return summaries for every ``*.jsonl`` in ``directory``, newest first."""
        try:
            paths = sorted(directory.glob("*.jsonl"))
        except OSError:
            return []
        result: list[SessionSummary] = []
        seen: set[Path] = set()
        with self._lock:
            for path in paths:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if not path.is_file():
                    continue
                seen.add(path)
                stamp = (stat.st_mtime_ns, stat.st_size)
                cached = self._cache.get(path)
                if cached is None or cached[0] != stamp:
                    cached = (stamp, summarize_log(path))
                    self._cache[path] = cached
                result.append(cached[1])
            for stale in set(self._cache) - seen:
                del self._cache[stale]
        result.sort(key=sort_key)
        return result

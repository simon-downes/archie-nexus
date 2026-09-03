"""Stateful durable storage for persisted session events."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import msgspec

from archie_shared.events import Event, LLMRequest, PersistedEvent, decode_event, encode_event

log = logging.getLogger(__name__)


class CursorNotFound(KeyError):  # noqa: N818
    """The requested history cursor is not present in the session log."""


class EventIdConflict(RuntimeError):  # noqa: N818
    """An event ID was reused with different canonical content."""


class LogAppendError(RuntimeError):
    """The session log could not be durably appended."""


def _append_serialized_event(path: Path, line: str) -> None:
    """Append one already-encoded line at the storage boundary."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()


class SessionLog:
    """Own one session log's valid events, ID index, and durable appends."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._events: list[PersistedEvent] = []
        self._index: dict[str, str] = {}
        self._scan()

    def _scan(self) -> None:
        if not self.path.exists():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise LogAppendError(f"failed to read session log {self.path}: {exc}") from exc

        for number, raw in enumerate(lines, 1):
            try:
                event = decode_event(raw, persisted=True)
                if not event.id:
                    raise ValueError("canonical events require a non-empty id")
                canonical = encode_event(event)
            except (ValueError, TypeError, msgspec.DecodeError, msgspec.ValidationError) as exc:
                log.warning(
                    "Skipping invalid session log line %d in %s: %s", number, self.path, exc
                )
                continue

            previous = self._index.get(event.id)
            if previous is not None:
                if previous == canonical:
                    log.warning(
                        "Skipping identical duplicate session log line %d in %s for event %s",
                        number,
                        self.path,
                        event.id,
                    )
                    continue
                raise EventIdConflict(f"conflicting duplicate event id: {event.id}")

            self._events.append(event)
            self._index[event.id] = canonical

    def append(self, event: PersistedEvent) -> bool:
        """Durably append a new persisted event, or return false for an identical retry."""
        if not isinstance(event, Event) or not event.persist:
            raise TypeError(f"event {type(event).__name__} is live-only")
        if not event.id:
            raise ValueError("canonical events require a non-empty id")

        canonical = encode_event(event)
        existing = self._index.get(event.id)
        if existing is not None:
            if existing == canonical:
                return False
            raise EventIdConflict(f"conflicting duplicate event id: {event.id}")

        try:
            _append_serialized_event(self.path, canonical)
        except OSError as exc:
            raise LogAppendError(f"failed to append event {event.id}: {exc}") from exc
        self._events.append(event)
        self._index[event.id] = canonical
        return True

    def read(self, after_id: str | None = None) -> list[PersistedEvent]:
        """Return valid persisted events in append order after an optional cursor."""
        if after_id:
            for index, event in enumerate(self._events):
                if event.id == after_id:
                    return list(self._events[index + 1 :])
            raise CursorNotFound(after_id)
        return list(self._events)


def session_accounting(path: Path) -> dict[str, Any]:
    """Compute token and cost totals from persisted request events."""
    events = SessionLog(path).read()
    latest_event_id = events[-1].id if events else None
    totals = {
        "total_cost": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_cache_write_tokens": 0,
    }
    for event in events:
        if not isinstance(event, LLMRequest):
            continue
        totals["total_cost"] += event.cost_usd
        totals["total_input_tokens"] += event.input_tokens
        totals["total_output_tokens"] += event.output_tokens
        totals["total_cache_read_tokens"] += event.cache_read_tokens
        totals["total_cache_write_tokens"] += event.cache_write_tokens
    totals["total_cost"] = round(totals["total_cost"], 6)
    return {"latest_event_id": latest_event_id, **totals}

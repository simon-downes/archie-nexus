"""Session log persistence for canonical NDJSON events.

Each session produces one JSONL file at <ARCHIE_HOME_DIR>/sessions/{id}.jsonl.
Each line is one canonical event. Legacy ``MessageEntry`` structures and
writers live in ``session.migrate`` for the one-shot M7 conversion only.
"""

import logging
from pathlib import Path

import msgspec

from archie_shared.canonical_events import (
    CanonicalEvent,
    PersistedEvent,
    decode_event,
    encode_event,
)

log = logging.getLogger(__name__)


def append_serialized_event(path: Path, line: str) -> None:
    """Append one already-validated canonical line without reparsing the log."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()


def append_event(path: Path, event: CanonicalEvent, serialized: str | None = None) -> str:
    """Append a canonical event, returning the exact persisted JSON string.

    This compatibility helper retains the original stateless API. New agent
    code uses ``SessionEventBus`` so duplicate detection is performed by its
    append-side index rather than reparsing the complete log per event.
    """
    if not getattr(event, "id", ""):
        raise ValueError("canonical events require a non-empty id")
    line = serialized if serialized is not None else encode_event(event)
    decoded = decode_event(line, persisted=True)
    if decoded.id != event.id:
        raise ValueError("serialized event id does not match event")
    existing = read_event_lines(path)
    for old in existing:
        if old.get("id") == event.id:
            if old["line"] == line:
                return line
            raise ValueError(f"conflicting duplicate event id: {event.id}")
    append_serialized_event(path, line)
    return line


def read_event_lines(path: Path) -> list[dict[str, str]]:
    """Read valid canonical lines in append order, skipping malformed records."""
    result: list[dict[str, str]] = []
    if not path.exists():
        return result
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        try:
            event = decode_event(raw, persisted=True)
        except (ValueError, TypeError, msgspec.DecodeError) as exc:
            log.warning("Skipping malformed canonical event line %d: %s", number, exc)
            continue
        result.append({"id": event.id, "line": raw})
    return result


def read_events(path: Path, after_id: str | None = None) -> list[PersistedEvent]:
    """Replay canonical events in JSONL order."""
    lines = read_event_lines(path)
    start = 0
    if after_id is not None:
        for index, item in enumerate(lines):
            if item["id"] == after_id:
                start = index + 1
                break
        else:
            raise KeyError(after_id)
    return [decode_event(item["line"], persisted=True) for item in lines[start:]]


def session_accounting(path: Path) -> dict:
    """Compute authoritative cumulative accounting from the persisted event log.

    Returns the latest persisted event id (replay cursor, ``None`` if the log
    is empty) and cumulative token/cost totals summed across all ``llm_request``
    events. This is derived purely from disk, so it is correct for a freshly
    attached client regardless of the agent process lifetime.
    """
    lines = read_event_lines(path)
    latest_event_id = lines[-1]["id"] if lines else None
    totals = {
        "total_cost": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_cache_write_tokens": 0,
    }
    for item in lines:
        event = decode_event(item["line"], persisted=True)
        if getattr(event, "type", None) != "llm_request":
            continue
        totals["total_cost"] += event.cost_usd
        totals["total_input_tokens"] += event.input_tokens
        totals["total_output_tokens"] += event.output_tokens
        totals["total_cache_read_tokens"] += event.cache_read_tokens
        totals["total_cache_write_tokens"] += event.cache_write_tokens
    totals["total_cost"] = round(totals["total_cost"], 6)
    return {"latest_event_id": latest_event_id, **totals}

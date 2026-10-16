"""Local input validation. Every helper raises IncidentIoInputError before any request."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from .client import IncidentIoInputError

_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_REFERENCE = re.compile(r"^(?:INC-)?([1-9][0-9]{0,9})$", re.IGNORECASE)
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_MAX_NAME = 200
_MAX_VALUES = 20


def limit(value: Any, maximum: int = 100) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise IncidentIoInputError(f"limit must be an integer 1-{maximum}")
    return value


def identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value.strip()):
        raise IncidentIoInputError(f"{name} must be an ID of 1-100 letters, digits, '_' or '-'")
    return value.strip()


def optional_identifier(value: Any, name: str) -> str | None:
    return None if value is None else identifier(value, name)


def incident_id(value: Any) -> str:
    """Accept a ULID-style ID, ``INC-123`` or ``123``; send the bare number for references."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    if not isinstance(value, str):
        raise IncidentIoInputError("incident must be an ID, INC-<number> or <number>")
    stripped = value.strip()
    match = _REFERENCE.fullmatch(stripped)
    if match:
        return match.group(1)
    if stripped.upper().startswith("INC-") or stripped.isdigit():
        raise IncidentIoInputError("incident reference must be INC-<positive number>")
    return identifier(stripped, "incident")


def text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > _MAX_NAME:
        raise IncidentIoInputError(f"{name} must be 1-{_MAX_NAME} characters")
    return value.strip()


def optional_text(value: Any, name: str) -> str | None:
    return None if value is None else text(value, name)


def values(value: Any, name: str) -> list[str]:
    """Normalise a string or non-empty list of strings to a de-duplicated list."""
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)) or not items or len(items) > _MAX_VALUES:
        raise IncidentIoInputError(f"{name} must be a value or a list of 1-{_MAX_VALUES} values")
    result: list[str] = []
    for item in items:
        clean = text(item, name)
        if clean not in result:
            result.append(clean)
    return result


def enum(value: Any, name: str, allowed: Iterable[str]) -> list[str]:
    options = tuple(allowed)
    result = [item.lower() for item in values(value, name)]
    bad = [item for item in result if item not in options]
    if bad:
        raise IncidentIoInputError(f"{name} must be one of: {', '.join(options)}")
    return result


def timestamp(value: Any, name: str) -> datetime:
    """Parse an ISO-8601 date or datetime; naive values are UTC."""
    if not isinstance(value, str) or not value.strip():
        raise IncidentIoInputError(f"{name} must be an ISO-8601 date or datetime")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise IncidentIoInputError(f"{name} must be an ISO-8601 date or datetime") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def date_filters(
    params: dict[str, Any],
    field: str,
    after: Any = None,
    before: Any = None,
    *,
    date_only: bool = False,
) -> None:
    """Add ``<field>[gte]``/``<field>[lte]`` params for a validated range.

    ``date_only`` endpoints (incidents) accept only ``YYYY-MM-DD``; a datetime is rejected
    rather than silently truncated.
    """
    prefix = field.removesuffix("_at")
    bounds = []
    for suffix, value in (("after", after), ("before", before)):
        if value is None:
            bounds.append(None)
            continue
        label = f"{prefix}_{suffix}"
        if date_only and (not isinstance(value, str) or not _DATE.fullmatch(value.strip())):
            raise IncidentIoInputError(f"{label} must be a YYYY-MM-DD date")
        bounds.append(timestamp(value, label))
    start, end = bounds
    if start and end and end < start:
        raise IncidentIoInputError(f"{prefix} range end must not be before its start")
    render = (lambda d: d.strftime("%Y-%m-%d")) if date_only else iso
    if start:
        params[f"{field}[gte]"] = render(start)
    if end:
        params[f"{field}[lte]"] = render(end)


def window(start: Any, end: Any, *, max_days: int = 31) -> tuple[datetime, datetime]:
    """Resolve an on-call window: default now → now + 1 minute, at most ``max_days``."""
    begin = timestamp(start, "start") if start is not None else datetime.now(UTC)
    finish = timestamp(end, "end") if end is not None else begin + timedelta(minutes=1)
    if finish <= begin:
        raise IncidentIoInputError("end must be after start")
    if finish - begin > timedelta(days=max_days):
        raise IncidentIoInputError(f"window must be at most {max_days} days")
    return begin, finish

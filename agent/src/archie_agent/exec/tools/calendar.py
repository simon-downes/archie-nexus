"""Bounded Google Calendar read tools and private event seam."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from archie_agent.exec.tools import tool
from archie_agent.exec.tools.google_common import (
    GoogleValidationError,
    bounded_limit,
    clean,
    credential,
    google_json,
    require_read,
    safe_id,
    safe_url,
    text,
)


async def calendar_request(operation: str, payload: dict[str, Any], token: str) -> Any:
    if operation == "get":
        return await google_json(
            f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{payload['id']}",
            token,
        )
    params = {
        **payload,
        "singleEvents": True,
        "showHiddenInvitations": True,
        "orderBy": "startTime",
    }
    if not params.get("q"):
        params.pop("q", None)
    data = await google_json(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events",
        token,
        params=params,
    )
    return data.get("items", [])


def _timezone() -> ZoneInfo:
    configured = os.environ.get("ARCHIE_TIMEZONE", "Europe/London")
    try:
        return ZoneInfo(configured)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Europe/London")


def _now() -> datetime:
    return datetime.now(UTC).astimezone(_timezone())


def _event_time(value: Any, name: str) -> str | None:
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("dateTime"), str):
        raw = value["dateTime"]
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise GoogleValidationError(f"invalid Calendar {name}") from None
        if parsed.tzinfo is None:
            raise GoogleValidationError(f"invalid Calendar {name}")
        return clean(raw, 64)
    if isinstance(value.get("date"), str):
        parsed = value["date"]
        try:
            date.fromisoformat(parsed)
        except ValueError:
            raise GoogleValidationError(f"invalid Calendar {name}") from None
        return parsed
    return None


def _organizer(value: Any) -> str | None:
    if not isinstance(value, dict):
        return clean(value, 500)
    return clean(value.get("email") or value.get("displayName"), 500)


def _attendees(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for attendee in value[:100]:
        if not isinstance(attendee, dict):
            continue
        email = clean(attendee.get("email"), 320)
        if not email:
            continue
        result.append(
            {
                "email": email,
                "display_name": clean(attendee.get("displayName"), 256),
                "response_status": clean(attendee.get("responseStatus"), 32),
            }
        )
    return result


def _event(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        event_id = safe_id(raw.get("id"), "event_id")
        start = _event_time(raw.get("start"), "start")
        end = _event_time(raw.get("end"), "end")
    except GoogleValidationError:
        return None
    if start is None or end is None:
        return None
    conference_url = None
    conference = raw.get("conferenceData")
    if isinstance(conference, dict):
        entries = conference.get("entryPoints", [])
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("entryPointType") == "video":
                    conference_url = safe_url(entry.get("uri"))
                    if conference_url:
                        break
    return {
        "id": event_id,
        "summary": clean(raw.get("summary"), 500),
        "description": clean(raw.get("description"), 4000),
        "start": start,
        "end": end,
        "status": clean(raw.get("status"), 32),
        "organizer": _organizer(raw.get("organizer")),
        "attendees": _attendees(raw.get("attendees")),
        "location": clean(raw.get("location"), 500),
        "meet_url": conference_url or safe_url(raw.get("hangoutLink")),
        "provenance": "google.calendar",
    }


def _today_window() -> tuple[str, str]:
    current = _now()
    start = datetime.combine(current.date(), time.min, tzinfo=current.tzinfo)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _upcoming_window(days: int) -> tuple[str, str]:
    current = _now()
    return current.isoformat(), (current + timedelta(days=days)).isoformat()


def _normalize(raw: Any, limit: int) -> list[dict[str, Any]]:
    return [
        item for item in (_event(value) for value in (raw if isinstance(raw, list) else [])) if item
    ][:limit]


async def get_event(event_id: str, policy_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Private normalized event lookup for sibling Google tools."""
    event_id = safe_id(event_id, "event_id")
    require_read(policy_snapshot)
    result = _event(
        await calendar_request("get", {"id": event_id}, credential().access_token or "")
    )
    if result is None:
        raise GoogleValidationError("Calendar event was not found or malformed")
    return result


async def search_events(
    query: str,
    start: datetime,
    end: datetime,
    limit: int,
    policy_snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    query = text(query, "query") if query else ""
    limit = bounded_limit(limit, maximum=50)
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise GoogleValidationError("invalid event range")
    require_read(policy_snapshot)
    raw = await calendar_request(
        "search",
        {
            "q": query,
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "maxResults": limit,
        },
        credential().access_token or "",
    )
    return _normalize(raw, limit)


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="calendar")
async def today() -> list[dict[str, Any]]:
    """Return events intersecting today in the configured local timezone."""
    require_read()
    start, end = _today_window()
    raw = await calendar_request(
        "list",
        {"timeMin": start, "timeMax": end, "maxResults": 50},
        credential().access_token or "",
    )
    return _normalize(raw, 50)


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="calendar")
async def upcoming(days: int = 7, limit: int = 50) -> list[dict[str, Any]]:
    """Return events in the next bounded timezone-aware window."""
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 31:
        raise GoogleValidationError("days must be 1-31")
    limit = bounded_limit(limit, maximum=50)
    require_read()
    start, end = _upcoming_window(days)
    raw = await calendar_request(
        "list",
        {"timeMin": start, "timeMax": end, "maxResults": limit},
        credential().access_token or "",
    )
    return _normalize(raw, limit)


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="calendar")
async def event(event_id: str) -> dict[str, Any]:
    """Return one normalized Calendar event."""
    return await get_event(event_id)


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="calendar")
async def search(
    start: str,
    end: str,
    query: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search Calendar events by date and optional title/description text."""
    query = text(query, "query") if query is not None else ""
    limit = bounded_limit(limit, maximum=50)
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise GoogleValidationError("start and end must be ISO-8601 timestamps") from None
    return await search_events(query, start_dt, end_dt, limit)


__all__ = ["today", "upcoming", "event", "search"]

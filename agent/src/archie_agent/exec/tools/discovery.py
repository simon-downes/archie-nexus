"""Metadata-only Google Workspace discovery."""

from __future__ import annotations

from typing import Any

from archie_agent.exec.tools import tool
from archie_agent.exec.tools.google_common import (
    GoogleConfigurationError,
    GoogleTransportError,
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

_TYPES = ("email", "event", "file", "meeting")
_HANDOFFS = {
    "email": "mail.read",
    "event": "calendar.event",
    "file": "drive.metadata",
    "meeting": "calendar.event",
}


def _types(value: list[str] | None) -> list[str]:
    if value is None:
        return ["email", "event", "file"]
    if not isinstance(value, list) or not value or len(set(value)) != len(value):
        raise GoogleValidationError("types must be a non-empty list of unique values")
    if any(not isinstance(item, str) or item not in _TYPES for item in value):
        raise GoogleValidationError("types contains an unsupported value")
    return value.copy()


def _reference(item: Any, kind: str) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    try:
        identifier = safe_id(item.get("id"))
    except GoogleValidationError:
        return None
    title = clean(item.get("title") or item.get("name"), 256)
    if title is None:
        return None
    source = clean(item.get("source") or kind, 64) or kind
    return {
        "type": kind,
        "id": identifier,
        "title": title,
        "summary": clean(item.get("summary") or item.get("snippet"), 500),
        "source": source,
        "timestamp": clean(item.get("timestamp"), 64),
        "url": safe_url(item.get("url")),
        "provenance": clean(item.get("provenance") or source, 256),
        "handoff": _HANDOFFS.get(kind),
    }


async def search_sources(
    query: str, types: list[str], limit: int, token: str
) -> list[dict[str, Any]]:
    """Fetch bounded metadata from the selected Google APIs."""
    results: list[dict[str, Any]] = []
    if "email" in types:
        data = await google_json(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            token,
            params={"q": query, "maxResults": min(limit, 50)},
        )
        results.extend(
            {"type": "email", "id": item.get("id"), "title": "Gmail message", "source": "gmail"}
            for item in data.get("messages", [])
            if isinstance(item, dict)
        )
    if "event" in types or "meeting" in types:
        data = await google_json(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            token,
            params={"q": query, "maxResults": min(limit, 50)},
        )
        for item in data.get("items", []):
            if not isinstance(item, dict):
                continue
            kind = "meeting" if "meeting" in types and item.get("conferenceData") else "event"
            if kind not in types:
                continue
            results.append(
                {
                    "type": kind,
                    "id": item.get("id"),
                    "title": item.get("summary"),
                    "source": "calendar",
                    "timestamp": item.get("start", {}).get("dateTime")
                    if isinstance(item.get("start"), dict)
                    else None,
                }
            )
    if "file" in types:
        data = await google_json(
            "https://www.googleapis.com/drive/v3/files",
            token,
            params={
                "q": f"fullText contains '{query}'",
                "pageSize": min(limit, 50),
                "fields": "files(id,name,mimeType,modifiedTime,webViewLink)",
            },
        )
        results.extend(
            {
                "type": "file",
                "id": item.get("id"),
                "title": item.get("name"),
                "source": "drive",
                "timestamp": item.get("modifiedTime"),
                "url": item.get("webViewLink"),
            }
            for item in data.get("files", [])
            if isinstance(item, dict)
        )
    return results[: limit * 2]


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="google")
async def search(
    query: str, types: list[str] | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """Search bounded metadata references across Google Workspace services."""
    query = text(query, "query")
    selected = _types(types)
    limit = bounded_limit(limit, maximum=20)
    require_read()
    token = credential().access_token
    try:
        raw = await search_sources(query, selected, min(limit, 50), token or "")
    except GoogleConfigurationError:
        raise
    except Exception as exc:
        raise GoogleTransportError("Google discovery request failed") from exc
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw if isinstance(raw, list) else []:
        kind = item.get("type") if isinstance(item, dict) else None
        if kind not in selected:
            continue
        normalized = _reference(item, kind)
        if normalized is None:
            continue
        key = (kind, normalized["id"])
        if key in seen:
            continue
        seen.add(key)
        results.append(normalized)
        if len(results) >= limit:
            break
    return results


__all__ = ["search"]

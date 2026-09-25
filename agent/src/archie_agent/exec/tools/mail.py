"""Bounded Gmail read and narrow label/archive tools."""

from __future__ import annotations

import json
from typing import Any

from archie_agent.exec.tools import tool
from archie_agent.exec.tools.cache import cache
from archie_agent.exec.tools.google_common import (
    GoogleTransportError,
    GoogleValidationError,
    bounded_limit,
    clean,
    credential,
    google_json,
    require_read,
    require_write,
    safe_id,
    text,
)


def _valid_cached_labels(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "name", "type"}:
            return False
        try:
            identifier = safe_id(item["id"], "label_id")
            name = text(item["name"], "label_name", maximum=256)
        except GoogleValidationError:
            return False
        if item["type"] not in {"SYSTEM", "USER"} or identifier in seen_ids or name in seen_names:
            return False
        seen_ids.add(identifier)
        seen_names.add(name)
    return True


async def gmail_request(operation: str, payload: dict[str, Any], token: str) -> Any:
    if operation == "search":
        data = await google_json(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages", token, params=payload
        )
        return data.get("messages", [])
    if operation == "read":
        return await google_json(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{payload['id']}",
            token,
            params={"format": "full"},
        )
    if operation == "labels":
        data = await google_json("https://gmail.googleapis.com/gmail/v1/users/me/labels", token)
        return data.get("labels", [])
    if operation == "batch_modify":
        result = await google_json(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/batchModify",
            token,
            method="POST",
            json=payload,
            allow_empty=True,
        )
        if result not in (None, {}):
            raise GoogleTransportError("Gmail batch mutation returned an unexpected response")
        return None
    raise GoogleValidationError("Unsupported Gmail operation")


def _header(payload: dict[str, Any], name: str) -> str | None:
    headers = payload.get("headers", [])
    if isinstance(headers, list):
        for header in headers:
            if isinstance(header, dict) and header.get("name", "").lower() == name.lower():
                return clean(header.get("value"), 500)
    return None


def _body(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    mime = payload.get("mimeType")
    data = payload.get("body", {}).get("data") if isinstance(payload.get("body"), dict) else None
    if isinstance(data, str) and (mime == "text/plain" or mime == "text/html"):
        import base64

        try:
            return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
                "utf-8", "replace"
            )[:30000]
        except (ValueError, UnicodeError):
            pass
    parts = payload.get("parts", [])
    if isinstance(parts, list):
        for part in parts:
            value = _body(part)
            if value:
                return value
    return None


def _message(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        message_id = safe_id(raw.get("id"), "message_id")
    except GoogleValidationError:
        return None
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    return {
        "id": message_id,
        "thread_id": clean(raw.get("threadId"), 256),
        "subject": clean(raw.get("subject"), 500) or _header(payload, "Subject"),
        "from": clean(raw.get("from"), 500) or _header(payload, "From"),
        "to": clean(raw.get("to"), 500) or _header(payload, "To"),
        "date": clean(raw.get("date"), 64) or _header(payload, "Date"),
        "snippet": clean(raw.get("snippet"), 1000),
        "body": clean(raw.get("body"), 30000) or _body(payload),
        "label_ids": [x for x in raw.get("labelIds", []) if isinstance(x, str)][:100],
    }


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="mail")
async def search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Search bounded Gmail message metadata and body text."""
    query = text(query, "query")
    limit = bounded_limit(limit, maximum=100)
    require_read()
    raw = await gmail_request(
        "search", {"q": query, "maxResults": limit}, credential().access_token or ""
    )
    return [
        item
        for item in (_message(value) for value in (raw if isinstance(raw, list) else []))
        if item
    ][:limit]


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="mail")
async def read(message_id: str) -> dict[str, Any]:
    """Read one bounded Gmail message."""
    message_id = safe_id(message_id, "message_id")
    require_read()
    result = _message(
        await gmail_request("read", {"id": message_id}, credential().access_token or "")
    )
    if result is None:
        raise ValueError("Gmail message was not found")
    return result


async def _load_labels() -> list[dict[str, str]]:
    cached = cache.get("google.labels")
    if cached is not None and _valid_cached_labels(cached.get("labels")):
        return cached["labels"]
    require_read()
    raw = await gmail_request("labels", {}, credential().access_token or "")
    labels = [
        {
            "id": safe_id(item["id"], "label_id"),
            "name": text(item["name"], "label_name", maximum=256),
            "type": item.get("type") if item.get("type") in {"SYSTEM", "USER"} else "USER",
        }
        for item in (raw if isinstance(raw, list) else [])
        if isinstance(item, dict) and item.get("id") and item.get("name")
    ]
    cache.set("google.labels", {"labels": labels}, 24 * 60 * 60)
    return labels


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="mail")
async def list_labels() -> list[dict[str, str]]:
    """List normalized Gmail label names and types without exposing IDs."""
    return [{"name": item["name"], "type": item["type"]} for item in await _load_labels()]


_MAX_MUTATION_IDS = 500
_MAX_LABELS = 50
_MAX_BATCH_BYTES = 64 * 1024


def _message_ids(value: Any) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_MUTATION_IDS:
        raise GoogleValidationError("message_ids must contain 1-500 IDs")
    if any(not isinstance(item, str) for item in value):
        raise GoogleValidationError("message_ids must contain strings")
    result = [safe_id(item, "message_id") for item in value]
    if len(set(result)) != len(result):
        raise GoogleValidationError("message_ids must be duplicate-free")
    return result


def _label_names(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_LABELS:
        raise GoogleValidationError(f"{name} must contain 0-50 names")
    result = [text(item, "label_name", maximum=256) for item in value]
    if len(set(result)) != len(result):
        raise GoogleValidationError(f"{name} must be duplicate-free")
    return result


async def _batch_modify(
    message_ids: list[str], *, add: list[str], remove: list[str]
) -> dict[str, Any]:
    payload = {"ids": message_ids, "addLabelIds": add, "removeLabelIds": remove}
    encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > _MAX_BATCH_BYTES:
        raise GoogleValidationError("Gmail mutation request is too large")
    require_write()
    await gmail_request("batch_modify", payload, credential().access_token or "")
    return {"count": len(message_ids), "complete": True}


def _resolve_user_labels(names: list[str], labels: list[dict[str, str]]) -> list[str]:
    by_name = {item["name"]: item for item in labels if item["type"] == "USER"}
    system_names = {item["name"] for item in labels if item["type"] == "SYSTEM"}
    if any(value in system_names for value in names):
        raise GoogleValidationError("System labels require a dedicated mail operation")
    if any(value not in by_name for value in names):
        raise GoogleValidationError("Unknown Gmail user label")
    return [by_name[value]["id"] for value in names]


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="mail")
async def label(
    message_ids: list[str], add: list[str] | None = None, remove: list[str] | None = None
) -> dict[str, Any]:
    """Apply or remove USER labels on up to 500 Gmail messages."""
    ids = _message_ids(message_ids)
    additions = _label_names(add, "add")
    removals = _label_names(remove, "remove")
    if not additions and not removals:
        raise GoogleValidationError("label requires add or remove names")
    labels = await _load_labels()
    resolved_add = _resolve_user_labels(additions, labels)
    resolved_remove = _resolve_user_labels(removals, labels)
    if set(resolved_add) & set(resolved_remove):
        raise GoogleValidationError("Gmail label cannot be added and removed")
    return await _batch_modify(ids, add=resolved_add, remove=resolved_remove)


async def _system(
    message_ids: list[str], *, add: str | None = None, remove: str | None = None
) -> dict[str, Any]:
    ids = _message_ids(message_ids)
    return await _batch_modify(ids, add=[add] if add else [], remove=[remove] if remove else [])


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="mail")
async def archive(message_ids: list[str]) -> dict[str, Any]:
    """Archive up to 500 Gmail messages."""
    return await _system(message_ids, remove="INBOX")


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="mail")
async def unarchive(message_ids: list[str]) -> dict[str, Any]:
    """Unarchive up to 500 Gmail messages."""
    return await _system(message_ids, add="INBOX")


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="mail")
async def mark_read(message_ids: list[str]) -> dict[str, Any]:
    """Mark up to 500 Gmail messages read."""
    return await _system(message_ids, remove="UNREAD")


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="mail")
async def mark_unread(message_ids: list[str]) -> dict[str, Any]:
    """Mark up to 500 Gmail messages unread."""
    return await _system(message_ids, add="UNREAD")


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="mail")
async def star(message_ids: list[str]) -> dict[str, Any]:
    """Star up to 500 Gmail messages."""
    return await _system(message_ids, add="STARRED")


@tool(exec_enabled=True, native=False, exec_docs=False, namespace="mail")
async def unstar(message_ids: list[str]) -> dict[str, Any]:
    """Unstar up to 500 Gmail messages."""
    return await _system(message_ids, remove="STARRED")


__all__ = [
    "search",
    "read",
    "list_labels",
    "label",
    "archive",
    "unarchive",
    "mark_read",
    "mark_unread",
    "star",
    "unstar",
]

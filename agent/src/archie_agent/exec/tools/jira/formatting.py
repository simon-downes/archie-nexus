"""Provider-to-model Jira formatting helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

_MAX_TEXT = 8000


def text(value: Any, limit: int = _MAX_TEXT) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value[:limit]
    return str(value)[:limit]


def adf_text(value: Any) -> str:
    if isinstance(value, str):
        return value[:_MAX_TEXT]
    if isinstance(value, dict):
        return " ".join(adf_text(v) for k, v in value.items() if k in {"text", "content", "body"})[
            :_MAX_TEXT
        ]
    if isinstance(value, list):
        return " ".join(adf_text(v) for v in value)[:_MAX_TEXT]
    return ""


def gmt(value: Any) -> str | None:
    if not value:
        return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def issue_fields(item: dict[str, Any]) -> dict[str, Any]:
    fields = item.get("fields") or {}
    status = fields.get("status") or {}
    assignee = fields.get("assignee") or {}
    parent = fields.get("parent") or {}
    parent_fields = parent.get("fields") or {}
    priority = fields.get("priority") or {}
    issue_type = fields.get("issuetype") or {}
    return {
        "key": text(item.get("key")),
        "summary": text(fields.get("summary")),
        "status": text(status.get("name")),
        "assignee": text(assignee.get("displayName")),
        "parent": (
            {"key": text(parent.get("key")), "summary": text(parent_fields.get("summary"))}
            if parent.get("key")
            else None
        ),
        "priority": text(priority.get("name")),
        "type": text(issue_type.get("name")),
        "labels": [text(label) for label in fields.get("labels", []) if text(label) is not None],
        "created": gmt(fields.get("created")),
        "updated": gmt(fields.get("updated")),
    }


def comment(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": text(item.get("id")),
        "author": text((item.get("author") or {}).get("displayName")),
        "body": adf_text(item.get("body")),
        "created": gmt(item.get("created")),
    }


def attachment(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "filename": text(item.get("filename"), 512),
        "size": item.get("size"),
        "content_url": text(item.get("content"), 2000),
    }

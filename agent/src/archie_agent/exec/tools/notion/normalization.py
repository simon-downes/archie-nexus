"""Normalization of Notion MCP response envelopes."""

from __future__ import annotations

import json
import re
from typing import Any

_ID_RE = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}"
_PARENT_RE = re.compile(rf'<(?:parent-page|ancestor-\d+-page)\s+url="[^"]*?({_ID_RE})"')
_PROPERTIES_RE = re.compile(r"<properties>\s*(\{.*?\})\s*</properties>", re.DOTALL)
_CONTENT_RE = re.compile(r"<content>\s*(.*?)\s*</content>", re.DOTALL)
_COMMENT_RE = re.compile(
    r'<comment\s+id="([^"]+)"\s+url="([^"]+)"\s+user-url="([^"]+)"\s+datetime="([^"]+)">(.*?)</comment>',
    re.DOTALL,
)


def _canonical_id(value: str) -> str:
    compact = value.replace("-", "").lower()
    return f"{compact[:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:]}"


def normalize_fetch(value: Any, requested_id: str) -> dict[str, Any] | None:
    """Convert the current Notion fetch envelope into the tool projection shape."""
    if not isinstance(value, dict) or not isinstance(value.get("metadata"), dict):
        return value if isinstance(value, dict) and value.get("id") else None
    resource_type = str(value["metadata"].get("type", ""))
    if resource_type not in {"page", "database", "data_source", "database_item"}:
        return None
    text = value.get("text", "")
    if not isinstance(text, str):
        text = ""
    result: dict[str, Any] = {
        "id": _canonical_id(requested_id),
        "type": resource_type,
        "title": value.get("title"),
        "url": value.get("url"),
        "path": value.get("path"),
        "last_edited_time": value.get("page_last_edited_at"),
        "ancestors": [_canonical_id(item) for item in _PARENT_RE.findall(text)],
    }
    properties_match = _PROPERTIES_RE.search(text)
    if properties_match:
        try:
            properties = json.loads(properties_match.group(1))
        except json.JSONDecodeError:
            properties = None
        if isinstance(properties, dict):
            result["properties"] = properties
    content_match = _CONTENT_RE.search(text)
    if content_match:
        result["content"] = content_match.group(1).strip()[:8000]
    return result


def normalize_comments(value: Any) -> list[dict[str, Any]]:
    """Normalize comment responses, including XML discussion envelopes."""
    if isinstance(value, dict) and "results" in value:
        value = value["results"]
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return [
            {"id": comment_id, "url": url, "user_url": user_url, "created": created, "body": body.strip()[:8000]}
            for comment_id, url, user_url, created, body in _COMMENT_RE.findall(value["text"])
        ][:50]
    if isinstance(value, dict) and set(value) <= {"suggested_edits_status"}:
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)][:50]

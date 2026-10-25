"""Normalization of Notion MCP response envelopes."""

from __future__ import annotations

import html
import json
import re
from typing import Any

_ID_RE = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}"
_PARENT_RE = re.compile(rf'<(?:parent-page|ancestor-\d+-page)\s+url="[^"]*?({_ID_RE})"')
_DATA_SOURCE_RE = re.compile(
    r'<(?:data-source|collection)\b[^>]*?url="(?:\{\{)?(collection://[^"}]+)(?:\}\})?"'
)
_VIEW_RE = re.compile(r'<view\b[^>]*?url="(?:\{\{)?(view://[^"}]+)(?:\}\})?"')
_PROPERTIES_RE = re.compile(r"<properties>\s*(\{.*?\})\s*</properties>", re.DOTALL)
_CONTENT_RE = re.compile(r"<content>\s*(.*?)\s*</content>", re.DOTALL)
_STATE_RE = re.compile(r"<data-source-state>\s*(\{.*?\})\s*</data-source-state>", re.DOTALL)
# Fetch reports status groups by key; filters take Notion's display label (case-insensitive).
_STATUS_GROUPS = {"to_do": "To-do", "in_progress": "In progress", "complete": "Complete"}
_MAX_PROPERTIES = 200
_MAX_OPTIONS = 100
# Attributes are matched by name, not position: the provider adds attributes such as
# by-you="true" (tests/fixtures/get_comments_response.json).
_COMMENT_RE = re.compile(r"<comment\b([^>]*)>(.*?)</comment>", re.DOTALL)
_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
_MAX_COMMENTS = 50


def _canonical_id(value: str) -> str:
    compact = value.replace("-", "").lower()
    return f"{compact[:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:]}"


def _option_names(options: Any) -> list[str]:
    if not isinstance(options, list):
        return []
    names = [item.get("name") for item in options if isinstance(item, dict)]
    return [name for name in names if isinstance(name, str)][:_MAX_OPTIONS]


def _project_property(spec: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"type": spec["type"]}
    if "options" in spec:
        result["options"] = _option_names(spec["options"])
    groups = spec.get("groups")
    if isinstance(groups, dict):
        result["options"] = [name for value in groups.values() for name in _option_names(value)][
            :_MAX_OPTIONS
        ]
        result["groups"] = {
            label: _option_names(groups[key])
            for key, label in _STATUS_GROUPS.items()
            if key in groups
        }
    return result


def _parse_schemas(text: str) -> dict[str, dict[str, Any]]:
    """Extract each data source's property names, types, and options from fetch text."""
    schemas: dict[str, dict[str, Any]] = {}
    for raw in _STATE_RE.findall(text):
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if (
            not isinstance(state, dict)
            or not isinstance(state.get("url"), str)
            or not isinstance(state.get("schema"), dict)
        ):
            continue
        properties = {
            name: _project_property(spec)
            for name, spec in list(state["schema"].items())[:_MAX_PROPERTIES]
            if isinstance(name, str)
            and isinstance(spec, dict)
            and isinstance(spec.get("type"), str)
        }
        schemas[state["url"]] = {"name": state.get("name"), "properties": properties}
    return schemas


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
        "data_sources": _DATA_SOURCE_RE.findall(text),
        "views": [{"url": url} for url in _VIEW_RE.findall(text)],
    }
    schemas = _parse_schemas(text)
    if schemas:
        result["schemas"] = schemas
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
        result["content"] = content_match.group(1).strip()
    return result


def _parse_comments(text: str) -> list[dict[str, Any]]:
    """Project every <comment> in a <discussions> document, across all discussion threads."""
    comments = []
    for raw_attrs, body in _COMMENT_RE.findall(text):
        attrs = {name: html.unescape(value) for name, value in _ATTR_RE.findall(raw_attrs)}
        if not attrs.get("id"):
            continue
        comments.append(
            {
                "id": attrs["id"],
                "url": attrs.get("url"),
                "user_url": attrs.get("user-url"),
                "created": attrs.get("datetime"),
                "body": html.unescape(body.strip()),
            }
        )
    return comments[:_MAX_COMMENTS]


def normalize_comments(value: Any) -> list[dict[str, Any]]:
    """Normalize comment responses, including XML discussion envelopes."""
    if isinstance(value, dict) and "results" in value:
        value = value["results"]
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return _parse_comments(value["text"])
    if isinstance(value, dict) and set(value) <= {"suggested_edits_status"}:
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)][:_MAX_COMMENTS]

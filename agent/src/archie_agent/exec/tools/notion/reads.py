"""Public Notion page and database read tools."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from archie_agent.exec.tools import ToolError, tool

from .client import notion_call
from .normalization import normalize_fetch
from .policy import NotionPolicyError, NotionValidationError, in_scope, normalize_id, require_read


class NotionResponseError(ToolError):
    pass


class NotionNotFoundError(ToolError):
    pass


def resource_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NotionValidationError("Notion resource ID or URL is required")
    value = value.strip()
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        if parsed.netloc not in {"notion.so", "www.notion.so"} and not parsed.netloc.endswith(
            ".notion.site"
        ):
            raise NotionValidationError("Unsupported Notion URL")
        match = re.search(
            r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32})$", parsed.path
        )
        if not match:
            raise NotionValidationError("Unsupported Notion URL")
        value = match.group(1)
    return normalize_id(value)


def _text(value: Any, limit: int = 8000) -> str:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return " ".join(_text(item, limit) for item in value)[:limit]
    if isinstance(value, dict):
        return " ".join(_text(item, limit) for item in value.values())[:limit]
    return ""


def _ancestors(value: dict[str, Any]) -> list[str]:
    raw = value.get("ancestors", value.get("path", []))
    if isinstance(raw, str):
        return [part for part in raw.split("/") if part]
    return [item for item in raw if isinstance(item, str)] if isinstance(raw, list) else []


def _authorize(
    value: dict[str, Any], identifier: str, policy: dict[str, Any], expected: str
) -> None:
    kind = str(value.get("type", expected))
    if not in_scope(identifier, kind, _ancestors(value), policy["scope"]):
        raise NotionPolicyError("Notion resource is outside the configured read scope")


def _project(value: dict[str, Any], *, include_properties: bool = False) -> dict[str, Any]:
    result = {
        key: value[key]
        for key in ("id", "type", "title", "url", "path", "last_edited_time", "content")
        if key in value
    }
    if include_properties and isinstance(value.get("properties"), dict):
        result["properties"] = dict(list(value["properties"].items())[:50])
    return result


@tool(native=False, exec_docs=False, namespace="notion")
async def get_page(id_or_url: str, *, include_properties: bool = False) -> dict[str, Any]:
    """Fetch one policy-authorized Notion page."""
    identifier = resource_id(id_or_url)
    policy = require_read()
    raw = normalize_fetch(await notion_call("notion-fetch", {"id": identifier}), identifier)
    if not raw or not raw.get("id"):
        raise NotionResponseError("Notion returned malformed page data")
    if str(raw.get("type", "page")) not in {"page", "database_item"}:
        raise NotionResponseError("Notion resource is not a page")
    _authorize(raw, identifier, policy, "page")
    return _project(raw, include_properties=include_properties)


@tool(native=False, exec_docs=False, namespace="notion")
async def get_database(id_or_url: str, *, include_views: bool = True) -> dict[str, Any]:
    """Fetch one policy-authorized Notion database or data source."""
    identifier = resource_id(id_or_url)
    policy = require_read()
    raw = normalize_fetch(await notion_call("notion-fetch", {"id": identifier}), identifier)
    if not raw or not raw.get("id"):
        raise NotionResponseError("Notion returned malformed database data")
    if str(raw.get("type", "database")) not in {"database", "data_source"}:
        raise NotionResponseError("Notion resource is not a database")
    _authorize(raw, identifier, policy, "database")
    result = _project(raw)
    if include_views and isinstance(raw.get("views"), list):
        result["views"] = raw["views"][:50]
    return result

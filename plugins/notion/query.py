"""Public Notion database query and comment tools."""

from __future__ import annotations

from typing import Any

from archie_agent.exec.plugin import ToolError, tool

from .client import notion_call
from .normalization import normalize_comments, normalize_fetch
from .policy import NotionValidationError, require_read
from .reads import resource_id


class NotionQueryError(ToolError):
    pass


def _validate_filter(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) > 20:
        raise NotionValidationError("filter must be a bounded structured filter")
    return value


def _validate_sort(value: list[dict[str, str]] | None) -> list[dict[str, str]] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 20:
        raise NotionValidationError("sort must contain at most 20 entries")
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"property", "direction"}
            or not isinstance(item["property"], str)
            or not item["property"].strip()
            or item["direction"] not in {"ascending", "descending"}
        ):
            raise NotionValidationError(
                "sort entries require property and ascending or descending direction"
            )
    return [
        {"property": item["property"].strip(), "direction": item["direction"]} for item in value
    ]


@tool(namespace="notion")
async def query_database(
    id_or_url: str,
    *,
    view: str | None = None,
    data_source: str | None = None,
    filter: dict[str, Any] | None = None,
    sort: list[dict[str, str]] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Query a bounded Notion database view."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise NotionValidationError("limit must be 1-100")
    database_id = resource_id(id_or_url)
    policy = require_read()
    # Fetching the database establishes scope before any row query.
    schema = normalize_fetch(await notion_call("notion-fetch", {"id": database_id}), database_id)
    if not schema or not schema.get("id"):
        raise NotionQueryError("Notion returned malformed database data")
    from .reads import _authorize

    _authorize(schema, database_id, policy, "database")
    if data_source is not None and (not isinstance(data_source, str) or not data_source.strip()):
        raise NotionValidationError("data_source must be a non-empty data-source URL")
    if view is not None:
        if not isinstance(view, str) or not view.strip() or len(view) > 200:
            raise NotionValidationError("view must be 1-200 characters")
        views = [item.get("url") for item in schema.get("views", []) if isinstance(item, dict)]
        if view.strip() not in views:
            raise NotionValidationError("view is not part of this database")
        args: dict[str, Any] = {
            "data": {"mode": "view", "view_url": view.strip(), "page_size": limit}
        }
    else:
        data_sources = schema.get("data_sources", [])
        if not isinstance(data_sources, list) or not data_sources:
            raise NotionQueryError("Notion database has no queryable data source")
        source = data_source.strip() if data_source else data_sources[0]
        if source not in data_sources:
            raise NotionValidationError("data_source is not part of this database")
        data: dict[str, Any] = {"mode": "rows", "data_source_url": source, "limit": limit}
        validated_filter = _validate_filter(filter)
        validated_sort = _validate_sort(sort)
        if validated_filter is not None:
            data["filter"] = validated_filter
        if validated_sort is not None:
            data["sort"] = validated_sort
        args = {"data": data}
    raw = await notion_call("notion-query-data-sources", args)
    rows = raw.get("results", raw) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise NotionQueryError("Notion returned malformed database rows")
    return [row for row in rows[:limit] if isinstance(row, dict)]


@tool(namespace="notion")
async def get_comments(id_or_url: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Fetch bounded comments for a policy-authorized page."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise NotionValidationError("limit must be 1-50")
    page_id = resource_id(id_or_url)
    policy = require_read()
    page = normalize_fetch(await notion_call("notion-fetch", {"id": page_id}), page_id)
    if not page or not page.get("id"):
        raise NotionQueryError("Notion returned malformed page data")
    from .reads import _authorize

    _authorize(page, page_id, policy, "page")
    raw = await notion_call("notion-get-comments", {"page_id": page_id})
    comments = normalize_comments(raw)
    return comments[:limit]

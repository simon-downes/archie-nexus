"""Public Notion database query and comment tools."""

from __future__ import annotations

from typing import Any

from archie_agent.exec.tools import ToolError, tool

from .client import notion_call
from .normalization import normalize_comments, normalize_fetch
from .policy import NotionValidationError, require_read
from .reads import resource_id


class NotionQueryError(ToolError):
    pass


def _validate_filters(filters: list[str] | None) -> list[str]:
    if filters is None:
        return []
    if (
        not isinstance(filters, list)
        or len(filters) > 20
        or any(not isinstance(item, str) or not item.strip() or len(item) > 200 for item in filters)
    ):
        raise NotionValidationError("filters must contain at most 20 bounded expressions")
    return [item.strip() for item in filters]


@tool(native=False, exec_docs=False, namespace="notion")
async def query_database(
    id_or_url: str,
    *,
    view: str | None = None,
    filters: list[str] | None = None,
    sort: str | None = None,
    columns: list[str] | None = None,
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
    args: dict[str, Any] = {"data_source_url": database_id, "mode": "rows", "limit": limit}
    if view:
        if not isinstance(view, str) or not view.strip() or len(view) > 200:
            raise NotionValidationError("view must be 1-200 characters")
        args["view"] = view.strip()
    args["filters"] = _validate_filters(filters)
    if sort is not None:
        if not isinstance(sort, str) or not sort.strip() or len(sort) > 200:
            raise NotionValidationError("sort must be a bounded expression")
        args["sort"] = sort.strip()
    if columns is not None:
        if (
            not isinstance(columns, list)
            or len(columns) > 50
            or any(not isinstance(item, str) or not item.strip() for item in columns)
        ):
            raise NotionValidationError("columns must contain at most 50 names")
        args["columns"] = columns
    raw = await notion_call("notion-query-data-sources", args)
    rows = raw.get("results", raw) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise NotionQueryError("Notion returned malformed database rows")
    return [row for row in rows[:limit] if isinstance(row, dict)]


@tool(native=False, exec_docs=False, namespace="notion")
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

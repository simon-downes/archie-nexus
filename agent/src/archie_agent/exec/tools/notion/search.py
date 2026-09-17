"""Public Notion search tool."""

from __future__ import annotations

from typing import Any

from archie_agent.exec.tools import ToolError, tool

from .policy import NotionValidationError, in_scope, require_read

_MAX_LIMIT = 50


class NotionResponseError(ToolError):
    pass


@tool(native=False, exec_docs=False, namespace="notion")
async def search(
    query: str, *, limit: int = 10, resource_type: str | None = None
) -> list[dict[str, Any]]:
    """Search Notion and return a bounded, policy-filtered result projection."""
    if not isinstance(query, str) or not query.strip() or len(query) > 200:
        raise NotionValidationError("query must be 1-200 characters")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_LIMIT:
        raise NotionValidationError("limit must be 1-50")
    if resource_type not in (None, "page", "database"):
        raise NotionValidationError("resource_type must be page or database")
    policy = require_read()
    from .client import notion_call

    raw = await notion_call(
        "notion-search",
        {"query": query.strip(), "filters": ({"type": resource_type} if resource_type else {})},
    )
    values = raw.get("results", raw) if isinstance(raw, dict) else raw
    if not isinstance(values, list):
        raise NotionResponseError("Notion returned malformed search results")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            continue
        identifier = value["id"].replace("-", "").lower()
        if identifier in seen:
            continue
        seen.add(identifier)
        if policy["scope"] is not None:
            try:
                allowed = in_scope(
                    identifier,
                    str(value.get("type", "page")),
                    value.get("ancestors", []),
                    policy["scope"],
                )
            except NotionValidationError:
                allowed = False
            if not allowed:
                continue
        results.append(
            {
                k: value[k]
                for k in ("id", "type", "title", "url", "path", "last_edited_time")
                if k in value
            }
        )
        if len(results) >= limit:
            break
    return results

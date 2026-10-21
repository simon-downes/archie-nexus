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
    # Only the outer group is checked locally; the server validates the leaf
    # filters against each property type and returns its message on rejection.
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or value.get("type") != "group"
        or value.get("operator") not in {"and", "or"}
        or not isinstance(value.get("filters"), list)
        or not 1 <= len(value["filters"]) <= 100
    ):
        raise NotionValidationError(
            'filter must be a group: {"type": "group", "operator": "and" or "or", '
            '"filters": [1-100 property filters or nested groups]}'
        )
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
    return [{"property": item["property"], "direction": item["direction"]} for item in value]


def _names(values: Any, limit: int = 30) -> str:
    items = [repr(value) for value in list(values)[:limit]]
    return ", ".join(items) + (", ..." if len(values) > limit else "")


def _option_values(value: Any) -> list[str] | None:
    """Return the literal values of a leaf's value object or array, or None if unparseable."""
    entries = value if isinstance(value, list) else [value]
    values = [entry.get("value") for entry in entries if isinstance(entry, dict)]
    if len(values) != len(entries) or not all(isinstance(item, str) for item in values):
        return None
    return values


def _check_options(leaf: dict[str, Any], spec: dict[str, Any]) -> None:
    # The provider matches unknown option values silently (0 rows, or all rows for a
    # status group), so known option lists are checked here.
    name = leaf["property"]
    value = leaf.get("value")
    entries = value if isinstance(value, list) else [value]
    if spec["type"] in {"select", "multi_select"}:
        options = spec.get("options") or []
        values = _option_values(value)
        if values is None or len(options) >= 100:
            return
        for item in values:
            if item not in options:
                raise NotionValidationError(
                    f"{item!r} is not an option of {name!r}; options are {_names(options)} "
                    "(case-sensitive)"
                )
    elif spec["type"] == "status":
        options = {option.lower() for option in spec.get("options") or []}
        groups = {group.lower() for group in spec.get("groups") or {}}
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("value"), str):
                continue
            item = entry["value"].lower()
            if entry.get("type") == "is_option" and options and item not in options:
                raise NotionValidationError(
                    f"{entry['value']!r} is not an option of {name!r}; "
                    f"options are {_names(spec.get('options') or [])}"
                )
            if entry.get("type") == "is_group" and groups and item not in groups:
                raise NotionValidationError(
                    f"{entry['value']!r} is not a status group of {name!r}; "
                    f"groups are {_names(spec.get('groups') or {})}"
                )


def _check_property(name: Any, properties: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(name, str) or name not in properties:
        raise NotionValidationError(
            f"{name!r} is not a property of this data source; properties are "
            f"{_names(properties)} (names are exact, including spaces)"
        )
    return properties[name]


def _check_filter(node: dict[str, Any], properties: dict[str, dict[str, Any]], depth: int) -> None:
    for entry in node["filters"]:
        if not isinstance(entry, dict):
            raise NotionValidationError("filter entries must be objects")
        if entry.get("type") == "group":
            if depth >= 1:
                raise NotionValidationError("filter groups can nest only one level deep")
            _validate_filter(entry)
            _check_filter(entry, properties, depth + 1)
            continue
        if entry.get("type") != "property":
            raise NotionValidationError('filter entries need "type": "property" or "group"')
        spec = _check_property(entry.get("property"), properties)
        if entry.get("propertyType") != spec["type"]:
            raise NotionValidationError(
                f"{entry['property']!r} has propertyType {spec['type']!r}, "
                f"not {entry.get('propertyType')!r}"
            )
        _check_options(entry, spec)


def _source_properties(schema: dict[str, Any], source: str) -> dict[str, dict[str, Any]]:
    schemas = schema.get("schemas")
    entry = schemas.get(source) if isinstance(schemas, dict) else None
    properties = entry.get("properties") if isinstance(entry, dict) else None
    if not isinstance(properties, dict) or not properties:
        raise NotionQueryError(
            "Notion did not return this data source's schema, so filter and sort "
            "cannot be checked; query without them or use a saved view"
        )
    return properties


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
    """Query up to 100 rows; filter is a group of typed property filters, names/types from get_database (see notion skill)."""
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
        if filter is not None or sort is not None:
            raise NotionValidationError("filter and sort cannot be combined with view")
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
        if validated_filter is not None or validated_sort is not None:
            # The provider silently ignores unknown property names, so check them here.
            properties = _source_properties(schema, source)
            if validated_filter is not None:
                _check_filter(validated_filter, properties, 0)
            for item in validated_sort or []:
                _check_property(item["property"], properties)
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
    """Fetch up to 50 comments (page and block discussions, with replies) for an authorized page."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise NotionValidationError("limit must be 1-50")
    page_id = resource_id(id_or_url)
    policy = require_read()
    page = normalize_fetch(await notion_call("notion-fetch", {"id": page_id}), page_id)
    if not page or not page.get("id"):
        raise NotionQueryError("Notion returned malformed page data")
    from .reads import _authorize

    _authorize(page, page_id, policy, "page")
    # Page-level and block-anchored discussions, each with all its replies; resolved
    # discussions stay excluded (provider default).
    raw = await notion_call("notion-get-comments", {"page_id": page_id, "include_all_blocks": True})
    comments = normalize_comments(raw)
    return comments[:limit]

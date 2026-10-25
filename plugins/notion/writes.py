"""Public Notion mutation tools."""

from __future__ import annotations

from typing import Any

from archie_agent.exec.plugin import ToolError, tool

from .client import NotionConfigurationError, NotionToolError, notion_call
from .normalization import normalize_fetch
from .policy import NotionValidationError, in_scope, normalize_id, require_write
from .reads import NotionResponseError, resource_id

# Raised before or instead of a write: the provider rejected the input (NotionToolError,
# with its message) or no request was sent, so nothing was written. Anything else from
# the mutation call may follow a write and is reported as indeterminate.
_DEFINITE = (NotionToolError, NotionConfigurationError)
# notion-create-comment caps each rich_text item at 2000 characters.
_COMMENT_CHUNK = 2000


class NotionMutationIndeterminateError(ToolError):
    pass


class NotionWriteError(ToolError):
    pass


def _properties(properties: dict[str, str] | None) -> dict[str, str]:
    if properties is None:
        return {}
    if not isinstance(properties, dict) or len(properties) > 50:
        raise NotionValidationError("properties must contain at most 50 fields")
    if any(
        not isinstance(k, str) or not k.strip() or not isinstance(v, str) or len(v) > 2000
        for k, v in properties.items()
    ):
        raise NotionValidationError("properties must contain bounded string values")
    return {k.strip(): v for k, v in properties.items()}


def _replacements(value: Any) -> list[dict[str, Any]]:
    """Map {old, new, replace_all} items to the provider's content_updates entries."""
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise NotionValidationError("replacements must be a list of 1-20 items")
    updates = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) - {"old", "new", "replace_all"}
            or not isinstance(item.get("old"), str)
            or not item["old"]
            or not isinstance(item.get("new"), str)
            or len(item["old"]) > 8000
            or len(item["new"]) > 8000
            or not isinstance(item.get("replace_all", False), bool)
        ):
            raise NotionValidationError(
                "replacements items need non-empty string old, string new (each at most "
                "8000 characters) and optional bool replace_all"
            )
        update: dict[str, Any] = {"old_str": item["old"], "new_str": item["new"]}
        if item.get("replace_all"):
            update["replace_all_matches"] = True
        updates.append(update)
    return updates


def _content(value: str | None) -> str | None:
    if value is not None and (not isinstance(value, str) or len(value) > 8000):
        raise NotionValidationError("content must be at most 8000 characters")
    return value


_DATA_SOURCE_PREFIX = "collection://"


def _parent_target(value: str) -> tuple[str, bool]:
    """Return (normalized ID, is_data_source) for a page/database ID or URL or collection:// URL."""
    if isinstance(value, str) and value.strip().startswith(_DATA_SOURCE_PREFIX):
        return normalize_id(value.strip()[len(_DATA_SOURCE_PREFIX) :]), True
    return resource_id(value), False


def _uuid(value: str) -> str:
    return f"{value[:8]}-{value[8:12]}-{value[12:16]}-{value[16:20]}-{value[20:]}"


async def _require_target_scope(page_id: str, scope: dict[str, set[str]]) -> None:
    target = await _fetch(page_id, page_id)
    if not target or not in_scope(
        page_id, str(target.get("type", "page")), _ancestor_ids(target), scope
    ):
        raise NotionWriteError("Notion page is outside the configured write scope")


def _ancestor_ids(value: dict[str, Any]) -> list[str]:
    raw = value.get("ancestors", [])
    return [item for item in raw if isinstance(item, str)] if isinstance(raw, list) else []


async def _fetch(identifier: str, requested_id: str) -> dict[str, Any] | None:
    return normalize_fetch(await notion_call("notion-fetch", {"id": identifier}), requested_id)


def _title_property(parent: dict[str, Any], source: str) -> str | None:
    schemas = parent.get("schemas")
    entry = schemas.get(source) if isinstance(schemas, dict) else None
    properties = entry.get("properties") if isinstance(entry, dict) else None
    if not isinstance(properties, dict):
        return None
    names = [
        n
        for n, spec in properties.items()
        if isinstance(spec, dict) and spec.get("type") == "title"
    ]
    return names[0] if len(names) == 1 else None


async def _resolve_parent(
    parent_id: str, is_data_source: bool, scope: dict[str, set[str]] | None
) -> tuple[dict[str, str], str | None]:
    """Fetch the parent, enforce write scope, and return (provider parent, title property).

    A database parent is created through its single data source, as the provider requires;
    pages outside a database take only the "title" property.
    """
    fetch_id = f"{_DATA_SOURCE_PREFIX}{_uuid(parent_id)}" if is_data_source else parent_id
    parent = await _fetch(fetch_id, parent_id)
    if not parent or not parent.get("id"):
        raise NotionResponseError("Notion returned malformed parent data")
    kind = str(parent.get("type", "page"))
    if is_data_source and kind != "data_source":
        raise NotionValidationError("collection:// parent is not a Notion data source")
    ancestors = _ancestor_ids(parent)
    if kind == "data_source" and scope is not None:
        # A data-source fetch reports no ancestry; its URL is the owning database, whose
        # fetch carries the path used for scope.
        database_id = None
        try:
            database_id = resource_id(str(parent.get("url") or ""))
        except NotionValidationError:
            pass
        if database_id and database_id != parent_id:
            database = await _fetch(database_id, database_id)
            if isinstance(database, dict) and database.get("type") == "database":
                ancestors = [database_id, *_ancestor_ids(database)]
    if scope is not None and not in_scope(parent_id, kind, ancestors, scope):
        raise NotionWriteError("Notion parent is outside the configured write scope")
    if kind in {"page", "database_item"}:
        return {"page_id": parent_id}, "title"
    if kind not in {"database", "data_source"}:
        raise NotionResponseError("Notion parent is not a page or database")
    sources = parent.get("data_sources")
    sources = (
        [item for item in sources if isinstance(item, str)] if isinstance(sources, list) else []
    )
    if kind == "data_source":
        source = f"{_DATA_SOURCE_PREFIX}{_uuid(parent_id)}"
    elif len(sources) == 1:
        source = sources[0]
    else:
        raise NotionValidationError(
            "database has no single data source; pass one of its collection:// URLs from "
            f"get_database as the parent: {sources[:20]}"
        )
    return {"data_source_id": source[len(_DATA_SOURCE_PREFIX) :]}, _title_property(parent, source)


@tool(namespace="notion")
async def create_page(
    parent_id_or_url: str,
    *,
    title: str | None = None,
    properties: dict[str, str] | None = None,
    content: str | None = None,
) -> dict[str, Any]:
    """Create one page under a write-authorized page, or a row in a database (ID/URL or collection:// data source)."""
    parent_id, is_data_source = _parent_target(parent_id_or_url)
    policy = require_write()
    if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 500):
        raise NotionValidationError("title must be 1-500 characters")
    props = _properties(properties)
    body_content = _content(content)
    parent, title_property = await _resolve_parent(parent_id, is_data_source, policy["scope"])
    if title:
        if title_property is None:
            raise NotionResponseError(
                "Notion did not report this database's title property; set it in properties"
            )
        if title_property in props:
            raise NotionValidationError(
                f"title and properties[{title_property!r}] both set the page title"
            )
        props[title_property] = title.strip()
    page: dict[str, Any] = {"properties": props}
    if body_content is not None:
        page["content"] = body_content
    body = {"parent": parent, "pages": [page]}
    try:
        result = await notion_call("notion-create-pages", body)
    except _DEFINITE:
        raise
    except Exception as exc:
        raise NotionMutationIndeterminateError("Notion page creation outcome is uncertain") from exc
    if not isinstance(result, dict):
        raise NotionMutationIndeterminateError("Notion page creation outcome is uncertain")
    return result


@tool(namespace="notion")
async def update_page(
    id_or_url: str,
    *,
    properties: dict[str, str] | None = None,
    replacements: list[dict[str, str | bool]] | None = None,
) -> dict[str, Any]:
    """Update properties, or apply {old, new, replace_all} content replacements (not both)."""
    page_id = resource_id(id_or_url)
    policy = require_write()
    props = _properties(properties)
    if not props and not replacements:
        raise NotionValidationError("an update requires properties or replacements")
    if props and replacements:
        raise NotionValidationError("update properties and replacements in separate calls")
    body: dict[str, Any] = {"page_id": page_id}
    if props:
        body.update({"command": "update_properties", "properties": props})
    else:
        body.update({"command": "update_content", "content_updates": _replacements(replacements)})
    if policy["scope"] is not None:
        await _require_target_scope(page_id, policy["scope"])
    try:
        result = await notion_call("notion-update-page", body)
    except _DEFINITE:
        raise
    except Exception as exc:
        raise NotionMutationIndeterminateError("Notion page update outcome is uncertain") from exc
    if not isinstance(result, dict):
        raise NotionMutationIndeterminateError("Notion page update outcome is uncertain")
    return result


@tool(namespace="notion")
async def add_comment(id_or_url: str, body: str) -> dict[str, Any]:
    """Add a bounded comment to a write-authorized page."""
    page_id = resource_id(id_or_url)
    policy = require_write()
    if not isinstance(body, str) or not 1 <= len(body) <= 8000:
        raise NotionValidationError("body must be 1-8000 characters")
    if policy["scope"] is not None:
        await _require_target_scope(page_id, policy["scope"])
    try:
        result = await notion_call(
            "notion-create-comment",
            {
                "page_id": page_id,
                "rich_text": [
                    {"type": "text", "text": {"content": body[i : i + _COMMENT_CHUNK]}}
                    for i in range(0, len(body), _COMMENT_CHUNK)
                ],
            },
        )
    except _DEFINITE:
        raise
    except Exception as exc:
        raise NotionMutationIndeterminateError("Notion comment outcome is uncertain") from exc
    if not isinstance(result, dict):
        raise NotionMutationIndeterminateError("Notion comment outcome is uncertain")
    return result

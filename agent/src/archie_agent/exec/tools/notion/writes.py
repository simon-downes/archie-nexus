"""Public Notion mutation tools."""

from __future__ import annotations

from typing import Any

from archie_agent.exec.tools import ToolError, tool

from .client import notion_call
from .policy import NotionValidationError, in_scope, require_write
from .reads import resource_id


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


def _content(value: str | None) -> str | None:
    if value is not None and (not isinstance(value, str) or len(value) > 8000):
        raise NotionValidationError("content must be at most 8000 characters")
    return value


@tool(native=False, exec_docs=False, namespace="notion")
async def create_page(
    parent_id_or_url: str,
    *,
    title: str | None = None,
    properties: dict[str, str] | None = None,
    content: str | None = None,
) -> dict[str, Any]:
    """Create one bounded child page under a write-authorized parent."""
    parent_id = resource_id(parent_id_or_url)
    policy = require_write()
    if policy["scope"] is not None:
        parent = await notion_call("notion-fetch", {"id": parent_id})
        if not isinstance(parent, dict) or not in_scope(
            parent_id, str(parent.get("type", "page")), parent.get("ancestors", []), policy["scope"]
        ):
            raise NotionWriteError("Notion parent is outside the configured write scope")
    if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 500):
        raise NotionValidationError("title must be 1-500 characters")
    body: dict[str, Any] = {"parent": {"page_id": parent_id}, "properties": _properties(properties)}
    if title:
        body["properties"]["title"] = title.strip()
    if (body_content := _content(content)) is not None:
        body["content"] = body_content
    try:
        result = await notion_call("notion-create-pages", body)
    except Exception as exc:
        raise NotionMutationIndeterminateError("Notion page creation outcome is uncertain") from exc
    if not isinstance(result, dict):
        raise NotionMutationIndeterminateError("Notion page creation outcome is uncertain")
    return result


@tool(native=False, exec_docs=False, namespace="notion")
async def update_page(
    id_or_url: str,
    *,
    properties: dict[str, str] | None = None,
    replacements: list[dict[str, str | bool]] | None = None,
) -> dict[str, Any]:
    """Update bounded properties or deterministic content replacements."""
    page_id = resource_id(id_or_url)
    policy = require_write()
    if policy["scope"] is not None:
        target = await notion_call("notion-fetch", {"id": page_id})
        if not isinstance(target, dict) or not in_scope(
            page_id, str(target.get("type", "page")), target.get("ancestors", []), policy["scope"]
        ):
            raise NotionWriteError("Notion page is outside the configured write scope")
    props = _properties(properties)
    if not props and not replacements:
        raise NotionValidationError("an update requires properties or replacements")
    if replacements is not None:
        if len(replacements) > 20 or any(
            not isinstance(item, dict) or not isinstance(item.get("old"), str) or not item["old"]
            for item in replacements
        ):
            raise NotionValidationError("replacements must contain bounded non-empty old values")
    body: dict[str, Any] = {"page_id": page_id}
    if props:
        body.update({"command": "update_properties", "properties": props})
    if replacements:
        body.update({"command": "update_content", "operations": replacements})
    try:
        result = await notion_call("notion-update-page", body)
    except Exception as exc:
        raise NotionMutationIndeterminateError("Notion page update outcome is uncertain") from exc
    if not isinstance(result, dict):
        raise NotionMutationIndeterminateError("Notion page update outcome is uncertain")
    return result


@tool(native=False, exec_docs=False, namespace="notion")
async def add_comment(id_or_url: str, body: str) -> dict[str, Any]:
    """Add a bounded comment to a write-authorized page."""
    page_id = resource_id(id_or_url)
    policy = require_write()
    if policy["scope"] is not None:
        target = await notion_call("notion-fetch", {"id": page_id})
        if not isinstance(target, dict) or not in_scope(
            page_id, str(target.get("type", "page")), target.get("ancestors", []), policy["scope"]
        ):
            raise NotionWriteError("Notion page is outside the configured write scope")
    if not isinstance(body, str) or not 1 <= len(body) <= 8000:
        raise NotionValidationError("body must be 1-8000 characters")
    try:
        result = await notion_call(
            "notion-create-comment",
            {"page_id": page_id, "rich_text": [{"type": "text", "text": {"content": body}}]},
        )
    except Exception as exc:
        raise NotionMutationIndeterminateError("Notion comment outcome is uncertain") from exc
    if not isinstance(result, dict):
        raise NotionMutationIndeterminateError("Notion comment outcome is uncertain")
    return result

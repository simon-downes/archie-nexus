"""Notion policy validation and scope matching."""

from __future__ import annotations

import re
from typing import Any

from archie_shared.tool_policy import resolve_provider_policy

from archie_agent.exec.tools import ToolError

_NOTION_ID = re.compile(
    r"^[0-9a-fA-F]{32}$|^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class NotionValidationError(ToolError):
    pass


class NotionPolicyError(ToolError):
    pass


class NotionWritePolicyError(NotionPolicyError):
    pass


def normalize_id(value: str) -> str:
    if not isinstance(value, str) or not _NOTION_ID.fullmatch(value.strip()):
        raise NotionValidationError("Invalid Notion resource ID")
    return value.strip().replace("-", "").lower()


def _scope(value: Any) -> dict[str, set[str]] | None:
    if value is None or value == {}:
        return None
    if not isinstance(value, dict) or set(value) - {"pages", "databases"}:
        raise NotionValidationError("Invalid Notion scope")
    result: dict[str, set[str]] = {}
    for kind in ("pages", "databases"):
        values = value.get(kind, [])
        if not isinstance(values, list) or len(values) > 100:
            raise NotionValidationError("Invalid Notion scope")
        result[kind] = {normalize_id(item) for item in values}
    return None if not result["pages"] and not result["databases"] else result


def policies(snapshot: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    read, write = resolve_provider_policy(snapshot, "notion")
    return (
        {"enabled": read["enabled"], "scope": _scope(read.get("scope"))},
        {"enabled": write["enabled"], "scope": _scope(write.get("scope"))},
    )


def require_read(snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    policy, _ = policies(snapshot)
    if not policy["enabled"]:
        raise NotionPolicyError("Notion reads are disabled by policy")
    return policy


def require_write(snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    _, policy = policies(snapshot)
    if not policy["enabled"]:
        raise NotionWritePolicyError("Notion writes are disabled by policy")
    return policy


def in_scope(
    resource_id: str, resource_type: str, ancestors: list[str], scope: dict[str, set[str]] | None
) -> bool:
    if scope is None:
        return True
    resource = normalize_id(resource_id)
    database_resource = resource_type in {"database", "data_source", "database_item"}
    roots = scope.get("databases", set()) if database_resource else scope.get("pages", set())
    if resource in roots:
        return True
    ancestor_ids = {normalize_id(item) for item in ancestors}
    if ancestor_ids & roots:
        return True
    # A page root authorizes nested databases and their items.
    return database_resource and bool(ancestor_ids & scope.get("pages", set()))

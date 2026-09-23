"""Authoritative Slack read/write policy validation."""

from __future__ import annotations

from typing import Any

from archie_shared.tool_policy import current_policy_snapshot

from .errors import SlackPolicyError, SlackValidationError


def _block(value: Any, *, write: bool) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {"enabled", "scope"}:
        raise SlackValidationError("Invalid Slack policy")
    enabled = value.get("enabled", False if write else True)
    scope = value.get("scope")
    if scope is None:
        scope = {}
    if not isinstance(scope, dict) or set(scope) - {"deny"}:
        raise SlackValidationError("Invalid Slack policy")
    deny = scope.get("deny", [])
    if (
        not isinstance(enabled, bool)
        or not isinstance(deny, list)
        or any(not isinstance(item, str) for item in deny)
    ):
        raise SlackValidationError("Invalid Slack policy")
    return {"enabled": enabled, "scope": {"deny": list(deny)}}


def policy(snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    root = (snapshot if snapshot is not None else current_policy_snapshot()).get("slack") or {}
    if not isinstance(root, dict) or set(root) - {"enabled", "read", "write"}:
        raise SlackValidationError("Invalid Slack policy")
    enabled = root.get("enabled", True)
    if not isinstance(enabled, bool):
        raise SlackValidationError("Invalid Slack policy")
    return {
        "enabled": enabled,
        "read": _block(root.get("read"), write=False),
        "write": _block(root.get("write"), write=True),
    }


def require_read(snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    result = policy(snapshot)
    if not result["enabled"] or not result["read"]["enabled"]:
        raise SlackPolicyError("Slack reads are disabled by policy")
    return result


def deny_conversation(conversation: dict[str, Any], deny: list[str]) -> bool:
    identifier = str(conversation.get("id", "")).lower()
    name = conversation.get("name")
    values = {item.lower() for item in deny}
    if identifier and identifier in values:
        return True
    return isinstance(name, str) and f"#{name.lower()}" in values

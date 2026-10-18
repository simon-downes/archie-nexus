"""Authoritative Slack read/write policy validation; policy comes from the plugin context."""

from __future__ import annotations

from typing import Any

from archie_agent.exec import plugin as sdk

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


def _raw(block: sdk.PolicyBlock | None) -> dict[str, Any] | None:
    """A context policy block in the shape ``_block`` validates (None: the block is absent)."""
    if block is None:
        return None
    return {"enabled": block.enabled, "scope": block.scope}


def policy() -> dict[str, Any]:
    """Return the validated read and write policy from the plugin context.

    Both blocks are validated on every call, so an invalid scope in either fails closed.
    """
    active = sdk.context().policy
    return {
        "read": _block(_raw(active.read), write=False),
        "write": _block(_raw(active.write), write=True),
    }


def require_read() -> dict[str, Any]:
    result = policy()
    sdk.require_read(SlackPolicyError, "Slack reads are disabled by policy", scope_supported=True)
    return result


def deny_conversation(conversation: dict[str, Any], deny: list[str]) -> bool:
    identifier = str(conversation.get("id", "")).lower()
    name = conversation.get("name")
    values = {item.lower() for item in deny}
    if identifier and identifier in values:
        return True
    return isinstance(name, str) and f"#{name.lower()}" in values

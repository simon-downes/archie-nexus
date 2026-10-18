"""Jira-specific policy and validation; policy comes from the plugin context."""

from __future__ import annotations

import re
from typing import Any

from archie_agent.exec import plugin as sdk
from archie_agent.exec.plugin import ToolError

from .client import JiraPolicyError

_PROJECT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,9}$")
_ISSUE = re.compile(r"^([A-Za-z][A-Za-z0-9_]{0,9})-([1-9][0-9]*)$")


class JiraValidationError(ToolError):
    pass


class JiraWritePolicyError(ToolError):
    pass


def project_key(value: str) -> str:
    if not isinstance(value, str) or not _PROJECT.fullmatch(value):
        raise JiraValidationError("Invalid Jira project key")
    return value.upper()


def issue_key(value: str) -> str:
    if not isinstance(value, str):
        raise JiraValidationError("Invalid Jira issue key")
    match = _ISSUE.fullmatch(value)
    if not match:
        raise JiraValidationError("Invalid Jira issue key")
    return f"{match.group(1).upper()}-{match.group(2)}"


def _scope(value: Any) -> set[str] | None:
    if value is None or value == []:
        return None
    if not isinstance(value, list) or len(value) > 100:
        raise JiraValidationError("Invalid Jira project scope")
    return {project_key(item) for item in value}


def policies() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the parsed read and write policy from the plugin context.

    Both scopes are parsed on every call, so an invalid scope in either block fails closed.
    """
    policy = sdk.context().policy
    read, write = policy.read, policy.write
    return (
        {"enabled": bool(read.enabled), "scope": _scope(read.scope)},
        {
            "enabled": write is not None and bool(write.enabled),
            "scope": _scope(None if write is None else write.scope),
        },
    )


def require_read(key: str | None = None) -> None:
    policies()  # parse both scopes first: an invalid scope in either block fails closed
    block = sdk.require_read(
        JiraPolicyError, "Jira reads are disabled by policy", scope_supported=True
    )
    scope = _scope(block.scope)
    if key and scope is not None and project_key(key) not in scope:
        raise JiraPolicyError("Jira resource is outside the configured read scope")


def require_write(key: str | None = None) -> None:
    policies()  # parse both scopes first: an invalid scope in either block fails closed
    block = sdk.require_write(
        JiraWritePolicyError, "Jira writes are disabled by policy", scope_supported=True
    )
    scope = _scope(block.scope)
    if key and scope is not None and project_key(key) not in scope:
        raise JiraWritePolicyError("Jira resource is outside the configured write scope")

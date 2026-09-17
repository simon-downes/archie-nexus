"""Jira-specific policy, validation, and request context."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from archie_shared.tool_policy import resolve_provider_policy

from archie_agent.exec.tools import ToolError

from .client import JiraPolicyError

_PROJECT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,9}$")
_ISSUE = re.compile(r"^([A-Za-z][A-Za-z0-9_]{0,9})-([1-9][0-9]*)$")
_POLICY: dict[str, Any] = {}


class JiraValidationError(ToolError):
    pass


class JiraWritePolicyError(ToolError):
    pass


def set_policy_snapshot(snapshot: dict[str, Any]) -> None:
    global _POLICY
    _POLICY = snapshot


def _snapshot() -> dict[str, Any]:
    if _POLICY:
        return _POLICY
    try:
        return json.loads(os.environ.get("ARCHIE_TOOL_POLICY", "{}"))
    except (TypeError, ValueError):
        raise JiraValidationError("Invalid Jira tool policy") from None


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
    read, write = resolve_provider_policy(_snapshot(), "jira")
    return (
        {"enabled": bool(read["enabled"]), "scope": _scope(read.get("scope"))},
        {"enabled": bool(write["enabled"]), "scope": _scope(write.get("scope"))},
    )


def require_read(key: str | None = None) -> None:
    policy, _ = policies()
    if not policy["enabled"]:
        raise JiraPolicyError("Jira reads are disabled by policy")
    if key and policy["scope"] is not None and project_key(key) not in policy["scope"]:
        raise JiraPolicyError("Jira resource is outside the configured read scope")


def require_write(key: str | None = None) -> None:
    _, policy = policies()
    if not policy["enabled"]:
        raise JiraWritePolicyError("Jira writes are disabled by policy")
    if key and policy["scope"] is not None and project_key(key) not in policy["scope"]:
        raise JiraWritePolicyError("Jira resource is outside the configured write scope")

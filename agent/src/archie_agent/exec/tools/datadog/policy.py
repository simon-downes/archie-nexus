"""Datadog read/write policy gates. Scopes are not supported and fail closed."""

from __future__ import annotations

from typing import Any

from archie_shared.tool_policy import resolve_provider_policy

from .client import DatadogPolicyError


def _check_scope(block: dict[str, Any], kind: str) -> None:
    if block.get("scope") not in (None, [], {}):
        raise DatadogPolicyError(f"Datadog {kind} scope is not supported; remove it")


def require_read() -> None:
    """Raise before any request when reads are disabled or a scope is set."""
    read, _ = resolve_provider_policy(None, "datadog")
    if not read["enabled"]:
        raise DatadogPolicyError("Datadog reads are disabled by policy")
    _check_scope(read, "read")


def require_write() -> None:
    """Raise before any request unless writes are explicitly enabled without a scope."""
    _, write = resolve_provider_policy(None, "datadog")
    if not write["enabled"]:
        raise DatadogPolicyError("Datadog writes are disabled by policy")
    _check_scope(write, "write")

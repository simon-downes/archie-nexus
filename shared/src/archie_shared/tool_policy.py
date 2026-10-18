"""Per-exec tool-policy snapshots.

Snapshots are built from ``plugins.<id>.policy`` and manifest defaults by
``archie_shared.plugins.config.policy_snapshot``; this module installs them for an
exec call and resolves one provider's read/write blocks.
"""

from __future__ import annotations

import contextvars
from typing import Any

_CURRENT_SNAPSHOT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "archie_tool_policy", default=None
)


def set_policy_snapshot(snapshot: dict[str, Any]) -> contextvars.Token:
    """Install a detached invocation snapshot and return its reset token."""
    return _CURRENT_SNAPSHOT.set(snapshot or {})


def reset_policy_snapshot(token: contextvars.Token) -> None:
    """Restore the previous invocation snapshot."""
    _CURRENT_SNAPSHOT.reset(token)


def current_policy_snapshot() -> dict[str, Any]:
    """Return the current non-secret invocation snapshot."""
    return _CURRENT_SNAPSHOT.get() or {}


def resolve_provider_policy(
    snapshot: dict[str, Any] | None, provider: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve a provider from an explicit or current runner snapshot."""
    entry = (snapshot if snapshot is not None else current_policy_snapshot()).get(provider) or {}
    read = entry.get("read") or {}
    write = entry.get("write") or {}
    return (
        {"enabled": bool(read.get("enabled", True)), "scope": read.get("scope")},
        {"enabled": bool(write.get("enabled", False)), "scope": write.get("scope")},
    )

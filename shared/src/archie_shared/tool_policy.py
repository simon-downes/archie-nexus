"""Non-secret tool policy configuration and session snapshots."""

from __future__ import annotations

import contextvars
from typing import Any

import msgspec


class ToolPolicy(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One enabled flag and an opaque provider-specific scope."""

    enabled: bool = True
    scope: Any = None


class ProviderToolPolicy(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Independent read/write policy blocks for one provider."""

    read: ToolPolicy = msgspec.field(default_factory=ToolPolicy)
    write: ToolPolicy = msgspec.field(default_factory=lambda: ToolPolicy(enabled=False))


class ToolsConfig(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Provider policies keyed by provider name."""

    providers: dict[str, ProviderToolPolicy] = msgspec.field(default_factory=dict)

    def for_provider(self, provider: str) -> ProviderToolPolicy:
        """Return policy blocks, applying read-only defaults when absent."""
        return self.providers.get(provider, ProviderToolPolicy())


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


def policy_snapshot(config: ToolsConfig) -> dict[str, dict[str, dict[str, Any]]]:
    """Make a detached non-secret snapshot for an exec runner."""
    return {
        name: {
            "read": {"enabled": value.read.enabled, "scope": value.read.scope},
            "write": {"enabled": value.write.enabled, "scope": value.write.scope},
        }
        for name, value in config.providers.items()
    }


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

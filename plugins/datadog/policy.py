"""Datadog read/write policy gates. Scopes are not supported and fail closed."""

from __future__ import annotations

from archie_agent.exec import plugin as sdk

from .client import DatadogPolicyError

_READ_SCOPE = "Datadog read scope is not supported; remove it"
_WRITE_SCOPE = "Datadog write scope is not supported; remove it"


def _check_scopes() -> None:
    """A scope in either block fails every call closed, whether or not that block is enabled."""
    policy = sdk.context().policy
    for block, message in ((policy.read, _READ_SCOPE), (policy.write, _WRITE_SCOPE)):
        if block is not None and block.scope not in (None, [], {}):
            raise DatadogPolicyError(message)


def require_read() -> None:
    """Raise before any request when reads are disabled or any scope is set."""
    _check_scopes()
    sdk.require_read(
        DatadogPolicyError, "Datadog reads are disabled by policy", scope_message=_READ_SCOPE
    )


def require_write() -> None:
    """Raise before any request unless writes are enabled and no scope is set."""
    _check_scopes()
    sdk.require_write(
        DatadogPolicyError, "Datadog writes are disabled by policy", scope_message=_WRITE_SCOPE
    )

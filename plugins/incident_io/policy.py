"""incident.io read policy. The tools are read-only, so there is no write gate."""

from __future__ import annotations

from archie_agent.exec import plugin as sdk

from .client import IncidentIoPolicyError


def require_read() -> None:
    """Raise before any request when reads are disabled or an unsupported scope is set."""
    sdk.require_read(
        IncidentIoPolicyError,
        "incident.io reads are disabled by policy",
        scope_message="incident.io read scope is not supported; remove it",
    )

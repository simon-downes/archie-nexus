"""incident.io read policy. The tools are read-only, so there is no write gate."""

from __future__ import annotations

from archie_shared.tool_policy import resolve_provider_policy

from .client import IncidentIoPolicyError


def require_read() -> None:
    """Raise before any request when reads are disabled or an unsupported scope is set."""
    read, _ = resolve_provider_policy(None, "incident_io")
    if not read["enabled"]:
        raise IncidentIoPolicyError("incident.io reads are disabled by policy")
    if read.get("scope") not in (None, [], {}):
        raise IncidentIoPolicyError("incident.io read scope is not supported; remove it")

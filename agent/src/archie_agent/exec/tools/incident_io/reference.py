"""incident.io reference data and name-to-ID resolution."""

from __future__ import annotations

from typing import Any

from archie_agent.exec.tools import tool

from .client import IncidentIoClient, IncidentIoInputError, incident_io_client
from .formatting import reference_item
from .policy import require_read
from .validation import choose

KINDS: dict[str, tuple[str, str]] = {
    "severities": ("/v1/severities", "severities"),
    "incident_types": ("/v1/incident_types", "incident_types"),
    "incident_statuses": ("/v1/incident_statuses", "incident_statuses"),
    "incident_roles": ("/v2/incident_roles", "incident_roles"),
    "custom_fields": ("/v2/custom_fields", "custom_fields"),
}


_LABELS = {
    "severities": "severity",
    "incident_types": "incident type",
    "incident_statuses": "incident status",
    "incident_roles": "incident role",
    "custom_fields": "custom field",
}


def _kind(value: Any) -> tuple[str, str]:
    if not isinstance(value, str) or value not in KINDS:
        raise IncidentIoInputError(f"kind must be one of: {', '.join(KINDS)}")
    return KINDS[value]


async def resolve_ids(client: IncidentIoClient, kind: str, wanted: list[str]) -> list[str]:
    """Map names (case-insensitive) or IDs to IDs; unknown or ambiguous values are input errors."""
    path, key = KINDS[kind]
    items = [item for item in await client.items(path, key) if item.get("id")]
    label = _LABELS[kind]
    result: list[str] = []
    for value in wanted:
        found = str(choose(items, value, label)["id"])
        if found not in result:
            result.append(found)
    return result


@tool(native=False, exec_docs=False, namespace="incident_io")
async def reference(kind: str) -> list[dict]:
    """List incident.io reference data: severities, incident types, statuses, roles or fields."""
    path, key = _kind(kind)
    require_read()
    async with incident_io_client() as client:
        items = await client.items(path, key)
    return [reference_item(item) for item in items[:200]]

"""incident.io reference data and name-to-ID resolution."""

from __future__ import annotations

from typing import Any

from archie_agent.exec.tools import tool

from .client import IncidentIoClient, IncidentIoInputError, incident_io_client
from .formatting import reference_item
from .policy import require_read

KINDS: dict[str, tuple[str, str]] = {
    "severities": ("/v1/severities", "severities"),
    "incident_types": ("/v1/incident_types", "incident_types"),
    "incident_statuses": ("/v1/incident_statuses", "incident_statuses"),
    "incident_roles": ("/v2/incident_roles", "incident_roles"),
    "custom_fields": ("/v2/custom_fields", "custom_fields"),
}


def _kind(value: Any) -> tuple[str, str]:
    if not isinstance(value, str) or value not in KINDS:
        raise IncidentIoInputError(f"kind must be one of: {', '.join(KINDS)}")
    return KINDS[value]


async def resolve_ids(client: IncidentIoClient, kind: str, wanted: list[str]) -> list[str]:
    """Map names (case-insensitive) or IDs to IDs; unknown values are an input error."""
    path, key = KINDS[kind]
    items = await client.items(path, key)
    by_id = {str(item.get("id")): str(item.get("id")) for item in items if item.get("id")}
    by_name: dict[str, list[str]] = {}
    for item in items:
        if item.get("id") and isinstance(item.get("name"), str):
            by_name.setdefault(item["name"].strip().lower(), []).append(str(item["id"]))
    result: list[str] = []
    for value in wanted:
        if value in by_id:
            match = [by_id[value]]
        else:
            match = by_name.get(value.lower(), [])
        if len(match) != 1:
            names = sorted({str(item.get("name")) for item in items if item.get("name")})
            problem = "matches more than one" if match else "is not a known"
            raise IncidentIoInputError(
                f"{value!r} {problem} {kind[:-1].replace('_', ' ')}; "
                f"valid names: {', '.join(names[:50])}"
            )
        if match[0] not in result:
            result.append(match[0])
    return result


@tool(native=False, exec_docs=False, namespace="incident_io")
async def reference(kind: str) -> list[dict]:
    """List incident.io reference data: severities, incident types, statuses, roles or fields."""
    path, key = _kind(kind)
    require_read()
    async with incident_io_client() as client:
        items = await client.items(path, key)
    return [reference_item(item) for item in items[:200]]

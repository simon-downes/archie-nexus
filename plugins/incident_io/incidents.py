"""incident.io incident reads."""

from __future__ import annotations

from typing import Any

from archie_agent.exec.plugin import tool

from . import validation as v
from .client import IncidentIoInputError, IncidentIoResponseError, incident_io_client
from .formatting import incident_detail, incident_summary, incident_update, timeline_item
from .policy import require_read
from .reference import resolve_ids

STATUS_CATEGORIES = (
    "triage",
    "declined",
    "merged",
    "canceled",
    "live",
    "learning",
    "closed",
    "paused",
)
MODES = ("standard", "retrospective", "test", "tutorial")
_SORT = {"newest": "created_at_newest_first", "oldest": "created_at_oldest_first"}


@tool(namespace="incident_io")
async def list_incidents(
    status_category: str | list[str] | None = None,
    severity: str | list[str] | None = None,
    incident_type: str | list[str] | None = None,
    status: str | list[str] | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    updated_after: str | None = None,
    updated_before: str | None = None,
    mode: str | list[str] | None = None,
    sort: str = "newest",
    limit: int = 25,
) -> list[dict]:
    """List incidents using structured filters; newest first by default."""
    count = v.limit(limit)
    if not isinstance(sort, str) or sort not in _SORT:
        raise IncidentIoInputError("sort must be 'newest' or 'oldest'")
    params: dict[str, Any] = {"sort_by": _SORT[sort]}
    if status_category is not None:
        params["status_category[one_of]"] = v.enum(
            status_category, "status_category", STATUS_CATEGORIES
        )
    if mode is not None:
        params["mode[one_of]"] = v.enum(mode, "mode", MODES)
    v.date_filters(params, "created_at", created_after, created_before, date_only=True)
    v.date_filters(params, "updated_at", updated_after, updated_before, date_only=True)
    lookups = {
        "severity": ("severities", severity),
        "incident_type": ("incident_types", incident_type),
        "status": ("incident_statuses", status),
    }
    wanted = {
        field: (kind, v.values(value, field))
        for field, (kind, value) in lookups.items()
        if value is not None
    }
    require_read()
    async with incident_io_client() as client:
        for field, (kind, names) in wanted.items():
            params[f"{field}[one_of]"] = await resolve_ids(client, kind, names)
        items = await client.paginate("/v2/incidents", "incidents", count, params)
    return [incident_summary(item) for item in items]


@tool(namespace="incident_io")
async def get_incident(incident: str) -> dict:
    """Get one incident by ID, INC-<number> or <number>, with participants and alert IDs."""
    key = v.incident_id(incident)
    require_read()
    async with incident_io_client() as client:
        data = await client.get(f"/v2/incidents/{key}")
        item = data.get("incident")
        if not isinstance(item, dict) or not item.get("id"):
            raise IncidentIoResponseError("incident.io returned a malformed incident")
        people = await client.get("/v2/incident_participants", {"incident_id": item["id"]})
        links = await client.paginate(
            "/v2/incident_alerts",
            "incident_alerts",
            50,
            {"incident_id": item["id"]},
            max_page_size=50,
        )
    alert_ids = []
    for link in links:
        alert = link.get("alert") if isinstance(link.get("alert"), dict) else {}
        value = alert.get("id") or link.get("alert_id")
        if isinstance(value, str) and value not in alert_ids:
            alert_ids.append(value)
    participants = people.get("incident_participants")
    return incident_detail(item, participants if isinstance(participants, dict) else {}, alert_ids)


@tool(namespace="incident_io")
async def incident_timeline(incident: str, limit: int = 25) -> list[dict]:
    """List an incident's timeline items in the API's order (oldest first)."""
    key, count = v.incident_id(incident), v.limit(limit)
    require_read()
    async with incident_io_client() as client:
        incident_ulid = await resolve_incident_ulid(client, key)
        items = await client.paginate(
            "/v2/incident_timeline_items",
            "incident_timeline_items",
            count,
            {"incident_id": incident_ulid},
        )
    return [timeline_item(item) for item in items]


@tool(namespace="incident_io")
async def incident_updates(incident: str, limit: int = 25) -> list[dict]:
    """List an incident's status and severity updates with their messages."""
    key, count = v.incident_id(incident), v.limit(limit)
    require_read()
    async with incident_io_client() as client:
        incident_ulid = await resolve_incident_ulid(client, key)
        items = await client.paginate(
            "/v2/incident_updates", "incident_updates", count, {"incident_id": incident_ulid}
        )
    return [incident_update(item) for item in items]


async def resolve_incident_ulid(client, key: str) -> str:
    """Child endpoints filter by the incident ULID, so resolve a reference number first."""
    if not key.isdigit():
        return key
    data = await client.get(f"/v2/incidents/{key}")
    item = data.get("incident")
    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
        raise IncidentIoResponseError("incident.io returned a malformed incident")
    return item["id"]

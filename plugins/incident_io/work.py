"""incident.io actions, follow-ups and post-mortems."""

from __future__ import annotations

from typing import Any

from archie_agent.exec.plugin import tool

from . import validation as v
from .client import IncidentIoResponseError, incident_io_client
from .formatting import action, follow_up, postmortem
from .incidents import resolve_incident_ulid
from .policy import require_read

WORK_STATUSES = ("outstanding", "completed", "deleted", "not_doing")


def _date_params(created_after: Any, updated_after: Any) -> dict[str, Any]:
    params: dict[str, Any] = {}
    v.date_filters(params, "created_at", created_after)
    v.date_filters(params, "updated_at", updated_after)
    return params


def _status_keep(status: Any):
    if status is None:
        return None
    wanted = set(v.enum(status, "status", WORK_STATUSES))
    return lambda item: item.get("status") in wanted


@tool(namespace="incident_io")
async def list_actions(
    incident: str | None = None,
    status: str | list[str] | None = None,
    created_after: str | None = None,
    updated_after: str | None = None,
    limit: int = 25,
) -> list[dict]:
    """List incident actions, optionally for one incident or with given statuses."""
    count = v.limit(limit)
    key = v.incident_id(incident) if incident is not None else None
    keep = _status_keep(status)
    params = _date_params(created_after, updated_after)
    require_read()
    async with incident_io_client() as client:
        if key is not None:
            params["incident_id"] = await resolve_incident_ulid(client, key)
        items = await client.paginate("/v3/actions", "actions", count, params, keep=keep)
    return [action(item) for item in items]


@tool(namespace="incident_io")
async def list_follow_ups(
    incident: str | None = None,
    status: str | list[str] | None = None,
    team: str | None = None,
    created_after: str | None = None,
    updated_after: str | None = None,
    limit: int = 25,
) -> list[dict]:
    """List follow-ups, optionally for one incident, owning team ID, or given statuses."""
    count = v.limit(limit)
    key = v.incident_id(incident) if incident is not None else None
    team_id = v.optional_identifier(team, "team")
    keep = _status_keep(status)
    params = _date_params(created_after, updated_after)
    if team_id:
        params["assignee_team_id"] = team_id
    require_read()
    async with incident_io_client() as client:
        if key is not None:
            params["incident_id"] = await resolve_incident_ulid(client, key)
        items = await client.paginate("/v3/follow_ups", "follow_ups", count, params, keep=keep)
    return [follow_up(item) for item in items]


@tool(namespace="incident_io")
async def list_postmortems(incident: str | None = None, limit: int = 25) -> list[dict]:
    """List post-mortem documents, newest first, optionally for one incident."""
    count = v.limit(limit)
    key = v.incident_id(incident) if incident is not None else None
    require_read()
    async with incident_io_client() as client:
        params: dict[str, Any] = {"sort_by": "created_at_newest_first"}
        if key is not None:
            params["incident_id"] = await resolve_incident_ulid(client, key)
        items = await client.paginate(
            "/v1/postmortem_documents", "postmortem_documents", count, params
        )
    return [postmortem(item) for item in items]


@tool(namespace="incident_io")
async def get_postmortem(postmortem_id: str) -> dict:
    """Get one post-mortem with its full markdown content."""
    key = v.identifier(postmortem_id, "postmortem_id")
    require_read()
    async with incident_io_client() as client:
        data = await client.get(f"/v1/postmortem_documents/{key}")
        item = data.get("postmortem_document")
        if not isinstance(item, dict) or not item.get("id"):
            raise IncidentIoResponseError("incident.io returned a malformed post-mortem")
        content = await client.get(f"/v1/postmortem_documents/{key}/content")
    markdown = content.get("markdown")
    return {
        **postmortem(item),
        "content": markdown if isinstance(markdown, str) and markdown else None,
    }

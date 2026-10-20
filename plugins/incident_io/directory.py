"""incident.io teams, users and catalog."""

from __future__ import annotations

from typing import Any

from archie_agent.exec.plugin import tool

from . import validation as v
from .client import IncidentIoClient, IncidentIoInputError, incident_io_client
from .formatting import catalog_entry, catalog_type, directory_user, team
from .policy import require_read


@tool(namespace="incident_io")
async def list_teams(limit: int = 25) -> list[dict]:
    """List teams with their members."""
    count = v.limit(limit)
    require_read()
    async with incident_io_client() as client:
        items = await client.paginate("/v3/teams", "teams", count)
    return [team(item) for item in items]


@tool(namespace="incident_io")
async def list_users(
    email: str | None = None,
    slack_user_id: str | None = None,
    include_inactive: bool = False,
    limit: int = 25,
) -> list[dict]:
    """List users, or find one by exact email or Slack user ID."""
    count = v.limit(limit)
    if not isinstance(include_inactive, bool):
        raise IncidentIoInputError("include_inactive must be a bool")
    params: dict[str, Any] = {}
    if email is not None:
        params["email"] = v.text(email, "email")
    if slack_user_id is not None:
        params["slack_user_id"] = v.identifier(slack_user_id, "slack_user_id")
    if include_inactive:
        params["include_inactive"] = "true"
    require_read()
    async with incident_io_client() as client:
        items = await client.paginate("/v2/users", "users", count, params)
    return [directory_user(item) for item in items]


@tool(namespace="incident_io")
async def list_catalog_types() -> list[dict]:
    """List catalog types (for example services, teams, features) and their attributes."""
    require_read()
    async with incident_io_client() as client:
        items = await client.items("/v3/catalog_types", "catalog_types")
    return [catalog_type(item) for item in items[:100]]


async def _catalog_type(client: IncidentIoClient, value: str) -> dict:
    """Resolve a catalog type by ID, type name, or case-insensitive display name."""
    items = await client.items("/v3/catalog_types", "catalog_types")
    return v.choose(items, value, "catalog type", id_keys=("id", "type_name"))


@tool(namespace="incident_io")
async def list_catalog_entries(
    catalog_type: str, identifier: str | None = None, limit: int = 25
) -> list[dict]:
    """List entries of one catalog type, or find entries by ID, external ID, alias or name."""
    wanted = v.text(catalog_type, "catalog_type")
    count = v.limit(limit)
    lookup = v.optional_text(identifier, "identifier")
    require_read()
    async with incident_io_client() as client:
        found = await _catalog_type(client, wanted)
        attributes = (found.get("schema") or {}).get("attributes") or []
        names = {
            str(a["id"]): str(a.get("name") or a["id"])
            for a in attributes
            if isinstance(a, dict) and a.get("id")
        }
        params: dict[str, Any] = {"catalog_type_id": found["id"]}
        if lookup:
            params["identifier"] = lookup
        items = await client.paginate("/v3/catalog_entries", "catalog_entries", count, params)
    return [catalog_entry(item, names) for item in items]

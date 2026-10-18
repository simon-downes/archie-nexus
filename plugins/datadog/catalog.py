"""Software catalog entities and teams."""

from __future__ import annotations

from typing import Any

from archie_agent.exec.plugin import tool

from . import validation
from .client import datadog_client
from .formatting import catalog_entity, team
from .paging import collection
from .policy import require_read


@tool(namespace="datadog")
async def list_catalog_entities(
    kind: str | None = None, query: str | None = None, limit: int = 50
) -> list[dict]:
    """List software catalog entities, optionally by ``kind`` (``service``, ``api``…) and name."""
    entity_kind = validation.kind(kind)
    name = validation.optional_query(query)
    count = validation.limit(limit)
    require_read()
    params: dict[str, Any] = {"page[limit]": count, "page[offset]": 0}
    if entity_kind:
        params["filter[kind]"] = entity_kind
    if name:
        params["filter[name]"] = name
    async with datadog_client() as client:
        value = await client.request("GET", "/api/v2/catalog/entity", params=params)
    return [catalog_entity(item) for item in collection(value)[:count]]


@tool(namespace="datadog")
async def list_teams(query: str | None = None, limit: int = 50) -> list[dict]:
    """List Datadog teams, optionally filtered by a keyword in the name or handle."""
    keyword = validation.optional_query(query)
    count = validation.limit(limit)
    require_read()
    params: dict[str, Any] = {"page[size]": count, "page[number]": 0}
    if keyword:
        params["filter[keyword]"] = keyword
    async with datadog_client() as client:
        value = await client.request("GET", "/api/v2/team", params=params)
    return [team(item) for item in collection(value)[:count]]

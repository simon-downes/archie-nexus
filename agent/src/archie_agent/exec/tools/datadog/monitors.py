"""Monitor reads, validation and ownership-guarded writes."""

from __future__ import annotations

from typing import Any

from archie_agent.exec.tools import tool

from . import validation
from .client import DatadogResponseError, datadog_client
from .formatting import monitor
from .policy import require_read


def definition(
    name: Any,
    type: Any,
    query: Any,
    message: Any,
    tags: Any,
    priority: Any,
    options: Any,
) -> dict[str, Any]:
    """Validate a full monitor definition and return the request body, ownership tag included."""
    body: dict[str, Any] = {
        "name": validation.text(name, "name", 500),
        "type": validation.choice(type, "type", validation.MONITOR_TYPES),
        "query": validation.query(query),
        "message": validation.text(message, "message"),
        "tags": validation.with_ownership(validation.tags(tags)),
    }
    level = validation.priority(priority)
    if level is not None:
        body["priority"] = level
    extra = validation.monitor_options(options)
    if extra is not None:
        body["options"] = extra
    return body


@tool(native=False, exec_docs=False, namespace="datadog")
async def search_monitors(query: str | None = None, limit: int = 20) -> list[dict]:
    """Search monitors with monitor search syntax (for example ``status:alert tag:env:prod``)."""
    text = validation.optional_query(query)
    count = validation.limit(limit)
    require_read()
    params: dict[str, Any] = {"per_page": count, "page": 0}
    if text:
        params["query"] = text
    async with datadog_client() as client:
        data = await client.request("GET", "/api/v1/monitor/search", params=params)
    items = data.get("monitors") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise DatadogResponseError("Datadog returned a malformed monitor search response")
    return [monitor(item) for item in items[:count] if isinstance(item, dict)]


async def _fetch(client: Any, monitor_id: int) -> dict[str, Any]:
    data = await client.request("GET", f"/api/v1/monitor/{monitor_id}")
    if not isinstance(data, dict) or "id" not in data:
        raise DatadogResponseError("Datadog returned a malformed monitor")
    return data


@tool(native=False, exec_docs=False, namespace="datadog")
async def get_monitor(monitor_id: int) -> dict:
    """Return one monitor with its message and allowed options."""
    identifier = validation.monitor_id(monitor_id)
    require_read()
    async with datadog_client() as client:
        data = await _fetch(client, identifier)
    return monitor(data, detail=True)


@tool(native=False, exec_docs=False, namespace="datadog")
async def validate_monitor(
    name: str,
    type: str,
    query: str,
    message: str,
    tags: list[str] | None = None,
    priority: int | None = None,
    options: dict | None = None,
) -> dict:
    """Ask Datadog whether a monitor definition is valid, without creating it."""
    body = definition(name, type, query, message, tags, priority, options)
    require_read()
    async with datadog_client() as client:
        await client.request("POST", "/api/v1/monitor/validate", json=body)
    return {"valid": True}

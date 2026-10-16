"""Event stream reads (deploys, alerts, integrations, Kubernetes changes)."""

from __future__ import annotations

from archie_agent.exec.tools import tool

from . import validation
from .client import datadog_client
from .formatting import event
from .paging import iso, list_pages
from .policy import require_read


@tool(native=False, exec_docs=False, namespace="datadog")
async def list_events(
    query: str | None = None,
    start: str = "1h",
    end: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """List events, newest first, optionally filtered with event search syntax."""
    text = validation.optional_query(query)
    begin, finish = validation.time_window(start, end)
    count = validation.limit(limit)
    require_read()
    params = {"filter[from]": iso(begin), "filter[to]": iso(finish), "sort": "-timestamp"}
    if text:
        params["filter[query]"] = text
    async with datadog_client() as client:
        items = await list_pages(client, "/api/v2/events", params, count)
    return [event(item) for item in items]

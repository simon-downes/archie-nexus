"""Dashboard reads. Dashboards are read-only: no ownership marker exists for them."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from archie_agent.exec.plugin import tool

from . import validation
from .client import DatadogClient, DatadogResponseError, datadog_client
from .formatting import dashboard_detail, dashboard_summary
from .policy import require_read

PAGE_SIZE = 100
MAX_PAGES = 20


async def dashboard_pages(client: DatadogClient) -> AsyncIterator[list[dict[str, Any]]]:
    """Yield pages of custom dashboard summaries, 100 at a time, at most 20 pages.

    The list endpoint returns 100 dashboards unless asked for more, and omits preset ones.
    """
    for page in range(MAX_PAGES):
        data = await client.request(
            "GET", "/api/v1/dashboard", params={"count": PAGE_SIZE, "start": page * PAGE_SIZE}
        )
        items = data.get("dashboards") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise DatadogResponseError("Datadog returned a malformed dashboard list")
        yield [item for item in items if isinstance(item, dict)]
        if len(items) < PAGE_SIZE:
            return


@tool(namespace="datadog")
async def list_dashboards(query: str | None = None, limit: int = 50) -> list[dict]:
    """List dashboards, optionally keeping titles that contain ``query`` (case-insensitive)."""
    text = validation.optional_query(query)
    count = validation.limit(limit, 1_000)
    require_read()
    needle = text.casefold() if text else None
    kept: list[dict[str, Any]] = []
    async with datadog_client() as client, aclosing(dashboard_pages(client)) as pages:
        async for page in pages:
            kept.extend(
                item
                for item in page
                if needle is None or needle in str(item.get("title") or "").casefold()
            )
            if len(kept) >= count:
                break
    return [dashboard_summary(item) for item in kept[:count]]


@tool(namespace="datadog")
async def get_dashboard(dashboard_id: str) -> dict:
    """Return a dashboard with its widgets, the widgets inside groups, and their queries."""
    identifier = validation.dashboard_id(dashboard_id)
    require_read()
    async with datadog_client() as client:
        data = await client.request("GET", f"/api/v1/dashboard/{identifier}")
    if not isinstance(data, dict) or "id" not in data:
        raise DatadogResponseError("Datadog returned a malformed dashboard")
    return dashboard_detail(data)

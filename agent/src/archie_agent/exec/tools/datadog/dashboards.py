"""Dashboard reads. Dashboards are read-only: no ownership marker exists for them."""

from __future__ import annotations

from archie_agent.exec.tools import tool

from . import validation
from .client import DatadogResponseError, datadog_client
from .formatting import dashboard_detail, dashboard_summary
from .policy import require_read


@tool(native=False, exec_docs=False, namespace="datadog")
async def list_dashboards(query: str | None = None, limit: int = 50) -> list[dict]:
    """List dashboards, optionally keeping titles that contain ``query`` (case-insensitive)."""
    text = validation.optional_query(query)
    count = validation.limit(limit)
    require_read()
    async with datadog_client() as client:
        data = await client.request("GET", "/api/v1/dashboard")
    items = data.get("dashboards") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise DatadogResponseError("Datadog returned a malformed dashboard list")
    needle = text.casefold() if text else None
    kept = [
        item
        for item in items
        if isinstance(item, dict)
        and (needle is None or needle in str(item.get("title") or "").casefold())
    ]
    return [dashboard_summary(item) for item in kept[:count]]


@tool(native=False, exec_docs=False, namespace="datadog")
async def get_dashboard(dashboard_id: str) -> dict:
    """Return a dashboard with its top-level widgets and their queries."""
    identifier = validation.dashboard_id(dashboard_id)
    require_read()
    async with datadog_client() as client:
        data = await client.request("GET", f"/api/v1/dashboard/{identifier}")
    if not isinstance(data, dict) or "id" not in data:
        raise DatadogResponseError("Datadog returned a malformed dashboard")
    return dashboard_detail(data)

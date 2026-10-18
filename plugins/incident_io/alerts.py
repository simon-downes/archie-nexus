"""incident.io alert reads."""

from __future__ import annotations

from typing import Any

from archie_agent.exec.plugin import tool

from . import validation as v
from .client import IncidentIoResponseError, incident_io_client
from .formatting import alert, alert_detail
from .policy import require_read

ALERT_STATUSES = ("firing", "resolved")
_PAGE = 50  # /v2/alerts and /v2/incident_alerts reject page_size above 50


@tool(namespace="incident_io")
async def list_alerts(
    status: str | list[str] | None = None,
    source: str | list[str] | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    limit: int = 25,
) -> list[dict]:
    """List alerts filtered by status, alert source ID or creation time (API order)."""
    count = v.limit(limit)
    params: dict[str, Any] = {}
    if status is not None:
        params["status[one_of]"] = v.enum(status, "status", ALERT_STATUSES)
    if source is not None:
        params["alert_source[one_of]"] = [
            v.identifier(item, "source") for item in v.values(source, "source")
        ]
    v.date_filters(params, "created_at", created_after, created_before)
    require_read()
    async with incident_io_client() as client:
        items = await client.paginate("/v2/alerts", "alerts", count, params, max_page_size=_PAGE)
    return [alert(item) for item in items]


@tool(namespace="incident_io")
async def get_alert(alert_id: str) -> dict:
    """Get one alert with its description, attributes and linked incident IDs."""
    key = v.identifier(alert_id, "alert_id")
    require_read()
    async with incident_io_client() as client:
        data = await client.get(f"/v2/alerts/{key}")
        item = data.get("alert")
        if not isinstance(item, dict) or not item.get("id"):
            raise IncidentIoResponseError("incident.io returned a malformed alert")
        links = await client.paginate(
            "/v2/incident_alerts", "incident_alerts", 50, {"alert_id": key}, max_page_size=_PAGE
        )
    incident_ids: list[str] = []
    for link in links:
        incident = link.get("incident") if isinstance(link.get("incident"), dict) else {}
        value = incident.get("id") or link.get("incident_id")
        if isinstance(value, str) and value not in incident_ids:
            incident_ids.append(value)
    return alert_detail(item, incident_ids)

"""Log search and log aggregation."""

from __future__ import annotations

from typing import Any

from archie_agent.exec.plugin import tool

from . import validation
from .client import DatadogResponseError, datadog_client
from .formatting import aggregate_bucket, log
from .paging import iso, search_pages
from .policy import require_read

_AGGREGATE_PAGES = 5


@tool(namespace="datadog")
async def search_logs(
    query: str,
    start: str = "1h",
    end: str | None = None,
    limit: int = 20,
    sort: str = "newest",
) -> list[dict]:
    """Search logs with Datadog log search syntax (for example ``service:api status:error``)."""
    text = validation.query(query)
    begin, finish = validation.time_window(start, end)
    count = validation.limit(limit)
    order = validation.SORTS[validation.choice(sort, "sort", set(validation.SORTS))]
    require_read()
    body = {"filter": {"query": text, "from": iso(begin), "to": iso(finish)}, "sort": order}
    async with datadog_client() as client:
        items = await search_pages(client, "/api/v2/logs/events/search", body, count)
    return [log(item) for item in items]


@tool(namespace="datadog")
async def aggregate_logs(
    query: str,
    group_by: list | None = None,
    start: str = "1h",
    end: str | None = None,
    aggregation: str = "count",
    metric: str | None = None,
    interval: str | None = None,
    order: str = "desc",
) -> dict:
    """Count or aggregate matching logs server-side, grouped by up to four facets."""
    text = validation.query(query)
    begin, finish = validation.time_window(start, end)
    groups = validation.log_group_by(group_by)
    function = validation.log_aggregation(aggregation, metric)
    field = validation.optional_facet(metric, "metric")
    step = validation.interval(interval)
    direction = validation.choice(order, "order", {"asc", "desc"})
    require_read()
    compute: dict[str, Any] = {"aggregation": function, "type": "total"}
    if field:
        compute["metric"] = field
    if step:
        compute.update(type="timeseries", interval=step)
    sort: dict[str, Any] = {"type": "measure", "aggregation": function, "order": direction}
    if field:
        sort["metric"] = field
    body: dict[str, Any] = {
        "filter": {"query": text, "from": iso(begin), "to": iso(finish)},
        "compute": [compute],
        "group_by": [
            {key: value for key, value in {**group, "sort": sort}.items() if value is not None}
            for group in groups
        ],
    }
    buckets: list[dict] = []
    warnings: list[str] = []
    partial = False
    async with datadog_client() as client:
        for _ in range(_AGGREGATE_PAGES):
            value = await client.request("POST", "/api/v2/logs/analytics/aggregate", json=body)
            data = value.get("data") if isinstance(value, dict) else None
            items = data.get("buckets") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise DatadogResponseError("Datadog returned a malformed log aggregate")
            buckets.extend(aggregate_bucket(item) for item in items if isinstance(item, dict))
            meta = value.get("meta") if isinstance(value.get("meta"), dict) else {}
            partial = partial or meta.get("status") == "timeout"
            warnings.extend(
                str(item.get("title") or item.get("code"))[:200]
                for item in meta.get("warnings") or []
                if isinstance(item, dict)
            )
            page = meta.get("page") if isinstance(meta.get("page"), dict) else {}
            cursor = page.get("after")
            if not isinstance(cursor, str) or not cursor or not items:
                break
            body = {**body, "page": {"cursor": cursor}}
    return {
        "from": iso(begin),
        "to": iso(finish),
        "group_by": [group["facet"] for group in groups],
        "bucket_count": len(buckets),
        "buckets": buckets,
        "partial": partial,
        "warnings": warnings[:10],
    }

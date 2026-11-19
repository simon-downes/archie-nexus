"""Log search and log aggregation."""

from __future__ import annotations

from archie_agent.exec.plugin import tool

from . import validation
from .client import DatadogResponseError, datadog_client
from .formatting import aggregate_bucket, log
from .paging import aggregate_attributes, aggregate_result, iso, meta_warnings, search_pages
from .policy import require_read

_AGGREGATE_PAGES = 5


@tool(namespace="datadog")
async def search_logs(
    query: str,
    start: str = "1h",
    end: str | None = None,
    limit: int = 20,
    sort: str = "newest",
    fields: list | None = None,
) -> list[dict]:
    """Search logs with Datadog log search syntax (for example ``service:api status:error``).

    ``fields`` names attribute paths (``@http.client_ip``) to return in full under ``fields``.
    """
    text = validation.query(query)
    begin, finish = validation.time_window(start, end)
    count = validation.limit(limit)
    order = validation.SORTS[validation.choice(sort, "sort", set(validation.SORTS))]
    paths = validation.fields(fields)
    require_read()
    body = {"filter": {"query": text, "from": iso(begin), "to": iso(finish)}, "sort": order}
    async with datadog_client() as client:
        items = await search_pages(client, "/api/v2/logs/events/search", body, count)
    return [log(item, paths) for item in items]


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
    spec = validation.aggregate(query, group_by, start, end, aggregation, metric, interval, order)
    require_read()
    body = aggregate_attributes(spec)
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
            warnings.extend(meta_warnings(meta))
            page = meta.get("page") if isinstance(meta.get("page"), dict) else {}
            cursor = page.get("after")
            if not isinstance(cursor, str) or not cursor or not items:
                break
            body = {**body, "page": {"cursor": cursor}}
    return aggregate_result(spec, buckets, partial, warnings)

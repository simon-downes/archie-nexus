"""APM span search and aggregation.

Span search allows 5 requests per minute and span aggregation 50, so each call makes one request.
"""

from __future__ import annotations

from archie_agent.exec.plugin import tool

from . import validation
from .client import DatadogResponseError, datadog_client
from .formatting import span, span_bucket
from .paging import aggregate_attributes, aggregate_result, collection, iso, meta_warnings
from .policy import require_read


@tool(namespace="datadog")
async def search_spans(
    query: str,
    start: str = "1h",
    end: str | None = None,
    limit: int = 20,
    sort: str = "newest",
    fields: list | None = None,
) -> list[dict]:
    """Search spans with Datadog span search syntax (for example ``service:api status:error``).

    ``fields`` names span attribute paths (``@http.client_ip``) to return under ``fields``.
    """
    text = validation.query(query)
    begin, finish = validation.time_window(start, end)
    count = validation.limit(limit)
    order = validation.SORTS[validation.choice(sort, "sort", set(validation.SORTS))]
    paths = validation.fields(fields)
    require_read()
    body = {
        "data": {
            "type": "search_request",
            "attributes": {
                "filter": {"query": text, "from": iso(begin), "to": iso(finish)},
                "sort": order,
                "page": {"limit": count},
            },
        }
    }
    async with datadog_client() as client:
        value = await client.request("POST", "/api/v2/spans/events/search", json=body)
    return [span(item, paths) for item in collection(value)[:count]]


@tool(namespace="datadog")
async def aggregate_spans(
    query: str,
    group_by: list | None = None,
    start: str = "1h",
    end: str | None = None,
    aggregation: str = "count",
    metric: str | None = None,
    interval: str | None = None,
    order: str = "desc",
) -> dict:
    """Count or aggregate matching spans server-side, grouped by up to four facets."""
    spec = validation.aggregate(query, group_by, start, end, aggregation, metric, interval, order)
    require_read()
    body = {"data": {"type": "aggregate_request", "attributes": aggregate_attributes(spec)}}
    async with datadog_client() as client:
        value = await client.request("POST", "/api/v2/spans/analytics/aggregate", json=body)
    items = value.get("data") if isinstance(value, dict) else None
    if not isinstance(items, list):
        raise DatadogResponseError("Datadog returned a malformed span aggregate")
    meta = value.get("meta") if isinstance(value.get("meta"), dict) else {}
    buckets = [span_bucket(item) for item in items if isinstance(item, dict)]
    return aggregate_result(spec, buckets, meta.get("status") == "timeout", meta_warnings(meta))

"""APM span search. One request per call: the endpoint allows 5 requests per minute."""

from __future__ import annotations

from archie_agent.exec.tools import tool

from . import validation
from .client import datadog_client
from .formatting import span
from .paging import collection, iso
from .policy import require_read


@tool(native=False, exec_docs=False, namespace="datadog")
async def search_spans(
    query: str,
    start: str = "1h",
    end: str | None = None,
    limit: int = 20,
    sort: str = "newest",
) -> list[dict]:
    """Search spans with Datadog span search syntax (for example ``service:api status:error``)."""
    text = validation.query(query)
    begin, finish = validation.time_window(start, end)
    count = validation.limit(limit)
    order = validation.SORTS[validation.choice(sort, "sort", set(validation.SORTS))]
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
    return [span(item) for item in collection(value)[:count]]

"""Log search."""

from __future__ import annotations

from archie_agent.exec.plugin import tool

from . import validation
from .client import datadog_client
from .formatting import log
from .paging import iso, search_pages
from .policy import require_read


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

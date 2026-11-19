"""Read-only log configuration: pipelines, indexes, log-based metrics and data scanning.

These endpoints need the application key's ``logs_read_config`` permission (the scanner needs
``data_scanner_read``); a missing permission surfaces as ``DatadogAuthorizationError``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from archie_agent.exec.plugin import tool

from . import validation
from .client import DatadogResponseError, datadog_client
from .formatting import pipeline_detail, pipeline_summary
from .policy import require_read


def in_order(
    items: list[dict[str, Any]], order: list[Any], key: Callable[[dict[str, Any]], Any]
) -> list[tuple[int | None, dict[str, Any]]]:
    """Pair items with their 1-based position in ``order``; unlisted items follow, unnumbered."""
    positions = {value: index for index, value in enumerate(order, start=1)}
    listed = sorted(
        ((positions[key(item)], item) for item in items if key(item) in positions),
        key=lambda pair: pair[0],
    )
    rest = [(None, item) for item in items if key(item) not in positions]
    return [*listed, *rest]


def _matches(needle: str | None, *values: Any) -> bool:
    if needle is None:
        return True
    return any(isinstance(value, str) and needle in value.casefold() for value in values)


@tool(namespace="datadog")
async def list_log_pipelines(query: str | None = None) -> list[dict]:
    """List log pipelines in processing order, keeping names or filters containing ``query``."""
    text = validation.optional_query(query)
    require_read()
    async with datadog_client() as client:
        items = await client.request("GET", "/api/v1/logs/config/pipelines")
        order = await client.request("GET", "/api/v1/logs/config/pipeline-order")
    ids = order.get("pipeline_ids") if isinstance(order, dict) else None
    if not isinstance(items, list) or not isinstance(ids, list):
        raise DatadogResponseError("Datadog returned a malformed pipeline list")
    needle = text.casefold() if text else None
    pipelines = [item for item in items if isinstance(item, dict)]
    return [
        pipeline_summary(item, position)
        for position, item in in_order(pipelines, ids, lambda item: item.get("id"))
        if _matches(needle, item.get("name"), (item.get("filter") or {}).get("query"))
    ]


@tool(namespace="datadog")
async def get_log_pipeline(pipeline_id: str) -> dict:
    """Return one log pipeline with every processor's configuration (grok rules included)."""
    identifier = validation.pipeline_id(pipeline_id)
    require_read()
    async with datadog_client() as client:
        data = await client.request("GET", f"/api/v1/logs/config/pipelines/{identifier}")
    if not isinstance(data, dict) or "id" not in data:
        raise DatadogResponseError("Datadog returned a malformed pipeline")
    return pipeline_detail(data)

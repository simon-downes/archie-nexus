"""Read-only log configuration: pipelines, indexes, log-based metrics and data scanning.

These endpoints need the application key's ``logs_read_config`` permission (the scanner needs
``data_scanner_read``); a missing permission surfaces as ``DatadogAuthorizationError``.
"""

from __future__ import annotations

from typing import Any

from archie_agent.exec.plugin import tool

from . import validation
from .client import DatadogResponseError, datadog_client
from .formatting import (
    in_order,
    log_index,
    log_metric,
    pipeline_detail,
    pipeline_summary,
    scanning_groups,
    standard_pattern_ids,
)
from .paging import collection
from .policy import require_read


def _matches(needle: str | None, *values: Any) -> bool:
    if needle is None:
        return True
    return any(isinstance(value, str) and needle in value.casefold() for value in values)


def _query_of(item: dict[str, Any]) -> Any:
    found = item.get("filter")
    return found.get("query") if isinstance(found, dict) else None


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
        if _matches(needle, item.get("name"), _query_of(item))
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


@tool(namespace="datadog")
async def list_log_indexes() -> list[dict]:
    """List log indexes in the order logs are routed, with retention and exclusion filters."""
    require_read()
    async with datadog_client() as client:
        data = await client.request("GET", "/api/v1/logs/config/indexes")
        order = await client.request("GET", "/api/v1/logs/config/index-order")
    items = data.get("indexes") if isinstance(data, dict) else None
    names = order.get("index_names") if isinstance(order, dict) else None
    if not isinstance(items, list) or not isinstance(names, list):
        raise DatadogResponseError("Datadog returned a malformed index list")
    indexes = [item for item in items if isinstance(item, dict)]
    return [
        log_index(item, position)
        for position, item in in_order(indexes, names, lambda item: item.get("name"))
    ]


@tool(namespace="datadog")
async def list_log_metrics(query: str | None = None) -> list[dict]:
    """List log-based metric definitions, keeping IDs or filters containing ``query``."""
    text = validation.optional_query(query)
    require_read()
    async with datadog_client() as client:
        data = await client.request("GET", "/api/v2/logs/config/metrics")
    needle = text.casefold() if text else None
    metrics = [log_metric(item) for item in collection(data)]
    return [item for item in metrics if _matches(needle, item["id"], item["filter"])]


@tool(namespace="datadog")
async def list_scanning_groups() -> list[dict]:
    """List Sensitive Data Scanner groups in scan order, each with its rules."""
    require_read()
    async with datadog_client() as client:
        config = await client.request("GET", "/api/v2/sensitive-data-scanner/config")
        if not isinstance(config, dict) or not isinstance(config.get("included", []), list):
            raise DatadogResponseError("Datadog returned a malformed scanner configuration")
        wanted = standard_pattern_ids(config)
        names: dict[str, str] = {}
        if wanted:
            patterns = await client.request(
                "GET", "/api/v2/sensitive-data-scanner/config/standard-patterns"
            )
            for item in collection(patterns):
                attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
                if item.get("id") in wanted and isinstance(attrs.get("name"), str):
                    names[item["id"]] = attrs["name"]
    return scanning_groups(config, names)

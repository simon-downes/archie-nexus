"""Metric timeseries queries and metric-name search."""

from __future__ import annotations

from archie_agent.exec.tools import tool

from . import validation
from .client import (
    MAX_ERROR,
    DatadogNotFoundError,
    DatadogRequestRejectedError,
    DatadogResponseError,
    datadog_client,
)
from .formatting import metric_description, metric_result
from .policy import require_read


@tool(native=False, exec_docs=False, namespace="datadog")
async def query_metrics(query: str, start: str = "1h", end: str | None = None) -> dict:
    """Run a Datadog metric query (for example ``avg:system.cpu.user{env:prod} by {host}``)."""
    text = validation.query(query)
    begin, finish = validation.time_window(start, end)
    require_read()
    params = {"query": text, "from": int(begin.timestamp()), "to": int(finish.timestamp())}
    async with datadog_client() as client:
        data = await client.request("GET", "/api/v1/query", params=params)
    if not isinstance(data, dict):
        raise DatadogResponseError("Datadog returned a malformed metric query response")
    error = data.get("error")
    if isinstance(error, str) and error.strip():
        raise DatadogRequestRejectedError(
            f"Datadog rejected the request: {error.strip()[:MAX_ERROR]}"
        )
    return metric_result(data)


@tool(native=False, exec_docs=False, namespace="datadog")
async def search_metrics(query: str, limit: int = 50) -> list[str]:
    """Return metric names containing ``query`` (for example ``aws.ec2``)."""
    text = validation.query(query)
    count = validation.limit(limit)
    require_read()
    async with datadog_client() as client:
        data = await client.request("GET", "/api/v1/search", params={"q": f"metrics:{text}"})
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, dict):
        raise DatadogResponseError("Datadog returned a malformed metric search response")
    # Datadog returns ``"metrics": null`` (or omits the key) when nothing matches.
    names = results.get("metrics")
    if names is None:
        return []
    if not isinstance(names, list):
        raise DatadogResponseError("Datadog returned a malformed metric search response")
    return [name for name in names if isinstance(name, str)][:count]


@tool(native=False, exec_docs=False, namespace="datadog")
async def describe_metric(name: str, max_values: int = 20) -> dict:
    """Describe one metric: type and unit, tag keys with sample values, and where it is used."""
    metric = validation.metric_name(name)
    per_key = validation.limit(max_values)
    require_read()
    async with datadog_client() as client:
        meta = await client.request("GET", f"/api/v1/metrics/{metric}")
        try:
            tags = await client.request("GET", f"/api/v2/metrics/{metric}/all-tags")
        except DatadogNotFoundError:
            tags = None
        assets = await client.request("GET", f"/api/v2/metrics/{metric}/assets")
    if not isinstance(meta, dict):
        raise DatadogResponseError("Datadog returned malformed metric metadata")
    return metric_description(metric, meta, tags, assets, per_key)

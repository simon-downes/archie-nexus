"""Broad discovery: one search term across monitors, dashboards, metrics, logs and spans.

Each source makes one or two read requests with its own client and a 25-second timeout, so a
slow or throttled source is reported in ``skipped`` instead of failing the whole search. Logs
and spans are aggregated by service: discovery never returns log or span content.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from archie_agent.exec.plugin import tool

from . import validation
from .client import (
    METRIC,
    DatadogAuthenticationError,
    DatadogConfigurationError,
    DatadogError,
    DatadogPolicyError,
    DatadogResponseError,
    DatadogTransportError,
    datadog_client,
)
from .metrics import list_metric_names, search_metric_names
from .paging import iso
from .policy import require_read

SOURCES = ("monitors", "dashboards", "metrics", "logs", "spans")
DEFAULT_SOURCES = ("monitors", "dashboards", "metrics", "logs")
SOURCE_TIMEOUT = 25
MAX_LIMIT = 50
_FATAL = (DatadogPolicyError, DatadogConfigurationError, DatadogAuthenticationError)
_WORD = re.compile(r"^[A-Za-z0-9_.-]+$")
_TAG_QUERY = re.compile(r"^[^\s:]+:\S+$")
# A metric after its aggregator and before its scope, e.g. ``max:aws.amazonmq.x{broker:dev}`` or
# ``p95:trace.http.request{service:api}``. Requiring the ``<aggregator>:`` prefix skips
# ``by {host}`` groupings and tag values inside scopes.
_QUERY_METRIC = re.compile(
    rf"\b(?:avg|sum|min|max|count|p[0-9]{{1,3}}(?:\.[0-9]+)?):({METRIC})\s*\{{"
)
_METRIC_MONITOR_TYPES = frozenset({"query alert", "metric alert"})

Result = dict[str, Any]


def _result(
    kind: str,
    identifier: Any,
    title: Any,
    source: str,
    matched_on: str,
    handoff: str,
    count: Any = None,
) -> Result:
    return {
        "type": kind,
        "id": identifier,
        "title": title,
        "source": source,
        "matched_on": matched_on,
        "count": count,
        "handoff": handoff,
    }


def query_metrics_in(query: Any) -> list[str]:
    """Return metric names referenced in a monitor query, in order and without duplicates."""
    if not isinstance(query, str):
        return []
    found: list[str] = []
    for match in _QUERY_METRIC.finditer(query):
        name = match.group(1)
        if name not in found:
            found.append(name)
    return found


async def _monitors(query: str, limit: int) -> tuple[list[Result], list[Result]]:
    """Matching monitors, plus the metrics their queries use."""
    async with datadog_client() as client:
        data = await client.request(
            "GET", "/api/v1/monitor/search", params={"query": query, "per_page": limit, "page": 0}
        )
    items = data.get("monitors") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise DatadogResponseError("Datadog returned a malformed monitor search response")
    monitors: list[Result] = []
    metrics: list[Result] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        monitors.append(
            _result(
                "monitor",
                item.get("id"),
                item.get("name"),
                "monitors",
                "monitor search",
                "datadog.get_monitor",
            )
        )
        if item.get("type") in _METRIC_MONITOR_TYPES:
            for name in query_metrics_in(item.get("query")):
                metrics.append(
                    _result(
                        "metric",
                        name,
                        name,
                        "monitors",
                        f"monitor {item.get('id')}",
                        "datadog.describe_metric",
                    )
                )
    return monitors, metrics


async def _dashboards(query: str, limit: int) -> list[Result]:
    async with datadog_client() as client:
        data = await client.request("GET", "/api/v1/dashboard")
    items = data.get("dashboards") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise DatadogResponseError("Datadog returned a malformed dashboard list")
    needle = query.casefold()
    found: list[Result] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if needle in str(item.get("title") or "").casefold():
            matched = "title"
        elif needle in str(item.get("description") or "").casefold():
            matched = "description"
        else:
            continue
        found.append(
            _result(
                "dashboard",
                item.get("id"),
                item.get("title"),
                "dashboards",
                matched,
                "datadog.get_dashboard",
            )
        )
        if len(found) >= limit:
            break
    return found


async def _metrics(query: str, limit: int) -> list[Result]:
    """Metric names containing the query; for a ``key:value`` query, metrics with that tag.

    Datadog's name search rejects a ``:`` (HTTP 400), and metric names cannot contain one. So a
    single ``key:value`` query uses the tag listing, and any other query with ``:`` (such as
    ``service:api env:prod``) finds no metrics without sending a request bound to fail.
    """
    if _TAG_QUERY.fullmatch(query):
        names = [(name, "tag") for name in sorted(await list_metric_names(query, None))]
    elif ":" in query:
        return []
    else:
        names = [(name, "name") for name in await search_metric_names(query)]
    found: list[Result] = []
    seen: set[str] = set()
    for name, matched in names:
        if name in seen:
            continue
        seen.add(name)
        found.append(_result("metric", name, name, "metrics", matched, "datadog.describe_metric"))
        if len(found) >= limit:
            break
    return found


def _service_results(
    buckets: list[tuple[Any, Any]], source: str, matched: str, handoff: str, limit: int
) -> list[Result]:
    found: list[Result] = []
    for service, count in buckets:
        if not isinstance(service, str) or not service:
            continue
        found.append(_result("service", service, service, source, matched, handoff, count))
        if len(found) >= limit:
            break
    return found


async def _logs(query: str, begin: datetime, end: datetime, limit: int) -> list[Result]:
    body = {
        "filter": {"query": query, "from": iso(begin), "to": iso(end)},
        "compute": [{"aggregation": "count"}],
        "group_by": [{"facet": "service", "limit": limit}],
    }
    async with datadog_client() as client:
        data = await client.request("POST", "/api/v2/logs/analytics/aggregate", json=body)
    inner = data.get("data") if isinstance(data, dict) else None
    buckets = inner.get("buckets") if isinstance(inner, dict) else None
    if not isinstance(buckets, list):
        raise DatadogResponseError("Datadog returned a malformed log aggregate")
    pairs = [
        ((item.get("by") or {}).get("service"), (item.get("computes") or {}).get("c0"))
        for item in buckets
        if isinstance(item, dict)
        and isinstance(item.get("by") or {}, dict)
        and isinstance(item.get("computes") or {}, dict)
    ]
    return _service_results(pairs, "logs", "log content", "datadog.search_logs", limit)


def span_query(query: str) -> str:
    """Widen a single bare word to service and resource matches; pass anything else through."""
    if _WORD.fullmatch(query):
        return f"service:*{query}* OR resource_name:*{query}*"
    return query


async def _spans(query: str, begin: datetime, end: datetime, limit: int) -> list[Result]:
    body = {
        "data": {
            "type": "aggregate_request",
            "attributes": {
                "filter": {"query": span_query(query), "from": iso(begin), "to": iso(end)},
                "compute": [{"aggregation": "count"}],
                "group_by": [{"facet": "service", "limit": limit}],
            },
        }
    }
    async with datadog_client() as client:
        data = await client.request("POST", "/api/v2/spans/analytics/aggregate", json=body)
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise DatadogResponseError("Datadog returned a malformed span aggregate")
    pairs = []
    for item in items:
        attrs = item.get("attributes") if isinstance(item, dict) else None
        if not isinstance(attrs, dict):
            continue
        by = attrs.get("by") if isinstance(attrs.get("by"), dict) else {}
        compute = attrs.get("compute") if isinstance(attrs.get("compute"), dict) else {}
        pairs.append((by.get("service"), compute.get("c0")))
    return _service_results(pairs, "spans", "span attributes", "datadog.search_spans", limit)


async def _bounded(run: Callable[[], Awaitable[Any]]) -> Any:
    """Run one source under its timeout, mapping every failure to a ``DatadogError``."""
    try:
        async with asyncio.timeout(SOURCE_TIMEOUT):
            return await run()
    except DatadogError:
        raise
    except TimeoutError as exc:
        raise DatadogTransportError(
            f"Datadog source exceeded {SOURCE_TIMEOUT}s", transmitted=True
        ) from exc
    except Exception as exc:
        raise DatadogResponseError("Datadog returned an unexpected response") from exc


@tool(namespace="datadog")
async def search(
    query: str,
    sources: list[str] | None = None,
    start: str = "1h",
    limit: int = 10,
) -> dict:
    """Search monitors, dashboards, metrics and log (or span) services for one term."""
    text = validation.query(query)
    selected = validation.sources(sources, SOURCES, DEFAULT_SOURCES)
    begin, end = validation.time_window(start, None)
    count = validation.limit(limit, MAX_LIMIT)
    require_read()
    runners: dict[str, Callable[[], Awaitable[Any]]] = {
        "monitors": lambda: _monitors(text, count),
        "dashboards": lambda: _dashboards(text, count),
        "metrics": lambda: _metrics(text, count),
        "logs": lambda: _logs(text, begin, end, count),
        "spans": lambda: _spans(text, begin, end, count),
    }
    outcomes = await asyncio.gather(
        *(_bounded(runners[name]) for name in selected), return_exceptions=True
    )
    failures = {
        name: outcome
        for name, outcome in zip(selected, outcomes, strict=True)
        if isinstance(outcome, BaseException)
    }
    for outcome in failures.values():
        if isinstance(outcome, _FATAL):
            raise outcome
    for outcome in failures.values():
        if not isinstance(outcome, Exception):
            raise outcome  # CancelledError and friends are never swallowed.
    if len(failures) == len(selected):
        raise next(iter(failures.values()))
    by_source = dict(zip(selected, outcomes, strict=True))
    results: list[Result] = []
    extracted: list[Result] = []
    for name in selected:
        value = by_source[name]
        if name in failures:
            continue
        if name == "monitors":
            monitors, extracted = value
            results.extend(monitors)
        else:
            results.extend(value)
    seen = {item["id"] for item in results if item["type"] == "metric"}
    extra: list[Result] = []
    for item in extracted:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        extra.append(item)
        if len(extra) >= count:
            break
    results.extend(extra)
    skipped = {name: f"{type(error).__name__}: {error}" for name, error in failures.items()}
    return {"results": results, "skipped": skipped}

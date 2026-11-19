"""Shared helpers for Datadog v2 search and list endpoints."""

from __future__ import annotations

from typing import Any

from .client import DatadogClient, DatadogResponseError
from .validation import Aggregate

_MAX_PAGES = 5


def iso(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def aggregate_attributes(spec: Aggregate) -> dict[str, Any]:
    """The ``filter``, ``compute`` and ``group_by`` shared by log and span aggregate requests."""
    compute: dict[str, Any] = {"aggregation": spec.function, "type": "total"}
    sort: dict[str, Any] = {"type": "measure", "aggregation": spec.function, "order": spec.order}
    if spec.metric:
        compute["metric"] = spec.metric
        sort["metric"] = spec.metric
    if spec.interval:
        compute.update(type="timeseries", interval=spec.interval)
    return {
        "filter": {"query": spec.query, "from": iso(spec.begin), "to": iso(spec.end)},
        "compute": [compute],
        "group_by": [
            {key: value for key, value in {**group, "sort": sort}.items() if value is not None}
            for group in spec.groups
        ],
    }


def meta_warnings(meta: dict[str, Any]) -> list[str]:
    return [
        str(item.get("title") or item.get("code"))[:200]
        for item in meta.get("warnings") or []
        if isinstance(item, dict)
    ]


def aggregate_result(
    spec: Aggregate, buckets: list[dict], partial: bool, warnings: list[str]
) -> dict[str, Any]:
    return {
        "from": iso(spec.begin),
        "to": iso(spec.end),
        "group_by": [group["facet"] for group in spec.groups],
        "bucket_count": len(buckets),
        "buckets": buckets,
        "partial": partial,
        "warnings": warnings[:10],
    }


def collection(value: Any) -> list[dict]:
    """Return the dict items under ``data``; anything else is a malformed response."""
    data = value.get("data") if isinstance(value, dict) else None
    if not isinstance(data, list):
        raise DatadogResponseError("Datadog returned a malformed collection")
    return [item for item in data if isinstance(item, dict)]


def _after(value: dict) -> str | None:
    meta = value.get("meta")
    page = meta.get("page") if isinstance(meta, dict) else None
    after = page.get("after") if isinstance(page, dict) else None
    return after if isinstance(after, str) and after else None


async def search_pages(
    client: DatadogClient, path: str, body: dict[str, Any], limit: int
) -> list[dict]:
    """POST a v2 search body, following ``meta.page.after`` until ``limit`` items."""
    results: list[dict] = []
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        page: dict[str, Any] = {"limit": limit - len(results)}
        if cursor:
            page["cursor"] = cursor
        value = await client.request("POST", path, json={**body, "page": page})
        items = collection(value)
        results.extend(items)
        cursor = _after(value)
        if len(results) >= limit or not items or not cursor:
            break
    return results[:limit]


async def list_pages(
    client: DatadogClient, path: str, params: dict[str, Any], limit: int
) -> list[dict]:
    """GET a v2 list with ``page[limit]``/``page[cursor]``, following ``meta.page.after``."""
    results: list[dict] = []
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        query = {**params, "page[limit]": limit - len(results)}
        if cursor:
            query["page[cursor]"] = cursor
        value = await client.request("GET", path, params=query)
        items = collection(value)
        results.extend(items)
        cursor = _after(value)
        if len(results) >= limit or not items or not cursor:
            break
    return results[:limit]

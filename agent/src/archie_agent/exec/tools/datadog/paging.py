"""Shared helpers for Datadog v2 search and list endpoints."""

from __future__ import annotations

from typing import Any

from .client import DatadogClient, DatadogResponseError

_MAX_PAGES = 5


def iso(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _data(value: Any) -> list[dict]:
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
        items = _data(value)
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
        items = _data(value)
        results.extend(items)
        cursor = _after(value)
        if len(results) >= limit or not items or not cursor:
            break
    return results[:limit]


def collection(value: Any) -> list[dict]:
    return _data(value)

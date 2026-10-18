"""Complete Slack directory record lists in the plugin cache (``{"records": [...]}``, D14)."""

from __future__ import annotations

from archie_agent.exec.tools.cache import ToolCache


def cached_records(cache: ToolCache, key: str) -> list[dict] | None:
    """Return the cached record list for ``key``, or None on a miss, expiry or bad entry."""
    entry = cache.get(key)
    if not isinstance(entry, dict) or not isinstance(entry.get("records"), list):
        return None
    return entry["records"]


def store_records(cache: ToolCache, key: str, records: list[dict], *, ttl: float) -> None:
    """Cache a complete record list; one over the cache size limit is just not cached."""
    try:
        cache.set(key, {"records": records}, ttl)
    except ValueError:
        return

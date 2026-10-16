"""Normalisers that turn Datadog payloads into bounded, model-friendly records."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .validation import OWNERSHIP_TAG

MAX_TEXT = 4_000
MAX_MESSAGE = 2_000
MAX_TAGS = 50
MAX_SERIES = 20
MAX_POINTS = 300


def text(value: Any, maximum: int = MAX_TEXT) -> tuple[str | None, bool]:
    """Return ``(text, truncated)`` for a string value; non-strings become ``None``."""
    if not isinstance(value, str):
        return None, False
    if len(value) <= maximum:
        return value, False
    return value[:maximum], True


def bounded(values: Any, maximum: int = MAX_TAGS) -> tuple[list, bool]:
    if not isinstance(values, list):
        return [], False
    return values[:maximum], len(values) > maximum


def iso_ms(value: Any) -> str | None:
    """Epoch milliseconds to an ISO-8601 UTC string."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, UTC).isoformat().replace("+00:00", "Z")


def metric_series(series: dict[str, Any]) -> dict[str, Any]:
    points = [
        [iso_ms(point[0]), point[1]]
        for point in series.get("pointlist") or []
        if isinstance(point, list) and len(point) == 2
    ]
    unit = series.get("unit")
    units = (
        [item.get("name") for item in unit if isinstance(item, dict)]
        if isinstance(unit, list)
        else []
    )
    tag_set, tags_truncated = bounded(series.get("tag_set"))
    return {
        "metric": series.get("metric"),
        "display_name": series.get("display_name"),
        "expression": series.get("expression"),
        "scope": series.get("scope"),
        "tags": tag_set,
        "unit": next((name for name in units if name), None),
        "interval_seconds": series.get("interval"),
        "point_count": len(points),
        "points": points[-MAX_POINTS:],
        "truncated": len(points) > MAX_POINTS or tags_truncated,
    }


def metric_result(data: dict[str, Any]) -> dict[str, Any]:
    raw = [item for item in data.get("series") or [] if isinstance(item, dict)]
    return {
        "query": data.get("query"),
        "from": iso_ms(data.get("from_date")),
        "to": iso_ms(data.get("to_date")),
        "series_count": len(raw),
        "series": [metric_series(item) for item in raw[:MAX_SERIES]],
        "truncated": len(raw) > MAX_SERIES,
    }


def has_ownership_tag(tags: Any) -> bool:
    return isinstance(tags, list) and OWNERSHIP_TAG in tags

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
MAX_ATTRIBUTES = 30
MAX_ATTRIBUTE = 200
MAX_SPAN_ERROR = 500
MAX_TITLE = 500


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


def _scalar_attributes(values: Any) -> tuple[dict[str, Any], bool]:
    """Top-level keys only, at most 30; long strings cut; nested values become ``"…"``."""
    if not isinstance(values, dict):
        return {}, False
    result: dict[str, Any] = {}
    truncated = len(values) > MAX_ATTRIBUTES
    for key, value in list(values.items())[:MAX_ATTRIBUTES]:
        if isinstance(value, str):
            cut, was_cut = text(value, MAX_ATTRIBUTE)
            result[str(key)] = cut
            truncated = truncated or was_cut
        elif value is None or isinstance(value, (bool, int, float)):
            result[str(key)] = value
        else:
            result[str(key)] = "…"
    return result, truncated


def log(item: dict[str, Any]) -> dict[str, Any]:
    attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    message, message_cut = text(attrs.get("message"), MAX_MESSAGE)
    tags, tags_cut = bounded(attrs.get("tags"))
    extra, extra_cut = _scalar_attributes(attrs.get("attributes"))
    return {
        "id": item.get("id"),
        "timestamp": attrs.get("timestamp"),
        "service": attrs.get("service"),
        "host": attrs.get("host"),
        "status": attrs.get("status"),
        "message": message,
        "tags": tags,
        "attributes": extra,
        "truncated": message_cut or tags_cut or extra_cut,
    }


def span(item: dict[str, Any]) -> dict[str, Any]:
    attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    custom = attrs.get("custom") if isinstance(attrs.get("custom"), dict) else {}
    error = custom.get("error") if isinstance(custom.get("error"), dict) else {}
    duration = custom.get("duration")
    parts = [error.get("type"), error.get("message")]
    error_text, _ = text(": ".join(str(part) for part in parts if part), MAX_SPAN_ERROR)
    return {
        "trace_id": attrs.get("trace_id"),
        "span_id": attrs.get("span_id"),
        "parent_id": attrs.get("parent_id"),
        "service": attrs.get("service"),
        "resource": attrs.get("resource_name"),
        "operation": attrs.get("operation_name"),
        "env": attrs.get("env"),
        "start": attrs.get("start_timestamp"),
        "end": attrs.get("end_timestamp"),
        "duration_ms": (
            round(duration / 1_000_000, 3)
            if isinstance(duration, (int, float)) and not isinstance(duration, bool)
            else None
        ),
        "status": attrs.get("status"),
        "error": error_text or None,
    }


def event(item: dict[str, Any]) -> dict[str, Any]:
    attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    inner = attrs.get("attributes") if isinstance(attrs.get("attributes"), dict) else {}
    evt = inner.get("evt") if isinstance(inner.get("evt"), dict) else {}
    message, message_cut = text(attrs.get("message"), MAX_MESSAGE)
    title, title_cut = text(attrs.get("title") or inner.get("title"), MAX_TITLE)
    tags, tags_cut = bounded(attrs.get("tags"))
    return {
        "id": item.get("id"),
        "timestamp": attrs.get("timestamp"),
        "title": title,
        "message": message,
        "source": inner.get("source_type_name") or evt.get("type"),
        "tags": tags,
        "truncated": message_cut or title_cut or tags_cut,
    }

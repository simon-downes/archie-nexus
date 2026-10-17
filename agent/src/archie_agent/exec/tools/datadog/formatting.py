"""Normalisers that turn Datadog payloads into bounded, model-friendly records."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .validation import MAX_TAGS, MAX_TEXT, MONITOR_OPTIONS, OWNERSHIP_TAG

MAX_MESSAGE = 2_000
MAX_SERIES = 20
MAX_POINTS = 300
MAX_ATTRIBUTES = 30
MAX_ATTRIBUTE = 200
MAX_SPAN_ERROR = 500
MAX_TITLE = 500
MAX_WIDGETS = 100
MAX_WIDGET_QUERIES = 10
MAX_WIDGET_QUERY = 500
MAX_TAG_KEYS = 100
MAX_ASSETS = 50


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
    error_text, error_cut = text(": ".join(str(part) for part in parts if part), MAX_SPAN_ERROR)
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
        "truncated": error_cut,
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


def _epoch_or_iso(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")
    return None


def monitor(item: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    tags, tags_cut = bounded(item.get("tags"))
    query, query_cut = text(item.get("query"))
    record = {
        "id": item.get("id"),
        "name": item.get("name"),
        "type": item.get("type"),
        "status": item.get("overall_state") or item.get("status"),
        "query": query,
        "tags": tags,
        "priority": item.get("priority"),
        "managed": has_ownership_tag(item.get("tags")),
        "created": _epoch_or_iso(item.get("created")),
        "modified": _epoch_or_iso(item.get("modified")),
    }
    truncated = tags_cut or query_cut
    if detail:
        message, message_cut = text(item.get("message"))
        options = item.get("options") if isinstance(item.get("options"), dict) else {}
        record["message"] = message
        record["options"] = {key: options[key] for key in sorted(MONITOR_OPTIONS) if key in options}
        truncated = truncated or message_cut
    record["truncated"] = truncated
    return record


def dashboard_summary(item: dict[str, Any]) -> dict[str, Any]:
    description, cut = text(item.get("description"), MAX_TITLE)
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "description": description,
        "layout_type": item.get("layout_type"),
        "author": item.get("author_handle"),
        "created_at": item.get("created_at"),
        "modified_at": item.get("modified_at"),
        "truncated": cut,
    }


def _widget_queries(definition: dict[str, Any]) -> tuple[list[str], bool]:
    found: list[str] = []
    requests = definition.get("requests")
    if isinstance(requests, dict):
        requests = list(requests.values())
    for request in requests if isinstance(requests, list) else []:
        if not isinstance(request, dict):
            continue
        if isinstance(request.get("q"), str):
            found.append(request["q"])
        for query in request.get("queries") or []:
            if not isinstance(query, dict):
                continue
            search = query.get("search") if isinstance(query.get("search"), dict) else {}
            value = query.get("query") or search.get("query")
            if isinstance(value, str):
                found.append(value)
    if isinstance(definition.get("query"), str):
        found.append(definition["query"])
    cut = [text(value, MAX_WIDGET_QUERY) for value in found[:MAX_WIDGET_QUERIES]]
    truncated = len(found) > MAX_WIDGET_QUERIES or any(was_cut for _, was_cut in cut)
    return [value for value, _ in cut], truncated


def widget(item: dict[str, Any]) -> dict[str, Any]:
    definition = item.get("definition") if isinstance(item.get("definition"), dict) else {}
    title, title_cut = text(definition.get("title"), MAX_TITLE)
    queries, queries_cut = _widget_queries(definition)
    record = {
        "id": item.get("id"),
        "type": definition.get("type"),
        "title": title,
        "queries": queries,
        "truncated": title_cut or queries_cut,
    }
    if definition.get("type") == "group":
        children = definition.get("widgets")
        record["child_count"] = len(children) if isinstance(children, list) else 0
    return record


def dashboard_detail(item: dict[str, Any]) -> dict[str, Any]:
    widgets = [value for value in item.get("widgets") or [] if isinstance(value, dict)]
    variables = [
        value.get("name")
        for value in item.get("template_variables") or []
        if isinstance(value, dict) and isinstance(value.get("name"), str)
    ]
    record = dashboard_summary(item)
    record.update(
        {
            "url": item.get("url"),
            "template_variables": variables[:MAX_TAGS],
            "widget_count": len(widgets),
            "widgets": [widget(value) for value in widgets[:MAX_WIDGETS]],
        }
    )
    record["truncated"] = (
        record["truncated"] or len(widgets) > MAX_WIDGETS or len(variables) > MAX_TAGS
    )
    return record


def _grouped_tags(tags: Any, max_values: int) -> tuple[dict[str, dict[str, Any]], int, bool]:
    """Group ``key:value`` tags by key; a bare tag becomes a key with no values.

    The returned count is the number of distinct tags.
    """
    unique = (
        {tag for tag in tags if isinstance(tag, str) and tag} if isinstance(tags, list) else set()
    )
    groups: dict[str, list[str]] = {}
    for tag in unique:
        key, _, value = tag.partition(":")
        values = groups.setdefault(key, [])
        if value:
            values.append(value)
    truncated = len(groups) > MAX_TAG_KEYS
    result: dict[str, dict[str, Any]] = {}
    for key in sorted(groups)[:MAX_TAG_KEYS]:
        values = sorted(groups[key])
        truncated = truncated or len(values) > max_values
        result[key] = {"values": values[:max_values], "value_count": len(values)}
    return result, len(unique), truncated


def _assets(assets: Any, kind: str) -> tuple[list[dict[str, Any]], bool]:
    included = assets.get("included") if isinstance(assets, dict) else None
    found = []
    for item in included if isinstance(included, list) else []:
        if not isinstance(item, dict) or item.get("type") != kind:
            continue
        attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
        found.append({"id": item.get("id"), "title": attrs.get("title")})
    return found[:MAX_ASSETS], len(found) > MAX_ASSETS


def metric_description(
    name: str, meta: Any, tags: Any, assets: Any, max_values: int
) -> dict[str, Any]:
    """Combine metric metadata, its tags and the dashboards and monitors that use it."""
    meta = meta if isinstance(meta, dict) else {}
    attrs = (tags.get("data") or {}).get("attributes") if isinstance(tags, dict) else None
    grouped, tag_count, tags_cut = _grouped_tags(
        attrs.get("tags") if isinstance(attrs, dict) else None, max_values
    )
    description, description_cut = text(meta.get("description"), MAX_TITLE)
    dashboards, dashboards_cut = _assets(assets, "dashboards")
    monitors, monitors_cut = _assets(assets, "monitors")
    return {
        "name": name,
        "type": meta.get("type"),
        "unit": meta.get("unit"),
        "per_unit": meta.get("per_unit"),
        "description": description,
        "integration": meta.get("integration"),
        "tags": grouped,
        "tag_count": tag_count,
        "dashboards": dashboards,
        "monitors": monitors,
        "truncated": tags_cut or description_cut or dashboards_cut or monitors_cut,
    }


def catalog_entity(item: dict[str, Any]) -> dict[str, Any]:
    attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    tags, tags_cut = bounded(attrs.get("tags"))
    return {
        "id": item.get("id"),
        "kind": attrs.get("kind"),
        "name": attrs.get("name"),
        "display_name": attrs.get("displayName"),
        "owner": attrs.get("owner"),
        "tags": tags,
        "truncated": tags_cut,
    }


def team(item: dict[str, Any]) -> dict[str, Any]:
    attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    return {
        "id": item.get("id"),
        "handle": attrs.get("handle"),
        "name": attrs.get("name"),
        "member_count": attrs.get("user_count"),
    }

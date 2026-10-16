"""Provider-to-model normalisers. Every output is bounded and free of raw payloads."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

MAX_TEXT = 8000
MAX_NESTED = 50


def text(value: Any, limit: int = MAX_TEXT) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    return str(value)[:limit]


def gmt(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return text(value, 64)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value[:MAX_NESTED] if isinstance(value, list) else []


def name(value: Any) -> str | None:
    return text(_dict(value).get("name"), 500)


def user(value: Any) -> dict[str, Any] | None:
    """Normalise a user, accepting both ``{"user": {...}}`` actors and bare users."""
    data = _dict(value)
    if isinstance(data.get("user"), dict):
        data = data["user"]
    if not data.get("id") and not data.get("name"):
        return None
    return {
        "id": text(data.get("id"), 100),
        "name": text(data.get("name"), 500),
        "email": text(data.get("email"), 320),
        "slack_user_id": text(data.get("slack_user_id"), 100),
    }


def actor(value: Any) -> dict[str, Any] | None:
    """Normalise a creator/updater, which may be a user, API key or workflow."""
    data = _dict(value)
    found = user(data)
    if found:
        return found
    for kind in ("api_key", "workflow", "alert"):
        if isinstance(data.get(kind), dict):
            return {"type": kind, "name": name(data[kind])}
    return None


def status(value: Any) -> dict[str, Any] | None:
    data = _dict(value)
    if not data:
        return None
    return {"name": text(data.get("name"), 500), "category": text(data.get("category"), 64)}


def _custom_value(value: Any) -> Any:
    data = _dict(value)
    if "value_text" in data:
        return text(data["value_text"])
    if "value_numeric" in data:
        return text(data["value_numeric"], 64)
    for key in ("value_option", "value_catalog_entry"):
        if isinstance(data.get(key), dict):
            return text(data[key].get("value") or data[key].get("name"), 500)
    if "value_link" in data:
        return text(data["value_link"], 2000)
    if isinstance(data.get("value_timestamp"), str):
        return gmt(data["value_timestamp"])
    return None


def incident_summary(item: dict[str, Any]) -> dict[str, Any]:
    reference = text(item.get("reference"), 64)
    return {
        "id": text(item.get("id"), 100),
        "reference": reference,
        "name": text(item.get("name"), 1000),
        "status": status(item.get("incident_status")),
        "severity": name(item.get("severity")),
        "type": name(item.get("incident_type")),
        "mode": text(item.get("mode"), 64),
        "visibility": text(item.get("visibility"), 64),
        "created_at": gmt(item.get("created_at")),
        "updated_at": gmt(item.get("updated_at")),
        "last_activity_at": gmt(item.get("last_activity_at")),
        "slack_channel_name": text(item.get("slack_channel_name"), 200),
        "permalink": text(item.get("permalink"), 2000),
    }


def incident_detail(
    item: dict[str, Any],
    participants: dict[str, Any] | None = None,
    alert_ids: list[str] | None = None,
) -> dict[str, Any]:
    roles = []
    for entry in _list(item.get("incident_role_assignments")):
        entry = _dict(entry)
        roles.append({"role": name(entry.get("role")), "assignee": user(entry.get("assignee"))})
    fields = []
    for entry in _list(item.get("custom_field_entries")):
        entry = _dict(entry)
        values = [_custom_value(value) for value in _list(entry.get("values"))]
        fields.append(
            {
                "name": name(entry.get("custom_field")),
                "values": [value for value in values if value is not None],
            }
        )
    timestamps = []
    for entry in _list(item.get("incident_timestamp_values")):
        entry = _dict(entry)
        value = gmt(_dict(entry.get("value")).get("value"))
        timestamps.append({"name": name(entry.get("incident_timestamp")), "value": value})
    durations = []
    for entry in _list(item.get("duration_metrics")):
        entry = _dict(entry)
        durations.append(
            {
                "name": name(entry.get("duration_metric")),
                "seconds": entry.get("value_seconds")
                if isinstance(entry.get("value_seconds"), (int, float))
                else None,
            }
        )
    people = _dict(participants)
    return {
        **incident_summary(item),
        "summary": text(item.get("summary")),
        "creator": actor(item.get("creator")),
        "role_assignments": roles,
        "custom_fields": fields,
        "timestamps": timestamps,
        "durations": durations,
        "slack_channel_url": text(item.get("slack_channel_url"), 2000),
        "call_url": text(item.get("call_url"), 2000),
        "postmortem_document_ids": [
            text(value, 100) for value in _list(item.get("postmortem_document_ids"))
        ],
        "participants": {
            "active": [user(p) for p in _list(people.get("active")) if user(p)],
            "passive": [user(p) for p in _list(people.get("passive")) if user(p)],
        },
        "alert_ids": list(alert_ids or [])[:MAX_NESTED],
    }


def timeline_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": text(item.get("id"), 100),
        "timestamp": gmt(item.get("timestamp")),
        "title": text(item.get("title"), 2000),
        "description": text(item.get("description")),
        "creator": actor(item.get("creator")),
    }


def incident_update(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": text(item.get("id"), 100),
        "created_at": gmt(item.get("created_at")),
        "updater": actor(item.get("updater")),
        "message": text(item.get("message")),
        "new_status": status(item.get("new_incident_status")),
        "new_severity": name(item.get("new_severity")),
    }


def _attributes(value: Any) -> list[dict[str, Any]]:
    result = []
    for entry in _list(value):
        entry = _dict(entry)
        raw = entry.get("value") or entry.get("array_value")
        items = raw if isinstance(raw, list) else [raw]
        values = []
        for item in items[:MAX_NESTED]:
            item = _dict(item)
            label = item.get("label") or item.get("literal")
            if label is not None:
                values.append(text(label, 1000))
        result.append({"name": name(entry.get("attribute")), "values": values})
    return result


def alert(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": text(item.get("id"), 100),
        "title": text(item.get("title"), 2000),
        "status": text(item.get("status"), 64),
        "alert_source_id": text(item.get("alert_source_id"), 100),
        "deduplication_key": text(item.get("deduplication_key"), 500),
        "source_url": text(item.get("source_url"), 2000),
        "created_at": gmt(item.get("created_at")),
        "updated_at": gmt(item.get("updated_at")),
        "resolved_at": gmt(item.get("resolved_at")),
        "tags": [text(tag, 200) for tag in _list(item.get("tags")) if isinstance(tag, str)],
    }


def alert_detail(item: dict[str, Any], incident_ids: list[str]) -> dict[str, Any]:
    return {
        **alert(item),
        "description": text(item.get("description")),
        "attributes": _attributes(item.get("attributes")),
        "incident_ids": incident_ids[:MAX_NESTED],
    }


def reference_item(item: dict[str, Any]) -> dict[str, Any]:
    result = {
        "id": text(item.get("id"), 100),
        "name": text(item.get("name"), 500),
        "description": text(item.get("description"), 2000),
    }
    for key in ("rank", "category", "field_type", "shortform", "role_type", "is_default"):
        if key in item and isinstance(item[key], (str, int, bool)):
            result[key] = item[key] if not isinstance(item[key], str) else text(item[key], 200)
    return result

"""Local input validation. Every helper raises DatadogInputError before any request."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from .client import DatadogInputError

MAX_QUERY = 2_000
MAX_TEXT = 4_000
_RELATIVE = re.compile(r"^([1-9][0-9]{0,5})([mhd])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}
_MAX_WINDOW = timedelta(days=31)
_DASHBOARD = re.compile(r"^[a-z0-9]{3}-[a-z0-9]{3}-[a-z0-9]{3}$")
_KIND = re.compile(r"^[a-z0-9_.-]{1,64}$")
_MAX_TAGS = 50
_MAX_TAG = 200

OWNERSHIP_TAG = "managedby:archie"

MONITOR_TYPES = frozenset(
    {
        "composite",
        "event alert",
        "log alert",
        "metric alert",
        "process alert",
        "query alert",
        "rum alert",
        "service check",
        "synthetics alert",
        "trace-analytics alert",
        "slo alert",
        "event-v2 alert",
        "audit alert",
        "ci-pipelines alert",
        "ci-tests alert",
        "error-tracking alert",
        "database-monitoring alert",
        "network-performance alert",
        "cost alert",
        "data-quality alert",
        "network-path alert",
        "data-jobs alert",
        "llm-observability alert",
    }
)

MONITOR_OPTIONS = frozenset(
    {
        "thresholds",
        "notify_no_data",
        "no_data_timeframe",
        "renotify_interval",
        "evaluation_delay",
        "new_group_delay",
        "require_full_window",
        "include_tags",
        "notify_audit",
        "escalation_message",
        "timeout_h",
    }
)


def limit(value: Any, maximum: int = 100) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise DatadogInputError(f"limit must be an integer 1-{maximum}")
    return value


def query(value: Any, name: str = "query") -> str:
    """A required query string: non-empty after trimming, at most 2,000 characters."""
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_QUERY:
        raise DatadogInputError(f"{name} must be 1-{MAX_QUERY} characters")
    return value.strip()


def optional_query(value: Any, name: str = "query") -> str | None:
    """An optional query string; ``None`` or blank means no filter."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_QUERY:
        raise DatadogInputError(f"{name} must be a string of at most {MAX_QUERY} characters")
    return value.strip() or None


def text(value: Any, name: str, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise DatadogInputError(f"{name} must be 1-{maximum} characters")
    return value


def choice(value: Any, name: str, options: frozenset[str] | set[str]) -> str:
    if not isinstance(value, str) or value not in options:
        raise DatadogInputError(f"{name} must be one of: {', '.join(sorted(options))}")
    return value


def _instant(value: Any, name: str, now: datetime) -> datetime:
    if isinstance(value, str):
        stripped = value.strip()
        match = _RELATIVE.fullmatch(stripped)
        if match:
            return now - timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})
        try:
            parsed = datetime.fromisoformat(stripped)
        except ValueError:
            pass
        else:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    raise DatadogInputError(
        f"{name} must be an ISO-8601 datetime or a relative age such as 15m, 2h or 1d"
    )


def time_window(start: Any, end: Any, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Resolve ``start``/``end`` to aware datetimes; ``end=None`` means now."""
    current = now or datetime.now(UTC)
    begin = _instant(start, "start", current)
    finish = current if end is None else _instant(end, "end", current)
    if finish <= begin:
        raise DatadogInputError("end must be after start")
    if finish - begin > _MAX_WINDOW:
        raise DatadogInputError("time window must be at most 31 days")
    return begin, finish


def monitor_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 10**19:
        raise DatadogInputError("monitor_id must be a positive integer")
    return value


def dashboard_id(value: Any) -> str:
    if not isinstance(value, str) or not _DASHBOARD.fullmatch(value.strip()):
        raise DatadogInputError("dashboard_id must look like abc-def-ghi")
    return value.strip()


def kind(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _KIND.fullmatch(value.strip()):
        raise DatadogInputError("kind must be 1-64 lowercase letters, digits, '_', '.' or '-'")
    return value.strip()


def tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_TAGS:
        raise DatadogInputError(f"tags must be a list of at most {_MAX_TAGS} strings")
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > _MAX_TAG:
            raise DatadogInputError(f"each tag must be 1-{_MAX_TAG} characters")
        result.append(item.strip())
    return result


def with_ownership(values: list[str]) -> list[str]:
    """Return ``values`` with the ownership tag appended once."""
    kept = [item for item in values if item != OWNERSHIP_TAG]
    if len(kept) >= _MAX_TAGS:
        raise DatadogInputError(f"tags must leave room for {OWNERSHIP_TAG}")
    return [*kept, OWNERSHIP_TAG]


def priority(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        raise DatadogInputError("priority must be an integer 1-5")
    return value


def monitor_options(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DatadogInputError("options must be a dict")
    unknown = sorted(str(key) for key in value if key not in MONITOR_OPTIONS)
    if unknown:
        raise DatadogInputError(
            f"options keys not allowed: {', '.join(unknown)}; "
            f"allowed: {', '.join(sorted(MONITOR_OPTIONS))}"
        )
    return dict(value)

"""Datadog read tool tests with a fake client (no network)."""

from datetime import UTC, datetime

import pytest
from archie_agent.exec.tools.datadog.client import (
    DatadogInputError,
    DatadogRequestRejectedError,
)
from archie_agent.exec.tools.datadog.metrics import query_metrics, search_metrics
from archie_agent.exec.tools.datadog.validation import time_window

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
QUERY = ("GET", "/api/v1/query")


def _series(points: int, **extra) -> dict:
    return {
        "metric": "system.cpu.user",
        "display_name": "system.cpu.user",
        "expression": "avg:system.cpu.user{*}",
        "scope": "*",
        "tag_set": [],
        "unit": [{"name": "percent"}, None],
        "interval": 20,
        "pointlist": [[1_700_000_000_000 + i * 20_000, float(i)] for i in range(points)],
        **extra,
    }


async def test_query_metrics_sends_epoch_window_and_normalises(fake):
    client = fake(
        {QUERY: {"query": "q", "from_date": 0, "to_date": 1000, "series": [_series(3)]}},
        "metrics",
    )
    result = await query_metrics("avg:system.cpu.user{*}", start="15m")
    method, path, params, body = client.calls[0]
    assert (method, path, body) == ("GET", "/api/v1/query", None)
    assert params["query"] == "avg:system.cpu.user{*}"
    assert params["to"] - params["from"] == 900
    series = result["series"][0]
    assert series["unit"] == "percent"
    assert series["point_count"] == 3
    assert series["points"][0] == ["2023-11-14T22:13:20Z", 0.0]
    assert result["truncated"] is False


async def test_query_metrics_keeps_last_points_and_caps_series(fake):
    fake({QUERY: {"series": [_series(450)] + [_series(1)] * 25}}, "metrics")
    result = await query_metrics("avg:x{*} by {host}")
    first = result["series"][0]
    assert len(first["points"]) == 300
    assert first["points"][-1][1] == 449.0
    assert first["truncated"] is True
    assert result["series_count"] == 26
    assert len(result["series"]) == 20
    assert result["truncated"] is True


async def test_query_metrics_error_body_is_rejected(fake):
    fake({QUERY: {"series": [], "error": "Rule 'scope_expr' didn't match"}}, "metrics")
    with pytest.raises(DatadogRequestRejectedError, match="scope_expr"):
        await query_metrics("avg:((")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": ""},
        {"query": "x" * 2001},
        {"query": "avg:x{*}", "start": "yesterday"},
        {"query": "avg:x{*}", "start": "1h", "end": "2h"},
        {"query": "avg:x{*}", "start": "32d"},
    ],
)
async def test_query_metrics_input_errors_make_no_request(fake, kwargs):
    client = fake({}, "metrics")
    with pytest.raises(DatadogInputError):
        await query_metrics(**kwargs)
    assert client.calls == []


def test_time_window_forms():
    begin, end = time_window("2h", None, now=NOW)
    assert (end - begin).total_seconds() == 7200
    begin, end = time_window("2026-05-01T10:00:00", "2026-05-01T11:00:00+00:00", now=NOW)
    assert begin.tzinfo is not None and (end - begin).total_seconds() == 3600


async def test_search_metrics_prefixes_and_limits(fake):
    client = fake(
        {("GET", "/api/v1/search"): {"results": {"metrics": ["a.b", "a.c", 3, "a.d"]}}},
        "metrics",
    )
    assert await search_metrics("a.", limit=2) == ["a.b", "a.c"]
    assert client.calls[0][2] == {"q": "metrics:a."}


@pytest.mark.parametrize("limit", [0, 101, True, "5"])
async def test_search_metrics_limit_validation(fake, limit):
    client = fake({}, "metrics")
    with pytest.raises(DatadogInputError):
        await search_metrics("a", limit=limit)
    assert client.calls == []

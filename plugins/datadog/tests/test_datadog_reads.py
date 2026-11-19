"""Datadog read tool tests with a fake client (no network)."""

from datetime import UTC, datetime

import pytest
from archie_agent.exec.plugin import plugin_tools
from archie_agent.exec.tools import get_all_tools
from archie_plugins.datadog.catalog import list_catalog_entities, list_teams
from archie_plugins.datadog.client import (
    DatadogInputError,
    DatadogNotFoundError,
    DatadogRateLimitError,
    DatadogRequestRejectedError,
    DatadogResponseError,
)
from archie_plugins.datadog.dashboards import get_dashboard, list_dashboards
from archie_plugins.datadog.events import list_events
from archie_plugins.datadog.logs import search_logs
from archie_plugins.datadog.metrics import describe_metric, query_metrics, search_metrics
from archie_plugins.datadog.monitors import search_monitors
from archie_plugins.datadog.traces import search_spans
from archie_plugins.datadog.validation import time_window
from datadog_fakes import Seq

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


async def test_query_metrics_sends_epoch_window_and_normalises(dd_fake):
    client = dd_fake(
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


async def test_query_metrics_keeps_last_points_and_caps_series(dd_fake):
    dd_fake({QUERY: {"series": [_series(450)] + [_series(1)] * 25}}, "metrics")
    result = await query_metrics("avg:x{*} by {host}")
    first = result["series"][0]
    assert len(first["points"]) == 300
    assert first["points"][-1][1] == 449.0
    assert first["truncated"] is True
    assert result["series_count"] == 26
    assert len(result["series"]) == 20
    assert result["truncated"] is True


async def test_query_metrics_error_body_is_rejected(dd_fake):
    dd_fake({QUERY: {"series": [], "error": "Rule 'scope_expr' didn't match"}}, "metrics")
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
async def test_query_metrics_input_errors_make_no_request(dd_fake, kwargs):
    client = dd_fake({}, "metrics")
    with pytest.raises(DatadogInputError):
        await query_metrics(**kwargs)
    assert client.calls == []


def test_time_window_forms():
    begin, end = time_window("2h", None, now=NOW)
    assert (end - begin).total_seconds() == 7200
    begin, end = time_window("2026-05-01T10:00:00", "2026-05-01T11:00:00+00:00", now=NOW)
    assert begin.tzinfo is not None and (end - begin).total_seconds() == 3600


async def test_search_metrics_prefixes_and_limits(dd_fake):
    client = dd_fake(
        {("GET", "/api/v1/search"): {"results": {"metrics": ["a.b", "a.c", 3, "a.d"]}}},
        "metrics",
    )
    assert await search_metrics("a.", limit=2) == ["a.b", "a.c"]
    assert client.calls[0][2] == {"q": "metrics:a."}


@pytest.mark.parametrize("results", [{"metrics": None}, {}])
async def test_search_metrics_no_matches_returns_empty(dd_fake, results):
    dd_fake({("GET", "/api/v1/search"): {"results": results}}, "metrics")
    assert await search_metrics("rabbitmq") == []


@pytest.mark.parametrize("data", [{}, {"results": None}, {"results": {"metrics": "x"}}, []])
async def test_search_metrics_malformed_response(dd_fake, data):
    dd_fake({("GET", "/api/v1/search"): data}, "metrics")
    with pytest.raises(DatadogResponseError):
        await search_metrics("a")


@pytest.mark.parametrize("limit", [0, 101, True, "5"])
async def test_search_metrics_limit_validation(dd_fake, limit):
    client = dd_fake({}, "metrics")
    with pytest.raises(DatadogInputError):
        await search_metrics("a", limit=limit)
    assert client.calls == []


LIST = ("GET", "/api/v1/metrics")


async def test_search_metrics_by_tag_lists_filters_and_sorts(dd_fake):
    client = dd_fake(
        {LIST: {"metrics": ["aws.mq.b", "aws.MQ.a", "system.cpu", 3, "aws.mq.c"]}}, "metrics"
    )
    assert await search_metrics("mq", tag=" queue:* ", limit=2) == ["aws.MQ.a", "aws.mq.b"]
    method, path, params, _ = client.calls[0]
    assert (method, path) == LIST
    assert params == {"from": 0, "tag_filter": "queue:*"}


@pytest.mark.parametrize("query", [None, "", "  "])
async def test_search_metrics_filtered_blank_query_means_no_name_filter(dd_fake, query):
    dd_fake({LIST: {"metrics": ["b", "a"]}}, "metrics")
    assert await search_metrics(query, tag="queue:*") == ["a", "b"]


async def test_search_metrics_active_within_sends_from_without_tag_filter(dd_fake):
    client = dd_fake({LIST: {"metrics": ["x"]}}, "metrics")
    before = datetime.now(UTC).timestamp()
    assert await search_metrics(active_within="2h") == ["x"]
    params = client.calls[0][2]
    assert "tag_filter" not in params
    assert before - 7200 - 5 <= params["from"] <= before - 7200 + 5


async def test_search_metrics_tag_and_active_within_together(dd_fake):
    client = dd_fake({LIST: {"metrics": []}}, "metrics")
    assert await search_metrics(tag="env:prod,service:api", active_within="30d") == []
    params = client.calls[0][2]
    assert params["tag_filter"] == "env:prod,service:api" and params["from"] > 0


@pytest.mark.parametrize("data", [{"metrics": None}, {}])
async def test_search_metrics_filtered_no_matches_returns_empty(dd_fake, data):
    dd_fake({LIST: data}, "metrics")
    assert await search_metrics(tag="bogus") == []


@pytest.mark.parametrize("data", [[], {"metrics": "x"}])
async def test_search_metrics_filtered_malformed_response(dd_fake, data):
    dd_fake({LIST: data}, "metrics")
    with pytest.raises(DatadogResponseError):
        await search_metrics(tag="queue:*")


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"query": "  "},
        {"tag": ""},
        {"tag": "   "},
        {"tag": "queue:* env:prod"},
        {"tag": "a" * 501},
        {"tag": 5},
        {"active_within": "2"},
        {"active_within": "1w"},
        {"active_within": "366d"},
        {"active_within": "2026-01-01T00:00:00"},
        {"active_within": 60},
        {"query": "x" * 2001, "tag": "a:b"},
    ],
)
async def test_search_metrics_rejects_bad_filters(dd_fake, kwargs):
    client = dd_fake({}, "metrics")
    with pytest.raises(DatadogInputError):
        await search_metrics(**kwargs)
    assert client.calls == []


METRIC = "aws.amazonmq.message_count"
META = ("GET", f"/api/v1/metrics/{METRIC}")
ALL_TAGS = ("GET", f"/api/v2/metrics/{METRIC}/all-tags")
ASSETS = ("GET", f"/api/v2/metrics/{METRIC}/assets")
META_BODY = {
    "description": "The total number of messages stored on the broker.",
    "short_name": "message_count",
    "type": "gauge",
    "unit": "message",
    "per_unit": None,
    "integration": None,
}
TAGS_BODY = {
    "data": {
        "id": METRIC,
        "type": "metrics",
        "attributes": {
            "tags": [
                "queue:b",
                "queue:a",
                "queue:a",
                "environment:production",
                "bare",
                7,
                "",
            ]
        },
    }
}
ASSETS_BODY = {
    "data": {"id": METRIC, "type": "metrics", "relationships": {}},
    "included": [
        {"id": "wy8-ypb-v2r", "type": "dashboards", "attributes": {"title": "RabbitMQ"}},
        {"id": "125239081", "type": "monitors", "attributes": {"title": "Queue high"}},
        {"id": "n1", "type": "notebooks", "attributes": {"title": "ignored"}},
        "junk",
    ],
}


async def test_describe_metric_combines_metadata_tags_and_assets(dd_fake):
    client = dd_fake({META: META_BODY, ALL_TAGS: TAGS_BODY, ASSETS: ASSETS_BODY}, "metrics")
    result = await describe_metric(f"  {METRIC} ")
    assert [call[1] for call in client.calls] == [META[1], ALL_TAGS[1], ASSETS[1]]
    assert result == {
        "name": METRIC,
        "type": "gauge",
        "unit": "message",
        "per_unit": None,
        "description": "The total number of messages stored on the broker.",
        "integration": None,
        "tags": {
            "bare": {"values": [], "value_count": 0},
            "environment": {"values": ["production"], "value_count": 1},
            "queue": {"values": ["a", "b"], "value_count": 2},
        },
        "tag_count": 4,
        "dashboards": [{"id": "wy8-ypb-v2r", "title": "RabbitMQ"}],
        "monitors": [{"id": "125239081", "title": "Queue high"}],
        "truncated": False,
    }


async def test_describe_metric_caps_values_keys_and_assets(dd_fake):
    tags = [f"queue:q{i:02d}" for i in range(30)] + [f"k{i:03d}:v" for i in range(120)]
    included = [{"id": f"d{i}", "type": "dashboards", "attributes": {}} for i in range(60)]
    dd_fake(
        {
            META: META_BODY,
            ALL_TAGS: {"data": {"attributes": {"tags": tags}}},
            ASSETS: {"included": included},
        },
        "metrics",
    )
    result = await describe_metric(METRIC, max_values=5)
    assert len(result["tags"]) == 100
    assert result["tag_count"] == 150
    assert len(result["dashboards"]) == 50
    assert result["truncated"] is True
    dd_fake(
        {META: META_BODY, ALL_TAGS: {"data": {"attributes": {"tags": tags[:30]}}}, ASSETS: {}},
        "metrics",
    )
    result = await describe_metric(METRIC, max_values=5)
    assert result["tags"]["queue"] == {
        "values": ["q00", "q01", "q02", "q03", "q04"],
        "value_count": 30,
    }
    assert result["truncated"] is True
    assert result["dashboards"] == [] and result["monitors"] == []


async def test_describe_metric_unknown_metric_raises_not_found(dd_fake):
    client = dd_fake({META: DatadogNotFoundError("Datadog resource was not found")}, "metrics")
    with pytest.raises(DatadogNotFoundError):
        await describe_metric(METRIC)
    assert len(client.calls) == 1


async def test_describe_metric_without_tag_data_returns_empty_tags(dd_fake):
    dd_fake(
        {
            META: META_BODY,
            ALL_TAGS: DatadogNotFoundError("Datadog resource was not found"),
            ASSETS: ASSETS_BODY,
        },
        "metrics",
    )
    result = await describe_metric(METRIC)
    assert result["tags"] == {} and result["tag_count"] == 0
    assert result["dashboards"] == [{"id": "wy8-ypb-v2r", "title": "RabbitMQ"}]


async def test_describe_metric_malformed_metadata(dd_fake):
    dd_fake({META: [], ALL_TAGS: TAGS_BODY, ASSETS: ASSETS_BODY}, "metrics")
    with pytest.raises(DatadogResponseError):
        await describe_metric(METRIC)


async def test_describe_metric_bad_max_values_names_the_argument(dd_fake):
    dd_fake({}, "metrics")
    with pytest.raises(DatadogInputError, match="max_values"):
        await describe_metric(METRIC, max_values=101)


@pytest.mark.parametrize(
    ("name", "max_values"),
    [("a/b", 20), ("1abc", 20), ("", 20), ("a..b/../x", 20), ("a" * 201, 20), (5, 20), ("a", 0)],
)
async def test_describe_metric_rejects_bad_input(dd_fake, name, max_values):
    client = dd_fake({}, "metrics")
    with pytest.raises(DatadogInputError):
        await describe_metric(name, max_values=max_values)
    assert client.calls == []


LOGS = ("POST", "/api/v2/logs/events/search")
SPANS = ("POST", "/api/v2/spans/events/search")
EVENTS = ("GET", "/api/v2/events")


def _log(i: int, **attrs) -> dict:
    return {
        "id": f"log-{i}",
        "type": "log",
        "attributes": {
            "timestamp": "2026-05-01T11:59:00Z",
            "service": "api",
            "host": "h1",
            "status": "error",
            "message": "boom",
            "tags": ["env:prod"],
            **attrs,
        },
    }


async def test_search_logs_pages_until_limit(dd_fake):
    client = dd_fake(
        {
            LOGS: Seq(
                [
                    {"data": [_log(1), _log(2)], "meta": {"page": {"after": "c1"}}},
                    {"data": [_log(3), _log(4)], "meta": {"page": {"after": "c2"}}},
                ]
            )
        },
        "logs",
    )
    result = await search_logs("service:api status:error", start="15m", limit=3, sort="oldest")
    assert [item["id"] for item in result] == ["log-1", "log-2", "log-3"]
    first, second = client.calls[0][3], client.calls[1][3]
    assert first["filter"]["query"] == "service:api status:error"
    assert first["sort"] == "timestamp"
    assert first["page"] == {"limit": 3}
    assert second["page"] == {"limit": 1, "cursor": "c1"}
    assert first["filter"]["from"].endswith("Z")


async def test_search_logs_stops_without_cursor(dd_fake):
    client = dd_fake({LOGS: {"data": [_log(1)], "meta": {}}}, "logs")
    assert len(await search_logs("*", limit=50)) == 1
    assert len(client.calls) == 1


async def test_log_record_is_bounded(dd_fake):
    item = _log(
        1,
        message="m" * 5000,
        tags=[f"t:{i}" for i in range(60)],
        attributes={**{f"k{i}": i for i in range(40)}, "nested": {"a": 1}, "long": "x" * 500},
    )
    dd_fake({LOGS: {"data": [item]}}, "logs")
    record = (await search_logs("*"))[0]
    assert len(record["message"]) == 2000
    assert len(record["tags"]) == 50
    assert len(record["attributes"]) == 30
    assert record["truncated"] is True
    assert set(record) == {
        "id",
        "timestamp",
        "service",
        "host",
        "status",
        "message",
        "tags",
        "attributes",
        "truncated",
    }


def test_log_attributes_replace_nested_values():
    from archie_plugins.datadog.formatting import log

    record = log(_log(1, attributes={"nested": {"a": 1}, "list": [1], "n": 3, "s": "y" * 300}))
    assert record["attributes"] == {"nested": "…", "list": "…", "n": 3, "s": "y" * 200}


async def test_search_logs_rejected_query_surfaces(dd_fake):
    dd_fake(
        {LOGS: DatadogRequestRejectedError("Datadog rejected the request: Cannot parse")}, "logs"
    )
    with pytest.raises(DatadogRequestRejectedError, match="Cannot parse"):
        await search_logs("status:((")


async def test_search_logs_rate_limit_surfaces(dd_fake):
    dd_fake({LOGS: DatadogRateLimitError(2)}, "logs")
    with pytest.raises(DatadogRateLimitError) as caught:
        await search_logs("*")
    assert caught.value.retry_after_seconds == 2


@pytest.mark.parametrize(
    "kwargs",
    [{"query": " "}, {"query": "*", "sort": "asc"}, {"query": "*", "limit": 0}],
)
async def test_search_logs_input_errors(dd_fake, kwargs):
    client = dd_fake({}, "logs")
    with pytest.raises(DatadogInputError):
        await search_logs(**kwargs)
    assert client.calls == []


AGGREGATE = ("POST", "/api/v2/logs/analytics/aggregate")


async def test_aggregate_logs_groups_by_several_facets(dd_fake):
    from archie_plugins.datadog.logs import aggregate_logs

    bucket = {
        "by": {"@network.client.ip": "1.2.3.4", "@http.useragent": "curl"},
        "computes": {"c0": 7},
    }
    client = dd_fake(
        {AGGREGATE: {"data": {"buckets": [bucket]}, "meta": {"status": "done"}}}, "logs"
    )
    result = await aggregate_logs(
        "service:api",
        ["@network.client.ip", {"facet": "@http.useragent", "limit": 5, "missing": "none"}],
        start="10m",
    )
    body = client.calls[0][3]
    assert body["compute"] == [{"aggregation": "count", "type": "total"}]
    sort = {"type": "measure", "aggregation": "count", "order": "desc"}
    assert body["group_by"] == [
        {"facet": "@network.client.ip", "limit": 100, "sort": sort},
        {"facet": "@http.useragent", "limit": 5, "missing": "none", "sort": sort},
    ]
    assert body["filter"]["query"] == "service:api"
    assert result["buckets"] == [{"by": bucket["by"], "value": 7}]
    assert result["bucket_count"] == 1
    assert result["group_by"] == ["@network.client.ip", "@http.useragent"]
    assert result["partial"] is False


async def test_aggregate_logs_timeseries_measure_and_paging(dd_fake):
    from archie_plugins.datadog.logs import aggregate_logs

    points = [{"time": "2026-05-01T11:00:00Z", "value": 3}]
    client = dd_fake(
        {
            AGGREGATE: Seq(
                [
                    {
                        "data": {"buckets": [{"by": {"service": "a"}, "computes": {"c0": points}}]},
                        "meta": {
                            "page": {"after": "p1"},
                            "status": "timeout",
                            "warnings": [{"title": "partial"}],
                        },
                    },
                    {"data": {"buckets": [{"by": {"service": "b"}, "computes": {"c0": points}}]}},
                ]
            )
        },
        "logs",
    )
    result = await aggregate_logs(
        "*", ["service"], aggregation="pc95", metric="@duration", interval="10m", order="asc"
    )
    first, second = client.calls[0][3], client.calls[1][3]
    assert first["compute"] == [
        {"aggregation": "pc95", "type": "timeseries", "metric": "@duration", "interval": "10m"}
    ]
    assert first["group_by"][0]["sort"] == {
        "type": "measure",
        "aggregation": "pc95",
        "order": "asc",
        "metric": "@duration",
    }
    assert "page" not in first and second["page"] == {"cursor": "p1"}
    assert [b["by"]["service"] for b in result["buckets"]] == ["a", "b"]
    assert result["buckets"][0]["points"] == [["2026-05-01T11:00:00Z", 3]]
    assert result["partial"] is True and result["warnings"] == ["partial"]


async def test_aggregate_logs_without_group_by_returns_total(dd_fake):
    from archie_plugins.datadog.logs import aggregate_logs

    client = dd_fake(
        {AGGREGATE: {"data": {"buckets": [{"by": {}, "computes": {"c0": 42}}]}}}, "logs"
    )
    result = await aggregate_logs("service:api")
    assert client.calls[0][3]["group_by"] == []
    assert result["buckets"] == [{"by": {}, "value": 42}]


async def test_aggregate_logs_malformed_response(dd_fake):
    from archie_plugins.datadog.logs import aggregate_logs

    dd_fake({AGGREGATE: {"data": []}}, "logs")
    with pytest.raises(DatadogResponseError):
        await aggregate_logs("*", ["service"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": ""},
        {"query": "*", "group_by": "service"},
        {"query": "*", "group_by": ["a", "b", "c", "d", "e"]},
        {"query": "*", "group_by": ["bad facet"]},
        {"query": "*", "group_by": [{"facet": "a", "size": 3}]},
        {"query": "*", "group_by": [{"limit": 3}]},
        {"query": "*", "group_by": [{"facet": "a", "limit": 1000}, {"facet": "b", "limit": 11}]},
        {"query": "*", "group_by": [{"facet": "a", "missing": 1}]},
        {"query": "*", "aggregation": "avg"},
        {"query": "*", "aggregation": "max"},
        {"query": "*", "interval": "10 minutes"},
        {"query": "*", "order": "newest"},
        {"query": "*", "metric": "x y", "aggregation": "sum"},
    ],
)
async def test_aggregate_logs_input_errors(dd_fake, kwargs):
    from archie_plugins.datadog.logs import aggregate_logs

    client = dd_fake({}, "logs")
    with pytest.raises(DatadogInputError):
        await aggregate_logs(**kwargs)
    assert client.calls == []


SPAN = {
    "id": "s",
    "type": "spans",
    "attributes": {
        "trace_id": "t1",
        "span_id": "s1",
        "parent_id": "0",
        "service": "api",
        "resource_name": "GET /orders",
        "operation_name": "http.request",
        "env": "prod",
        "start_timestamp": "2026-05-01T11:59:00.000Z",
        "end_timestamp": "2026-05-01T11:59:00.372Z",
        "status": "error",
        "custom": {
            "duration": 372_039_555,
            "error": {"type": "http_error", "message": "502 Bad Gateway"},
            "secret_header": "nope",
        },
    },
}


async def test_search_spans_one_request_and_normalises(dd_fake):
    client = dd_fake({SPANS: {"data": [SPAN, SPAN, SPAN]}}, "traces")
    result = await search_spans("service:api status:error", limit=2)
    assert len(client.calls) == 1
    attributes = client.calls[0][3]["data"]["attributes"]
    assert client.calls[0][3]["data"]["type"] == "search_request"
    assert attributes["page"] == {"limit": 2}
    assert attributes["sort"] == "-timestamp"
    assert len(result) == 2
    assert result[0]["duration_ms"] == 372.04
    assert result[0]["error"] == "http_error: 502 Bad Gateway"
    assert "custom" not in result[0]
    assert "secret_header" not in str(result[0])
    assert "fields" not in result[0]


NESTED = {
    "http": {"client_ip": "203.0.113.7", "url_details": {"path": "/gift/abc"}},
    "http.url_details": {"host": "flat.example"},
    "context": {"brand": "acme", "items": list(range(80)), "note": "n" * 3000},
    "network": {"client": {"ip": "10.0.0.1"}},
}


async def test_search_logs_fields_read_nested_attributes(dd_fake):
    dd_fake({LOGS: {"data": [_log(1, attributes=NESTED)]}}, "logs")
    [record] = await search_logs(
        "*",
        fields=[" @http.client_ip ", "@context", "http.url_details.host", "@missing.x", "network"],
    )
    fields = record["fields"]
    assert list(fields) == [
        "@http.client_ip",
        "@context",
        "http.url_details.host",
        "@missing.x",
        "network",
    ]
    assert fields["@http.client_ip"] == "203.0.113.7"
    assert fields["http.url_details.host"] == "flat.example"
    assert fields["@missing.x"] is None
    assert fields["network"] == {"client": {"ip": "10.0.0.1"}}
    assert fields["@context"]["brand"] == "acme"
    assert len(fields["@context"]["items"]) == 50
    assert len(fields["@context"]["note"]) == 2000
    assert record["truncated"] is True
    assert record["attributes"]["http"] == "…"


def test_fields_prefer_longest_dotted_key_then_fall_back_to_nesting():
    from archie_plugins.datadog.formatting import field_values

    source = {"a.b": {"x": 1}, "a": {"b": {"c": 2}}}
    values, truncated = field_values([source], ["a.b.x", "a.b.c"])
    assert values == {"a.b.x": 1, "a.b.c": 2}
    assert truncated is False


def test_fields_bound_depth_and_keys():
    from archie_plugins.datadog.formatting import field_values

    deep = {"l1": {"l2": {"l3": {"l4": {"l5": 1}}}}}
    wide = {f"k{i}": i for i in range(60)}
    values, truncated = field_values([{"deep": deep, "wide": wide}], ["deep", "wide"])
    assert values["deep"] == {"l1": {"l2": {"l3": {"l4": "…"}}}}
    assert len(values["wide"]) == 50
    assert truncated is True


@pytest.mark.parametrize("fields", [[], "@http", ["bad path"], [5], ["@a"] * 21, ["a/b"], [""]])
async def test_search_logs_and_spans_reject_bad_fields(dd_fake, fields):
    client = dd_fake({}, "logs", "traces")
    with pytest.raises(DatadogInputError):
        await search_logs("*", fields=fields)
    with pytest.raises(DatadogInputError):
        await search_spans("*", fields=fields)
    assert client.calls == []


async def test_search_spans_fields_read_custom_then_attributes(dd_fake):
    item = {
        **SPAN,
        "attributes": {
            **SPAN["attributes"],
            "custom": {**SPAN["attributes"]["custom"], "http": {"client_ip": "198.51.100.2"}},
            "attributes": {"http": {"client_ip": "shadowed"}, "peer": {"service": "db"}},
        },
    }
    dd_fake({SPANS: {"data": [item]}}, "traces")
    [record] = await search_spans("*", fields=["@http.client_ip", "@peer.service", "@duration"])
    assert record["fields"] == {
        "@http.client_ip": "198.51.100.2",
        "@peer.service": "db",
        "@duration": 372_039_555,
    }
    assert record["truncated"] is False


SPAN_AGGREGATE = ("POST", "/api/v2/spans/analytics/aggregate")


async def test_aggregate_spans_builds_the_shared_request_and_parses_compute(dd_fake):
    from archie_plugins.datadog.traces import aggregate_spans

    bucket = {
        "type": "bucket",
        "attributes": {"by": {"resource_name": "GET /"}, "compute": {"c0": 12.5}},
    }
    client = dd_fake(
        {SPAN_AGGREGATE: {"data": [bucket, "junk"], "meta": {"status": "done"}}}, "traces"
    )
    result = await aggregate_spans(
        "service:api", ["resource_name"], aggregation="avg", metric="@duration", start="30m"
    )
    assert len(client.calls) == 1
    body = client.calls[0][3]
    assert body["data"]["type"] == "aggregate_request"
    attributes = body["data"]["attributes"]
    assert attributes["compute"] == [{"aggregation": "avg", "type": "total", "metric": "@duration"}]
    assert attributes["group_by"] == [
        {
            "facet": "resource_name",
            "limit": 100,
            "sort": {
                "type": "measure",
                "aggregation": "avg",
                "order": "desc",
                "metric": "@duration",
            },
        }
    ]
    assert attributes["filter"]["query"] == "service:api"
    assert result["buckets"] == [{"by": {"resource_name": "GET /"}, "value": 12.5}]
    assert result["group_by"] == ["resource_name"]
    assert result["partial"] is False


async def test_aggregate_spans_timeseries_and_timeout(dd_fake):
    from archie_plugins.datadog.traces import aggregate_spans

    points = [{"time": "2026-05-01T11:00:00Z", "value": 4}]
    bucket = {"attributes": {"by": {"service": "api"}, "compute": {"c0": points}}}
    dd_fake(
        {
            SPAN_AGGREGATE: {
                "data": [bucket],
                "meta": {"status": "timeout", "warnings": [{"code": "slow"}]},
            }
        },
        "traces",
    )
    result = await aggregate_spans("*", ["service"], interval="5m")
    assert result["buckets"][0]["points"] == [["2026-05-01T11:00:00Z", 4]]
    assert result["partial"] is True and result["warnings"] == ["slow"]


async def test_aggregate_spans_malformed_and_input_errors(dd_fake):
    from archie_plugins.datadog.traces import aggregate_spans

    client = dd_fake({SPAN_AGGREGATE: {"data": {}}}, "traces")
    with pytest.raises(DatadogResponseError):
        await aggregate_spans("*")
    calls = len(client.calls)
    for kwargs in ({"aggregation": "mean"}, {"aggregation": "median"}, {"order": "up"}):
        with pytest.raises(DatadogInputError):
            await aggregate_spans("*", **kwargs)
    assert len(client.calls) == calls


async def test_aggregate_logs_accepts_avg_and_median(dd_fake):
    from archie_plugins.datadog.logs import aggregate_logs

    client = dd_fake({AGGREGATE: {"data": {"buckets": []}}}, "logs")
    await aggregate_logs("*", aggregation="median", metric="@duration")
    assert client.calls[0][3]["compute"][0]["aggregation"] == "median"


EVENT = {
    "id": "e1",
    "type": "event",
    "attributes": {
        "timestamp": "2026-05-01T11:00:00Z",
        "message": "Deployed v42",
        "tags": ["service:api"],
        "attributes": {"title": "Deploy", "evt": {"type": "kube_cronjob"}, "_dd": {"x": 1}},
    },
}


async def test_list_events_omits_query_when_none(dd_fake):
    client = dd_fake({EVENTS: {"data": [EVENT], "meta": {"page": {}}}}, "events")
    result = await list_events(limit=5)
    params = client.calls[0][2]
    assert "filter[query]" not in params
    assert params["page[limit]"] == 5
    assert params["sort"] == "-timestamp"
    assert result == [
        {
            "id": "e1",
            "timestamp": "2026-05-01T11:00:00Z",
            "title": "Deploy",
            "message": "Deployed v42",
            "source": "kube_cronjob",
            "tags": ["service:api"],
            "truncated": False,
        }
    ]


async def test_list_events_sends_query_and_follows_cursor(dd_fake):
    client = dd_fake(
        {
            EVENTS: Seq(
                [
                    {"data": [EVENT], "meta": {"page": {"after": "n"}}},
                    {"data": [EVENT], "meta": {"page": {}}},
                ]
            )
        },
        "events",
    )
    assert len(await list_events("source:deploy", start="1d", limit=10)) == 2
    assert client.calls[0][2]["filter[query]"] == "source:deploy"
    assert client.calls[1][2]["page[cursor]"] == "n"
    assert client.calls[1][2]["page[limit]"] == 9


@pytest.mark.parametrize(
    ("module", "call"),
    [
        ("events", lambda: list_events("x" * 2001)),
        ("monitors", lambda: search_monitors("x" * 2001)),
        ("dashboards", lambda: list_dashboards("x" * 2001)),
        ("catalog", lambda: list_catalog_entities(query="x" * 2001)),
        ("catalog", lambda: list_teams("x" * 2001)),
        ("events", lambda: list_events(3)),
    ],
)
async def test_optional_queries_reject_bad_values(dd_fake, module, call):
    client = dd_fake({}, module)
    with pytest.raises(DatadogInputError):
        await call()
    assert client.calls == []


def test_truncation_is_flagged_on_every_bounded_record():
    from archie_plugins.datadog.formatting import catalog_entity, dashboard_detail, span

    long_error = {"attributes": {"custom": {"error": {"type": "E", "message": "m" * 900}}}}
    assert span(long_error)["truncated"] is True
    assert len(span(long_error)["error"]) == 500
    assert span(SPAN)["truncated"] is False
    entity = {"id": "u", "attributes": {"tags": [f"t{i}" for i in range(60)]}}
    assert catalog_entity(entity)["truncated"] is True
    many = {"requests": [{"q": f"q{i}"} for i in range(12)]}
    detail = dashboard_detail(
        {
            "id": "abc-def-ghi",
            "widgets": [{"id": 1, "definition": {"type": "timeseries", **many}}],
            "template_variables": [{"name": f"v{i}"} for i in range(60)],
        }
    )
    assert detail["widgets"][0]["truncated"] is True
    assert len(detail["widgets"][0]["queries"]) == 10
    assert len(detail["template_variables"]) == 50
    assert detail["truncated"] is True


async def test_list_events_blank_query_is_no_filter(dd_fake):
    client = dd_fake({EVENTS: {"data": []}}, "events")
    await list_events("  ")
    assert "filter[query]" not in client.calls[0][2]


READS = {
    "aggregate_logs",
    "aggregate_spans",
    "describe_metric",
    "get_dashboard",
    "get_log_pipeline",
    "get_monitor",
    "list_catalog_entities",
    "list_dashboards",
    "list_events",
    "list_log_indexes",
    "list_log_metrics",
    "list_log_pipelines",
    "list_scanning_groups",
    "list_teams",
    "query_metrics",
    "search",
    "search_logs",
    "search_metrics",
    "search_monitors",
    "search_spans",
    "validate_monitor",
}


def test_registration_contains_read_functions():
    tools = plugin_tools()
    keys = {key.removeprefix("datadog.") for key in tools if key.startswith("datadog.")}
    assert READS <= keys
    core = get_all_tools()
    for name in READS:
        fn = tools[f"datadog.{name}"]
        assert fn._plugin == "datadog" and fn._namespace == "datadog"
        assert f"datadog.{name}" not in core


DASHBOARDS = ("GET", "/api/v1/dashboard")
DASHBOARD = {
    "id": "abc-def-ghi",
    "title": "Payments",
    "description": "d" * 900,
    "layout_type": "ordered",
    "author_handle": "a@example.com",
    "url": "/dashboard/abc-def-ghi/payments",
    "template_variables": [{"name": "env"}, {"prefix": "x"}],
    "widgets": [
        {"id": 1, "definition": {"type": "note", "content": "hi"}},
        {
            "id": 2,
            "definition": {
                "type": "timeseries",
                "title": "Errors",
                "requests": [
                    {"q": "sum:errors{*}"},
                    {
                        "queries": [
                            {
                                "name": "q1",
                                "data_source": "logs",
                                "search": {"query": "status:error"},
                            }
                        ]
                    },
                    {"queries": [{"name": "q2", "data_source": "metrics", "query": "avg:cpu{*}"}]},
                ],
            },
        },
        {"id": 3, "definition": {"type": "group", "title": "G", "widgets": [{}, {}]}},
    ]
    + [{"id": 100 + i, "definition": {"type": "note"}} for i in range(100)],
}


async def test_list_dashboards_filters_title_locally(dd_fake):
    items = [
        {"id": "aaa-bbb-ccc", "title": "Payments overview"},
        {"id": "ddd-eee-fff", "title": "Infra"},
        {"id": "ggg-hhh-iii", "title": "payments errors"},
    ]
    client = dd_fake({DASHBOARDS: {"dashboards": items}}, "dashboards")
    result = await list_dashboards("PAYMENTS", limit=1)
    assert [item["id"] for item in result] == ["aaa-bbb-ccc"]
    assert client.calls[0][2] == {"count": 100, "start": 0}
    assert len(await list_dashboards()) == 3


def _dashboards(start: int, count: int, title: str = "Board") -> dict:
    return {"dashboards": [{"id": f"d{start + i}", "title": title} for i in range(count)]}


async def test_list_dashboards_pages_past_the_first_hundred(dd_fake):
    second = _dashboards(100, 30)
    second["dashboards"][5]["title"] = "Payments"
    client = dd_fake({DASHBOARDS: Seq([_dashboards(0, 100), second])}, "dashboards")
    result = await list_dashboards("payments")
    assert [item["id"] for item in result] == ["d105"]
    assert [call[2] for call in client.calls] == [
        {"count": 100, "start": 0},
        {"count": 100, "start": 100},
    ]


async def test_list_dashboards_stops_once_limit_matches(dd_fake):
    client = dd_fake({DASHBOARDS: _dashboards(0, 100)}, "dashboards")
    assert len(await list_dashboards(limit=10)) == 10
    assert len(client.calls) == 1


async def test_list_dashboards_full_page_then_empty_page(dd_fake):
    client = dd_fake({DASHBOARDS: Seq([_dashboards(0, 100), {"dashboards": []}])}, "dashboards")
    assert len(await list_dashboards(limit=1000)) == 100
    assert len(client.calls) == 2


async def test_list_dashboards_stops_after_twenty_pages(dd_fake):
    client = dd_fake({DASHBOARDS: _dashboards(0, 100)}, "dashboards")
    assert len(await list_dashboards("missing")) == 0
    assert len(client.calls) == 20
    assert client.calls[-1][2] == {"count": 100, "start": 1900}


@pytest.mark.parametrize("limit", [0, 1001])
async def test_list_dashboards_limit_bounds(dd_fake, limit):
    client = dd_fake({}, "dashboards")
    with pytest.raises(DatadogInputError):
        await list_dashboards(limit=limit)
    assert client.calls == []


async def test_list_dashboards_malformed(dd_fake):
    dd_fake({DASHBOARDS: {"dashboards": None}}, "dashboards")
    with pytest.raises(DatadogResponseError):
        await list_dashboards()


async def test_get_dashboard_widgets_and_bounds(dd_fake):
    dd_fake({("GET", "/api/v1/dashboard/abc-def-ghi"): DASHBOARD}, "dashboards")
    result = await get_dashboard("abc-def-ghi")
    assert result["widget_count"] == 103
    assert len(result["widgets"]) == 100
    assert result["truncated"] is True
    assert result["template_variables"] == ["env"]
    assert len(result["description"]) == 500
    assert result["widgets"][1]["queries"] == ["sum:errors{*}", "status:error", "avg:cpu{*}"]
    empty = {"id": None, "type": None, "title": None, "queries": [], "truncated": False}
    assert result["widgets"][2] == {
        "id": 3,
        "type": "group",
        "title": "G",
        "queries": [],
        "truncated": False,
        "child_count": 2,
        "widgets": [empty, empty],
    }


async def test_get_dashboard_lists_widgets_inside_groups(dd_fake):
    child = {
        "id": 11,
        "definition": {"type": "query_value", "title": "5xx", "requests": [{"q": "sum:5xx{*}"}]},
    }
    group = {"id": 10, "definition": {"type": "group", "title": "HTTP", "widgets": [child, child]}}
    dd_fake(
        {("GET", "/api/v1/dashboard/abc-def-ghi"): {"id": "abc-def-ghi", "widgets": [group]}},
        "dashboards",
    )
    result = await get_dashboard("abc-def-ghi")
    [record] = result["widgets"]
    assert record["child_count"] == 2
    assert [w["queries"] for w in record["widgets"]] == [["sum:5xx{*}"], ["sum:5xx{*}"]]
    assert record["widgets"][0]["title"] == "5xx"
    assert result["widget_count"] == 1 and result["truncated"] is False


async def test_get_dashboard_caps_group_children_in_order(dd_fake):
    def group(gid, count):
        children = [{"id": gid * 1000 + i, "definition": {"type": "note"}} for i in range(count)]
        return {"id": gid, "definition": {"type": "group", "widgets": children}}

    body = {"id": "abc-def-ghi", "widgets": [group(1, 70), group(2, 50), group(3, 5)]}
    dd_fake({("GET", "/api/v1/dashboard/abc-def-ghi"): body}, "dashboards")
    result = await get_dashboard("abc-def-ghi")
    counts = [len(w["widgets"]) for w in result["widgets"]]
    assert counts == [70, 30, 0]
    assert [w["truncated"] for w in result["widgets"]] == [False, True, True]
    assert [w["child_count"] for w in result["widgets"]] == [70, 50, 5]
    assert result["truncated"] is True


@pytest.mark.parametrize("value", ["abc", "ABC-DEF-GHI", "abc-def-ghi/../x", 5])
async def test_get_dashboard_rejects_bad_ids(dd_fake, value):
    client = dd_fake({}, "dashboards")
    with pytest.raises(DatadogInputError):
        await get_dashboard(value)
    assert client.calls == []


async def test_list_catalog_entities_filters(dd_fake):
    entity = {
        "id": "u1",
        "attributes": {
            "kind": "service",
            "name": "api",
            "displayName": "API",
            "owner": "team:pay",
            "tags": ["t"],
        },
    }
    client = dd_fake({("GET", "/api/v2/catalog/entity"): {"data": [entity]}}, "catalog")
    result = await list_catalog_entities(kind="service", query="api", limit=5)
    assert client.calls[0][2] == {
        "page[limit]": 5,
        "page[offset]": 0,
        "filter[kind]": "service",
        "filter[name]": "api",
    }
    assert result == [
        {
            "id": "u1",
            "kind": "service",
            "name": "api",
            "display_name": "API",
            "owner": "team:pay",
            "tags": ["t"],
            "truncated": False,
        }
    ]


async def test_list_catalog_entities_rejects_bad_kind(dd_fake):
    client = dd_fake({}, "catalog")
    with pytest.raises(DatadogInputError):
        await list_catalog_entities(kind="Service Name")
    assert client.calls == []


async def test_list_teams_keyword(dd_fake):
    item = {"id": "t1", "attributes": {"handle": "pay", "name": "Payments", "user_count": 4}}
    client = dd_fake({("GET", "/api/v2/team"): {"data": [item]}}, "catalog")
    assert await list_teams("pay") == [
        {"id": "t1", "handle": "pay", "name": "Payments", "member_count": 4}
    ]
    assert client.calls[0][2]["filter[keyword]"] == "pay"

"""Datadog read tool tests with a fake client (no network)."""

from datetime import UTC, datetime

import pytest
from archie_agent.exec.tools import get_all_tools
from archie_agent.exec.tools.datadog.catalog import list_catalog_entities, list_teams
from archie_agent.exec.tools.datadog.client import (
    DatadogInputError,
    DatadogNotFoundError,
    DatadogRateLimitError,
    DatadogRequestRejectedError,
    DatadogResponseError,
)
from archie_agent.exec.tools.datadog.dashboards import get_dashboard, list_dashboards
from archie_agent.exec.tools.datadog.events import list_events
from archie_agent.exec.tools.datadog.logs import search_logs
from archie_agent.exec.tools.datadog.metrics import describe_metric, query_metrics, search_metrics
from archie_agent.exec.tools.datadog.monitors import search_monitors
from archie_agent.exec.tools.datadog.traces import search_spans
from archie_agent.exec.tools.datadog.validation import time_window

from tests.datadog_fakes import Seq

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
    from archie_agent.exec.tools.datadog.formatting import log

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
    from archie_agent.exec.tools.datadog.formatting import catalog_entity, dashboard_detail, span

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
    "describe_metric",
    "get_dashboard",
    "get_monitor",
    "list_catalog_entities",
    "list_dashboards",
    "list_events",
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
    tools = get_all_tools()
    keys = {key.removeprefix("datadog.") for key in tools if key.startswith("datadog.")}
    assert READS <= keys
    for name in READS:
        fn = tools[f"datadog.{name}"]
        assert fn._native is False and fn._exec_docs is False


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
    assert client.calls[0][2] is None
    assert len(await list_dashboards()) == 3


async def test_get_dashboard_widgets_and_bounds(dd_fake):
    dd_fake({("GET", "/api/v1/dashboard/abc-def-ghi"): DASHBOARD}, "dashboards")
    result = await get_dashboard("abc-def-ghi")
    assert result["widget_count"] == 103
    assert len(result["widgets"]) == 100
    assert result["truncated"] is True
    assert result["template_variables"] == ["env"]
    assert len(result["description"]) == 500
    assert result["widgets"][1]["queries"] == ["sum:errors{*}", "status:error", "avg:cpu{*}"]
    assert result["widgets"][2] == {
        "id": 3,
        "type": "group",
        "title": "G",
        "queries": [],
        "truncated": False,
        "child_count": 2,
    }


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

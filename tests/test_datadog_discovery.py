"""Datadog cross-source discovery tests with a fake client (no network)."""

import asyncio

import pytest
from archie_agent.exec.tools.datadog import discovery
from archie_agent.exec.tools.datadog.client import (
    DatadogAuthenticationError,
    DatadogAuthorizationError,
    DatadogConfigurationError,
    DatadogInputError,
    DatadogPolicyError,
    DatadogRateLimitError,
    DatadogResponseError,
    DatadogTransportError,
)
from archie_agent.exec.tools.datadog.discovery import query_metrics_in, search, span_query

MONITORS = ("GET", "/api/v1/monitor/search")
DASHBOARDS = ("GET", "/api/v1/dashboard")
NAMES = ("GET", "/api/v1/search")
LIST = ("GET", "/api/v1/metrics")
LOGS = ("POST", "/api/v2/logs/analytics/aggregate")
SPANS = ("POST", "/api/v2/spans/analytics/aggregate")

MONITOR_BODY = {
    "monitors": [
        {
            "id": 11,
            "name": "RabbitMQ high CPU",
            "type": "query alert",
            "query": "max(last_5m):max:aws.amazonmq.system_cpu{broker:dev}.rollup(max, 60) >= 70",
        },
        {
            "id": 12,
            "name": "RabbitMQ memory",
            "type": "query alert",
            "query": (
                "max(last_5m):(max:aws.amazonmq.mem_used{broker:staging} / "
                "max:aws.amazonmq.mem_limit{broker:staging}) * 100 >= 70"
            ),
        },
        {
            "id": 13,
            "name": "RabbitMQ log errors",
            "type": "log alert",
            "query": 'logs("service:rabbitmq status:error").index("*").rollup("count") > 5',
        },
        "junk",
    ]
}
DASHBOARD_BODY = {
    "dashboards": [
        {"id": "aaa-bbb-ccc", "title": "RabbitMQ Queue Worker Dashboard", "description": None},
        {"id": "ddd-eee-fff", "title": "Brokers", "description": "rabbitmq overview"},
        {"id": "ggg-hhh-iii", "title": "Payments", "description": "unrelated"},
        {"id": "jjj-kkk-lll", "title": "RabbitMQ both", "description": "rabbitmq too"},
    ]
}
LOG_BODY = {
    "data": {
        "buckets": [
            {"by": {"service": "core-worker"}, "computes": {"c0": 3}},
            {"by": {"service": "agent"}, "computes": {"c0": 13}},
            {"by": {"service": ""}, "computes": {"c0": 1}},
            {"by": "bad", "computes": {}},
        ]
    }
}
SPAN_BODY = {
    "data": [
        {"type": "bucket", "attributes": {"by": {"service": "amqp"}, "compute": {"c0": 7487}}},
        {"type": "bucket", "attributes": "bad"},
    ]
}


def _default_fake(dd_fake):
    responses = {
        MONITORS: MONITOR_BODY,
        DASHBOARDS: DASHBOARD_BODY,
        NAMES: {"results": {"metrics": ["aws.amazonmq.mem_used", "rabbitmq.queue.depth"]}},
        LIST: {"metrics": []},
        LOGS: LOG_BODY,
        SPANS: SPAN_BODY,
    }
    return dd_fake(responses, "discovery", "metrics")


def _paths(client):
    return [(method, path) for method, path, _, _ in client.calls]


async def test_search_returns_every_type_with_its_handoff(dd_fake):
    client = _default_fake(dd_fake)
    result = await search("rabbitmq")
    assert result["skipped"] == {}
    assert SPANS not in _paths(client)
    by_type = {}
    for item in result["results"]:
        assert set(item) == {"type", "id", "title", "source", "matched_on", "count", "handoff"}
        by_type.setdefault(item["type"], []).append(item)

    assert by_type["monitor"][0] == {
        "type": "monitor",
        "id": 11,
        "title": "RabbitMQ high CPU",
        "source": "monitors",
        "matched_on": "monitor search",
        "count": None,
        "handoff": "datadog.get_monitor",
    }
    assert [d["id"] for d in by_type["dashboard"]] == ["aaa-bbb-ccc", "ddd-eee-fff", "jjj-kkk-lll"]
    assert [d["matched_on"] for d in by_type["dashboard"]] == ["title", "description", "title"]
    assert {d["handoff"] for d in by_type["dashboard"]} == {"datadog.get_dashboard"}

    metrics = [(m["id"], m["source"], m["matched_on"]) for m in by_type["metric"]]
    assert metrics == [
        ("aws.amazonmq.mem_used", "metrics", "name"),
        ("rabbitmq.queue.depth", "metrics", "name"),
        ("aws.amazonmq.system_cpu", "monitors", "monitor 11"),
        ("aws.amazonmq.mem_limit", "monitors", "monitor 12"),
    ]
    assert {m["handoff"] for m in by_type["metric"]} == {"datadog.describe_metric"}
    assert all(m["count"] is None and m["title"] == m["id"] for m in by_type["metric"])

    assert by_type["service"] == [
        {
            "type": "service",
            "id": "core-worker",
            "title": "core-worker",
            "source": "logs",
            "matched_on": "log content",
            "count": 3,
            "handoff": "datadog.search_logs",
        },
        {
            "type": "service",
            "id": "agent",
            "title": "agent",
            "source": "logs",
            "matched_on": "log content",
            "count": 13,
            "handoff": "datadog.search_logs",
        },
    ]


async def test_search_sends_bounded_requests(dd_fake):
    client = _default_fake(dd_fake)
    await search("rabbitmq", start="2h", limit=7)
    calls = {(method, path): (params, body) for method, path, params, body in client.calls}
    assert calls[MONITORS][0] == {"query": "rabbitmq", "per_page": 7, "page": 0}
    assert calls[NAMES][0] == {"q": "metrics:rabbitmq"}
    logs = calls[LOGS][1]
    assert logs["filter"]["query"] == "rabbitmq"
    assert logs["group_by"] == [{"facet": "service", "limit": 7}]
    assert logs["filter"]["from"].endswith("Z") and logs["filter"]["to"].endswith("Z")
    assert LIST not in calls


async def test_search_spans_only_when_requested_with_widened_query(dd_fake):
    client = _default_fake(dd_fake)
    result = await search("rabbitmq", sources=["spans"])
    assert _paths(client) == [SPANS]
    body = client.calls[0][3]["data"]["attributes"]
    assert body["filter"]["query"] == "service:*rabbitmq* OR resource_name:*rabbitmq*"
    assert result["results"] == [
        {
            "type": "service",
            "id": "amqp",
            "title": "amqp",
            "source": "spans",
            "matched_on": "span attributes",
            "count": 7487,
            "handoff": "datadog.search_spans",
        }
    ]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("rabbitmq", "service:*rabbitmq* OR resource_name:*rabbitmq*"),
        ("core-worker.v2", "service:*core-worker.v2* OR resource_name:*core-worker.v2*"),
        ("service:api", "service:api"),
        ("two words", "two words"),
    ],
)
def test_span_query(query, expected):
    assert span_query(query) == expected


async def test_tag_shaped_query_lists_metrics_by_tag_instead_of_name_search(dd_fake):
    client = _default_fake(dd_fake)
    client.responses[LIST] = {"metrics": ["b.metric", "a.metric", "b.metric"]}
    result = await search("queue:audit-log", sources=["metrics"])
    assert _paths(client) == [LIST]
    assert client.calls[0][2] == {"from": 0, "tag_filter": "queue:audit-log"}
    assert [(m["id"], m["matched_on"]) for m in result["results"]] == [
        ("a.metric", "tag"),
        ("b.metric", "tag"),
    ]


async def test_bare_word_query_does_not_list_metrics_by_tag(dd_fake):
    client = _default_fake(dd_fake)
    await search("rabbitmq", sources=["metrics"])
    assert _paths(client) == [NAMES]


async def test_metric_results_are_limited_per_part(dd_fake):
    client = _default_fake(dd_fake)
    client.responses[NAMES] = {"results": {"metrics": [f"m{i}" for i in range(5)]}}
    client.responses[MONITORS] = {
        "monitors": [
            {"id": i, "name": f"n{i}", "type": "metric alert", "query": f"avg:x{i}{{*}} > 1"}
            for i in range(5)
        ]
    }
    result = await search("x", sources=["monitors", "metrics"], limit=2)
    kinds = [(r["type"], r["source"]) for r in result["results"]]
    assert kinds.count(("monitor", "monitors")) == 2
    assert kinds.count(("metric", "metrics")) == 2
    assert kinds.count(("metric", "monitors")) == 2


async def test_failed_source_is_skipped_and_others_return(dd_fake):
    client = _default_fake(dd_fake)
    client.responses[SPANS] = DatadogRateLimitError(30)
    client.responses[DASHBOARDS] = DatadogAuthorizationError("Datadog authorization denied")
    result = await search("rabbitmq", sources=["monitors", "dashboards", "spans"])
    assert result["skipped"] == {
        "dashboards": "DatadogAuthorizationError: Datadog authorization denied",
        "spans": "DatadogRateLimitError: Datadog rate limit reached; retry after 30 seconds",
    }
    assert {r["type"] for r in result["results"]} == {"monitor", "metric"}


async def test_monitor_failure_drops_extracted_metrics_only(dd_fake):
    client = _default_fake(dd_fake)
    client.responses[MONITORS] = DatadogResponseError("bad")
    result = await search("rabbitmq", sources=["monitors", "metrics"])
    assert set(result["skipped"]) == {"monitors"}
    assert {r["source"] for r in result["results"]} == {"metrics"}


async def test_unexpected_source_exception_is_skipped_as_response_error(dd_fake):
    client = _default_fake(dd_fake)
    client.responses[LOGS] = KeyError("boom")
    result = await search("rabbitmq", sources=["logs", "dashboards"])
    assert result["skipped"] == {
        "logs": "DatadogResponseError: Datadog returned an unexpected response"
    }


async def test_source_timeout_is_skipped(dd_fake, monkeypatch):
    client = _default_fake(dd_fake)
    monkeypatch.setattr(discovery, "SOURCE_TIMEOUT", 0.01)

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(discovery, "_dashboards", slow)
    result = await search("rabbitmq", sources=["dashboards", "monitors"])
    assert result["skipped"] == {
        "dashboards": "DatadogTransportError: Datadog source exceeded 0.01s"
    }
    assert client.calls


async def test_all_sources_failing_raises_first_in_selection_order(dd_fake):
    client = _default_fake(dd_fake)
    client.responses[DASHBOARDS] = DatadogRateLimitError(5)
    client.responses[MONITORS] = DatadogResponseError("monitors broke")
    with pytest.raises(DatadogRateLimitError):
        await search("rabbitmq", sources=["dashboards", "monitors"])
    with pytest.raises(DatadogResponseError):
        await search("rabbitmq", sources=["monitors", "dashboards"])


async def test_all_sources_timing_out_raises_transport_error(dd_fake, monkeypatch):
    _default_fake(dd_fake)
    monkeypatch.setattr(discovery, "SOURCE_TIMEOUT", 0.01)

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(discovery, "_dashboards", slow)
    with pytest.raises(DatadogTransportError):
        await search("rabbitmq", sources=["dashboards"])


@pytest.mark.parametrize(
    "error",
    [
        DatadogAuthenticationError("Datadog authentication failed"),
        DatadogConfigurationError("Datadog credentials are unavailable or invalid"),
        DatadogPolicyError("Datadog request is not permitted"),
    ],
)
async def test_fatal_errors_are_raised_not_skipped(dd_fake, error):
    client = _default_fake(dd_fake)
    client.responses[LOGS] = error
    with pytest.raises(type(error)):
        await search("rabbitmq")


async def test_reads_disabled_by_policy_makes_no_calls(dd_fake, policy):
    client = _default_fake(dd_fake)
    policy({"datadog": {"read": {"enabled": False}}})
    with pytest.raises(DatadogPolicyError):
        await search("rabbitmq")
    assert client.calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": ""},
        {"query": "  "},
        {"query": None},
        {"query": "x" * 2001},
        {"query": "x", "sources": []},
        {"query": "x", "sources": ["logs", "logs"]},
        {"query": "x", "sources": ["hosts"]},
        {"query": "x", "sources": "logs"},
        {"query": "x", "start": "yesterday"},
        {"query": "x", "start": "40d"},
        {"query": "x", "limit": 0},
        {"query": "x", "limit": 51},
        {"query": "x", "limit": True},
    ],
)
async def test_search_rejects_bad_input_before_any_call(dd_fake, kwargs):
    client = _default_fake(dd_fake)
    with pytest.raises(DatadogInputError):
        await search(**kwargs)
    assert client.calls == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("max(last_5m):max:aws.amazonmq.x{broker:dev} > 1", ["aws.amazonmq.x"]),
        ("avg(last_1h):avg:a.b{*} by {host} - avg:c.d {*} > 0", ["a.b", "c.d"]),
        ("avg(last_5m):avg:a.b{env:prod,queue:x.y} by {queue} > 0", ["a.b"]),
        ("sum(last_5m):sum:a.b{x}.as_count() / sum:a.b{y}.as_count() > 1", ["a.b"]),
        ("avg(last_5m):anomalies(avg:system.load.1{*}, 'basic', 2) >= 1", ["system.load.1"]),
        ("no scope here > 1", []),
        (None, []),
    ],
)
def test_query_metrics_in(query, expected):
    assert query_metrics_in(query) == expected

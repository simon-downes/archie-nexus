"""Log configuration read tests with a fake client (no network)."""

import pytest
from archie_plugins.datadog.client import (
    DatadogAuthorizationError,
    DatadogInputError,
    DatadogNotFoundError,
    DatadogResponseError,
)
from archie_plugins.datadog.log_config import (
    get_log_pipeline,
    list_log_indexes,
    list_log_metrics,
    list_log_pipelines,
    list_scanning_groups,
)

PIPELINES = ("GET", "/api/v1/logs/config/pipelines")
PIPELINE_ORDER = ("GET", "/api/v1/logs/config/pipeline-order")

GROK = {
    "type": "grok-parser",
    "name": "Parse access line",
    "is_enabled": True,
    "source": "message",
    "samples": ['1.2.3.4 - - [01/Oct/2026] "GET / HTTP/1.1" 200'],
    "grok": {
        "match_rules": "access %{ip:network.client.ip} %{notSpace} %{data:http.url}",
        "support_rules": "",
    },
}
APACHE = {
    "id": "aaa",
    "type": "pipeline",
    "name": "Apache",
    "is_enabled": True,
    "is_read_only": True,
    "filter": {"query": "source:apache"},
    "processors": [
        GROK,
        {
            "type": "pipeline",
            "name": "Nested",
            "is_enabled": False,
            "filter": {"query": "service:web"},
            "processors": [
                {
                    "type": "attribute-remapper",
                    "name": "XFF",
                    "is_enabled": True,
                    "sources": ["http.x_forwarded_for"],
                    "target": "network.client.ip",
                }
            ],
        },
    ],
}
JSON_APP = {
    "id": "bbb",
    "type": "pipeline",
    "name": "Wrappr app",
    "is_enabled": True,
    "is_read_only": False,
    "filter": {"query": "service:wrappr"},
    "description": "Custom",
    "tags": ["team:web"],
    "processors": [],
}
ORPHAN = {"id": "ccc", "name": "Not in order", "filter": {}, "processors": []}


async def test_list_log_pipelines_returns_processing_order_with_summaries(dd_fake):
    client = dd_fake(
        {
            PIPELINES: [APACHE, ORPHAN, JSON_APP, "junk"],
            PIPELINE_ORDER: {"pipeline_ids": ["bbb", "aaa"]},
        },
        "log_config",
    )
    result = await list_log_pipelines()
    assert [call[:2] for call in client.calls] == [PIPELINES, PIPELINE_ORDER]
    assert [(item["position"], item["id"]) for item in result] == [
        (1, "bbb"),
        (2, "aaa"),
        (None, "ccc"),
    ]
    assert result[0] == {
        "position": 1,
        "id": "bbb",
        "name": "Wrappr app",
        "type": "pipeline",
        "enabled": True,
        "read_only": False,
        "filter": "service:wrappr",
        "description": "Custom",
        "tags": ["team:web"],
        "processor_count": 0,
        "processors": [],
        "truncated": False,
    }
    assert result[1]["processors"] == [
        {"type": "grok-parser", "name": "Parse access line", "enabled": True},
        {
            "type": "pipeline",
            "name": "Nested",
            "enabled": False,
            "filter": "service:web",
            "processors": [{"type": "attribute-remapper", "name": "XFF", "enabled": True}],
        },
    ]
    assert result[1]["processor_count"] == 2
    assert result[2]["filter"] is None


@pytest.mark.parametrize(
    ("query", "expected"),
    [("APACHE", ["aaa"]), ("service:wrappr", ["bbb"]), ("  ", ["bbb", "aaa"]), ("x", [])],
)
async def test_list_log_pipelines_filters_on_name_or_filter_query(dd_fake, query, expected):
    dd_fake(
        {PIPELINES: [APACHE, JSON_APP], PIPELINE_ORDER: {"pipeline_ids": ["bbb", "aaa"]}},
        "log_config",
    )
    result = await list_log_pipelines(query)
    assert [item["id"] for item in result] == expected


async def test_list_log_pipelines_filter_keeps_global_positions(dd_fake):
    dd_fake(
        {PIPELINES: [APACHE, JSON_APP], PIPELINE_ORDER: {"pipeline_ids": ["bbb", "aaa"]}},
        "log_config",
    )
    assert [item["position"] for item in await list_log_pipelines("apache")] == [2]


async def test_list_log_pipelines_caps_processor_summary(dd_fake):
    many = {**JSON_APP, "processors": [GROK] * 120}
    dd_fake({PIPELINES: [many], PIPELINE_ORDER: {"pipeline_ids": []}}, "log_config")
    [item] = await list_log_pipelines()
    assert item["processor_count"] == 120
    assert len(item["processors"]) == 100
    assert item["truncated"] is True


@pytest.mark.parametrize(
    "responses",
    [
        {PIPELINES: {"pipelines": []}, PIPELINE_ORDER: {"pipeline_ids": []}},
        {PIPELINES: [], PIPELINE_ORDER: {}},
        {PIPELINES: [], PIPELINE_ORDER: []},
    ],
)
async def test_list_log_pipelines_malformed_response(dd_fake, responses):
    dd_fake(responses, "log_config")
    with pytest.raises(DatadogResponseError):
        await list_log_pipelines()


async def test_list_log_pipelines_missing_permission_is_authorization_error(dd_fake):
    dd_fake(
        {PIPELINES: DatadogAuthorizationError("Datadog authorization denied: logs_read_config")},
        "log_config",
    )
    with pytest.raises(DatadogAuthorizationError, match="logs_read_config"):
        await list_log_pipelines()


async def test_get_log_pipeline_returns_full_processor_configuration(dd_fake):
    client = dd_fake({("GET", "/api/v1/logs/config/pipelines/aaa"): APACHE}, "log_config")
    result = await get_log_pipeline(" aaa ")
    assert client.calls[0][1] == "/api/v1/logs/config/pipelines/aaa"
    assert result["processors"][0] == GROK
    assert result["processors"][1]["processors"][0]["target"] == "network.client.ip"
    assert result["read_only"] is True and result["filter"] == "source:apache"
    assert "position" not in result
    assert result["truncated"] is False


async def test_get_log_pipeline_bounds_long_rules_and_lookup_tables(dd_fake):
    rules = "r " * 6_000
    lookup = {
        "type": "lookup-processor",
        "source": "code",
        "target": "name",
        "lookup_table": [f"{i},v{i}" for i in range(150)],
    }
    pipeline = {
        **JSON_APP,
        "processors": [{**GROK, "grok": {"match_rules": rules}}, lookup],
    }
    dd_fake({("GET", "/api/v1/logs/config/pipelines/bbb"): pipeline}, "log_config")
    result = await get_log_pipeline("bbb")
    assert len(result["processors"][0]["grok"]["match_rules"]) == 10_000
    assert len(result["processors"][1]["lookup_table"]) == 100
    assert result["truncated"] is True


async def test_get_log_pipeline_bounds_depth_and_keys(dd_fake):
    deep: dict = {"leaf": 1}
    for _ in range(10):
        deep = {"next": deep}
    wide = {f"k{i}": i for i in range(250)}
    pipeline = {**JSON_APP, "processors": [deep, wide]}
    dd_fake({("GET", "/api/v1/logs/config/pipelines/bbb"): pipeline}, "log_config")
    result = await get_log_pipeline("bbb")
    node = result["processors"][0]
    for _ in range(6):
        node = node["next"]
    assert node == {"next": "…"}
    assert len(result["processors"][1]) == 200
    assert result["truncated"] is True


@pytest.mark.parametrize("value", ["a/b", "", "x" * 65, "a b", None, 5])
async def test_get_log_pipeline_rejects_bad_ids(dd_fake, value):
    client = dd_fake({}, "log_config")
    with pytest.raises(DatadogInputError):
        await get_log_pipeline(value)
    assert client.calls == []


async def test_get_log_pipeline_unknown_id(dd_fake):
    dd_fake(
        {("GET", "/api/v1/logs/config/pipelines/zzz"): DatadogNotFoundError("not found")},
        "log_config",
    )
    with pytest.raises(DatadogNotFoundError):
        await get_log_pipeline("zzz")


@pytest.mark.parametrize("data", [[], {"name": "no id"}])
async def test_get_log_pipeline_malformed(dd_fake, data):
    dd_fake({("GET", "/api/v1/logs/config/pipelines/aaa"): data}, "log_config")
    with pytest.raises(DatadogResponseError):
        await get_log_pipeline("aaa")


async def test_log_config_reads_respect_read_policy(dd_fake, policy):
    from archie_plugins.datadog.client import DatadogPolicyError

    client = dd_fake({}, "log_config")
    policy({"datadog": {"read": {"enabled": False}}})
    with pytest.raises(DatadogPolicyError):
        await list_log_pipelines()
    with pytest.raises(DatadogPolicyError):
        await get_log_pipeline("aaa")
    for function in (list_log_indexes, list_log_metrics, list_scanning_groups):
        with pytest.raises(DatadogPolicyError):
            await function()
    assert client.calls == []


INDEXES = ("GET", "/api/v1/logs/config/indexes")
INDEX_ORDER = ("GET", "/api/v1/logs/config/index-order")
MAIN_INDEX = {
    "name": "main",
    "filter": {"query": "*"},
    "num_retention_days": 15,
    "num_flex_logs_retention_days": 30,
    "daily_limit": 300000000,
    "daily_limit_reset": {"reset_time": "14:00", "reset_utc_offset": "+00:00"},
    "daily_limit_warning_threshold_percentage": 70,
    "is_rate_limited": False,
    "tags": ["team:sre"],
    "exclusion_filters": [
        {
            "name": "drop health checks",
            "is_enabled": True,
            "filter": {"query": "@http.url:/health", "sample_rate": 1.0},
        },
        "junk",
    ],
}


async def test_list_log_indexes_in_routing_order(dd_fake):
    client = dd_fake(
        {
            INDEXES: {"indexes": [MAIN_INDEX, {"name": "payments"}, {"name": "orphan"}]},
            INDEX_ORDER: {"index_names": ["payments", "main"]},
        },
        "log_config",
    )
    result = await list_log_indexes()
    assert [call[:2] for call in client.calls] == [INDEXES, INDEX_ORDER]
    assert [(item["position"], item["name"]) for item in result] == [
        (1, "payments"),
        (2, "main"),
        (None, "orphan"),
    ]
    assert result[1] == {
        "position": 2,
        "name": "main",
        "filter": "*",
        "retention_days": 15,
        "flex_retention_days": 30,
        "daily_limit": 300000000,
        "daily_limit_reset": {"reset_time": "14:00", "reset_utc_offset": "+00:00"},
        "daily_limit_warning_threshold_percentage": 70,
        "is_rate_limited": False,
        "tags": ["team:sre"],
        "exclusion_filters": [
            {
                "name": "drop health checks",
                "enabled": True,
                "query": "@http.url:/health",
                "sample_rate": 1.0,
                "sample_attribute": None,
            }
        ],
        "truncated": False,
    }
    assert result[0]["exclusion_filters"] == [] and result[0]["filter"] is None


@pytest.mark.parametrize(
    "responses",
    [
        {INDEXES: [], INDEX_ORDER: {"index_names": []}},
        {INDEXES: {"indexes": []}, INDEX_ORDER: {"index_names": "main"}},
    ],
)
async def test_list_log_indexes_malformed(dd_fake, responses):
    dd_fake(responses, "log_config")
    with pytest.raises(DatadogResponseError):
        await list_log_indexes()


LOG_METRICS = ("GET", "/api/v2/logs/config/metrics")
LOG_METRIC_ITEMS = [
    {
        "id": "logs.web.requests",
        "type": "logs_metrics",
        "attributes": {
            "compute": {"aggregation_type": "count"},
            "filter": {"query": "service:web"},
            "group_by": [{"path": "@network.client.ip", "tag_name": "client_ip"}, "junk"],
        },
    },
    {
        "id": "logs.api.latency",
        "type": "logs_metrics",
        "attributes": {
            "compute": {
                "aggregation_type": "distribution",
                "path": "@duration",
                "include_percentiles": True,
            },
            "filter": {"query": "service:api"},
        },
    },
]


async def test_list_log_metrics_normalises_definitions(dd_fake):
    dd_fake({LOG_METRICS: {"data": LOG_METRIC_ITEMS}}, "log_config")
    result = await list_log_metrics()
    assert result == [
        {
            "id": "logs.web.requests",
            "aggregation": "count",
            "path": None,
            "include_percentiles": None,
            "filter": "service:web",
            "group_by": [{"path": "@network.client.ip", "tag_name": "client_ip"}],
            "truncated": False,
        },
        {
            "id": "logs.api.latency",
            "aggregation": "distribution",
            "path": "@duration",
            "include_percentiles": True,
            "filter": "service:api",
            "group_by": [],
            "truncated": False,
        },
    ]


@pytest.mark.parametrize(
    ("query", "expected"),
    [("LATENCY", ["logs.api.latency"]), ("service:web", ["logs.web.requests"]), ("zzz", [])],
)
async def test_list_log_metrics_filters_on_id_or_filter(dd_fake, query, expected):
    dd_fake({LOG_METRICS: {"data": LOG_METRIC_ITEMS}}, "log_config")
    assert [item["id"] for item in await list_log_metrics(query)] == expected


async def test_list_log_metrics_malformed(dd_fake):
    dd_fake({LOG_METRICS: {"data": {}}}, "log_config")
    with pytest.raises(DatadogResponseError):
        await list_log_metrics()


SCANNER = ("GET", "/api/v2/sensitive-data-scanner/config")
PATTERNS = ("GET", "/api/v2/sensitive-data-scanner/config/standard-patterns")


def _group(gid, name, rules):
    return {
        "id": gid,
        "type": "sensitive_data_scanner_group",
        "attributes": {
            "name": name,
            "description": "",
            "filter": {"query": "service:web"},
            "is_enabled": True,
            "product_list": ["logs"],
            "samplings": [{"product": "logs", "rate": 100.0}],
        },
        "relationships": {"rules": {"data": [{"id": rid} for rid in rules]}},
    }


def _rule(rid, name, standard=None, **attrs):
    item = {
        "id": rid,
        "type": "sensitive_data_scanner_rule",
        "attributes": {"name": name, "is_enabled": True, **attrs},
        "relationships": {"group": {"data": {"id": "g"}}},
    }
    if standard:
        item["relationships"]["standard_pattern"] = {"data": {"id": standard}}
    return item


def _config(order, included):
    return {
        "data": {
            "id": "cfg",
            "relationships": {"groups": {"data": [{"id": gid} for gid in order]}},
        },
        "included": included,
    }


async def test_list_scanning_groups_orders_groups_and_rules_and_names_patterns(dd_fake):
    config = _config(
        ["g2", "g1", "missing"],
        [
            _group("g1", "Cards", ["r2", "r1", "gone"]),
            _rule("r1", "Visa", standard="sp1"),
            _rule(
                "r2",
                "Path ids",
                pattern=r"/gift/[^/]+",
                namespaces=["http.url"],
                text_replacement={"type": "replacement_string", "replacement_string": "[x]"},
                priority=2,
            ),
            _group("g2", "Tokens", []),
            _group("g3", "Unordered", []),
        ],
    )
    client = dd_fake(
        {
            SCANNER: config,
            PATTERNS: {
                "data": [
                    {"id": "sp1", "attributes": {"name": "Visa Card Scanner", "pattern": "x"}},
                    {"id": "sp2", "attributes": {"name": "Unused"}},
                ]
            },
        },
        "log_config",
    )
    result = await list_scanning_groups()
    assert [call[:2] for call in client.calls] == [SCANNER, PATTERNS]
    assert [(group["position"], group["id"]) for group in result] == [
        (1, "g2"),
        (2, "g1"),
        (None, "g3"),
    ]
    cards = result[1]
    assert cards["filter"] == "service:web"
    assert cards["products"] == ["logs"]
    assert cards["samplings"] == [{"product": "logs", "rate": 100.0}]
    assert [rule["id"] for rule in cards["rules"]] == ["r2", "r1"]
    assert cards["rules"][0] == {
        "id": "r2",
        "name": "Path ids",
        "description": None,
        "enabled": True,
        "priority": 2,
        "pattern": r"/gift/[^/]+",
        "standard_pattern": None,
        "namespaces": ["http.url"],
        "excluded_namespaces": None,
        "text_replacement": {"type": "replacement_string", "replacement_string": "[x]"},
        "keywords": None,
        "suppressions": None,
        "tags": None,
    }
    assert cards["rules"][1]["standard_pattern"] == {"id": "sp1", "name": "Visa Card Scanner"}
    assert cards["rules"][1]["pattern"] is None


async def test_list_scanning_groups_skips_pattern_lookup_without_standard_rules(dd_fake):
    config = _config(["g1"], [_group("g1", "Cards", ["r1"]), _rule("r1", "Mine", pattern="x")])
    client = dd_fake({SCANNER: config}, "log_config")
    result = await list_scanning_groups()
    assert len(client.calls) == 1
    assert result[0]["rules"][0]["pattern"] == "x"


@pytest.mark.parametrize("data", [[], {"included": "x"}])
async def test_list_scanning_groups_malformed(dd_fake, data):
    dd_fake({SCANNER: data}, "log_config")
    with pytest.raises(DatadogResponseError):
        await list_scanning_groups()


async def test_list_scanning_groups_missing_permission(dd_fake):
    dd_fake(
        {SCANNER: DatadogAuthorizationError("Datadog authorization denied: data_scanner_read")},
        "log_config",
    )
    with pytest.raises(DatadogAuthorizationError, match="data_scanner_read"):
        await list_scanning_groups()

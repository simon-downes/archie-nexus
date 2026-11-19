"""Log configuration read tests with a fake client (no network)."""

import pytest
from archie_plugins.datadog.client import (
    DatadogAuthorizationError,
    DatadogInputError,
    DatadogNotFoundError,
    DatadogResponseError,
)
from archie_plugins.datadog.log_config import get_log_pipeline, list_log_pipelines

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
    assert client.calls == []

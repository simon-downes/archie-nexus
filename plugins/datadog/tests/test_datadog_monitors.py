"""Datadog monitor tool tests with a fake client (no network)."""

import asyncio
import contextvars
import json
import sys
from contextlib import asynccontextmanager

import pytest
from archie_agent.exec.envelope import Envelope
from archie_agent.exec.plugin import plugin_tools
from archie_agent.exec.runner import run
from archie_agent.exec.tools import get_all_tools
from archie_plugins.datadog.client import (
    DatadogInputError,
    DatadogMutationIndeterminateError,
    DatadogOwnershipError,
    DatadogPolicyError,
    DatadogRequestRejectedError,
    DatadogResponseError,
    DatadogTransportError,
)
from archie_plugins.datadog.monitors import (
    create_monitor,
    get_monitor,
    search_monitors,
    update_monitor,
    validate_monitor,
)

SEARCH = ("GET", "/api/v1/monitor/search")
VALIDATE = ("POST", "/api/v1/monitor/validate")

MONITOR = {
    "id": 42,
    "name": "API 5xx",
    "type": "query alert",
    "overall_state": "Alert",
    "query": "sum(last_5m):sum:trace.http.request.errors{service:api} > 10",
    "message": "Page @oncall",
    "tags": ["service:api", "managedby:pulumi"],
    "priority": 2,
    "created": "2023-10-25T13:41:34.394640+00:00",
    "modified": "2024-01-01T00:00:00+00:00",
    "options": {"thresholds": {"critical": 10}, "restricted_roles": ["x"], "silenced": {}},
}

DEFINITION = {
    "name": "x",
    "type": "metric alert",
    "query": "avg(last_5m):avg:system.cpu.user{*} > 90",
    "message": "x",
}


async def test_search_monitors_sends_query_and_normalises(dd_fake):
    summary = {**MONITOR, "status": "Alert", "created": 1_700_000_000}
    summary.pop("overall_state")
    client = dd_fake({SEARCH: {"monitors": [summary, summary]}}, "monitors")
    result = await search_monitors("status:alert", limit=1)
    assert client.calls[0][2] == {"per_page": 1, "page": 0, "query": "status:alert"}
    assert len(result) == 1
    assert result[0]["status"] == "Alert"
    assert result[0]["managed"] is False
    assert result[0]["created"] == "2023-11-14T22:13:20Z"


async def test_search_monitors_without_query(dd_fake):
    client = dd_fake({SEARCH: {"monitors": []}}, "monitors")
    assert await search_monitors() == []
    assert "query" not in client.calls[0][2]


async def test_get_monitor_detail_keeps_only_allowed_options(dd_fake):
    dd_fake({("GET", "/api/v1/monitor/42"): MONITOR}, "monitors")
    result = await get_monitor(42)
    assert result["message"] == "Page @oncall"
    assert result["options"] == {"thresholds": {"critical": 10}}
    assert result["status"] == "Alert"


async def test_managed_flag_reflects_ownership_tag(dd_fake):
    dd_fake(
        {("GET", "/api/v1/monitor/7"): {**MONITOR, "id": 7, "tags": ["managedby:archie"]}},
        "monitors",
    )
    assert (await get_monitor(7))["managed"] is True


@pytest.mark.parametrize("value", [0, -1, True, "42", 4.2])
async def test_get_monitor_rejects_bad_ids(dd_fake, value):
    client = dd_fake({}, "monitors")
    with pytest.raises(DatadogInputError):
        await get_monitor(value)
    assert client.calls == []


async def test_validate_monitor_adds_tag_and_needs_only_read(dd_fake):
    client = dd_fake({VALIDATE: {}}, "monitors")
    assert await validate_monitor(**DEFINITION, tags=["team:x"], priority=3) == {"valid": True}
    body = client.calls[0][3]
    assert body["tags"] == ["team:x", "managedby:archie"]
    assert body["priority"] == 3


async def test_validate_monitor_rejection_surfaces(dd_fake):
    dd_fake(
        {VALIDATE: DatadogRequestRejectedError("Datadog rejected the request: bad query")},
        "monitors",
    )
    with pytest.raises(DatadogRequestRejectedError, match="bad query"):
        await validate_monitor(**DEFINITION)


@pytest.mark.parametrize(
    "change",
    [
        {"type": "metric"},
        {"name": ""},
        {"query": ""},
        {"priority": 6},
        {"tags": ["ok", ""]},
        {"tags": [f"t:{i}" for i in range(50)]},
        {"options": {"restricted_roles": ["admin"]}},
        {"options": ["thresholds"]},
    ],
)
async def test_validate_monitor_input_errors(dd_fake, change):
    client = dd_fake({}, "monitors")
    with pytest.raises(DatadogInputError):
        await validate_monitor(**{**DEFINITION, **change})
    assert client.calls == []


async def test_read_disabled_blocks_validate(dd_fake, policy):
    policy({"datadog": {"read": {"enabled": False}}})
    client = dd_fake({}, "monitors")
    with pytest.raises(DatadogPolicyError):
        await validate_monitor(**DEFINITION)
    assert client.calls == []


def test_runner_exposes_datadog_namespace(tmp_path, monkeypatch):
    from archie_plugins.datadog import monitors as monitors_module

    class Client:
        async def request(self, method, path, *, params=None, json=None):
            assert (method, path) == SEARCH
            return {"monitors": [MONITOR]}

    @asynccontextmanager
    async def factory():
        yield Client()

    monkeypatch.setattr(sys.modules[monitors_module.__name__], "datadog_client", factory)
    (tmp_path / "main.py").write_text(
        "async def main():\n    return await datadog.search_monitors('status:alert')\n"
    )
    (tmp_path / "context.json").write_text(
        json.dumps({"policy": {"datadog": {"read": {"enabled": True}}}, "enabled": ["datadog"]})
    )
    contextvars.copy_context().run(run, tmp_path)
    envelope = Envelope.read(tmp_path)
    assert envelope.ok is True, envelope.error
    assert envelope.return_value[0]["id"] == 42


CREATE = ("POST", "/api/v1/monitor")
GET42 = ("GET", "/api/v1/monitor/42")
PUT42 = ("PUT", "/api/v1/monitor/42")
OWNED = {**MONITOR, "tags": ["service:api", "managedby:archie"]}
WRITE = {"datadog": {"write": {"enabled": True}}}


async def test_write_disabled_by_default_makes_no_request(dd_fake):
    client = dd_fake({}, "monitors")
    with pytest.raises(DatadogPolicyError, match="writes are disabled"):
        await create_monitor(**DEFINITION)
    with pytest.raises(DatadogPolicyError, match="writes are disabled"):
        await update_monitor(42, name="y")
    assert client.calls == []


async def test_create_monitor_adds_ownership_tag(dd_fake, policy):
    policy(WRITE)
    client = dd_fake({CREATE: {**OWNED, "id": 99}}, "monitors")
    result = await create_monitor(**DEFINITION, tags=["team:x", "managedby:archie"])
    method, path, _, body = client.calls[0]
    assert (method, path) == CREATE
    assert body["tags"] == ["team:x", "managedby:archie"]
    assert body["type"] == "metric alert"
    assert result["id"] == 99 and result["managed"] is True


async def test_update_refuses_untagged_monitor_without_put(dd_fake, policy):
    policy(WRITE)
    client = dd_fake({GET42: MONITOR}, "monitors")
    with pytest.raises(DatadogOwnershipError, match="managedby:archie"):
        await update_monitor(42, name="hijack")
    assert [call[:2] for call in client.calls] == [GET42]


async def test_update_keeps_ownership_tag(dd_fake, policy):
    policy(WRITE)
    client = dd_fake({GET42: OWNED, PUT42: OWNED}, "monitors")
    await update_monitor(42, tags=["team:x"])
    assert client.calls[1][3] == {"tags": ["team:x", "managedby:archie"]}


async def test_update_sends_only_given_fields_and_merges_options(dd_fake, policy):
    policy(WRITE)
    current = {**OWNED, "options": {"thresholds": {"critical": 10}, "notify_no_data": True}}
    client = dd_fake({GET42: current, PUT42: {**current, "message": "new"}}, "monitors")
    result = await update_monitor(42, message="new", options={"thresholds": {"critical": 5}})
    assert [call[:2] for call in client.calls] == [GET42, PUT42]
    assert result["message"] == "new"
    assert client.calls[1][3] == {
        "message": "new",
        "options": {"thresholds": {"critical": 5}, "notify_no_data": True},
    }
    assert result["id"] == 42


async def test_update_with_no_fields_is_input_error(dd_fake, policy):
    policy(WRITE)
    client = dd_fake({}, "monitors")
    with pytest.raises(DatadogInputError):
        await update_monitor(42)
    assert client.calls == []


async def test_update_rejects_restricted_options(dd_fake, policy):
    policy(WRITE)
    client = dd_fake({}, "monitors")
    with pytest.raises(DatadogInputError, match="restricted_roles"):
        await update_monitor(42, options={"restricted_roles": ["admin"]})
    assert client.calls == []


@pytest.mark.parametrize(
    "failure",
    [
        DatadogResponseError("Datadog returned HTTP 500"),
        DatadogTransportError("Datadog request outcome is uncertain", transmitted=True),
    ],
)
async def test_create_failure_after_send_is_indeterminate(dd_fake, policy, failure):
    policy(WRITE)
    dd_fake({CREATE: failure}, "monitors")
    with pytest.raises(DatadogMutationIndeterminateError, match="verify"):
        await create_monitor(**DEFINITION)


async def test_create_malformed_body_is_indeterminate(dd_fake, policy):
    policy(WRITE)
    dd_fake({CREATE: {"unexpected": True}}, "monitors")
    with pytest.raises(DatadogMutationIndeterminateError):
        await create_monitor(**DEFINITION)


async def test_create_rejected_is_not_indeterminate(dd_fake, policy):
    policy(WRITE)
    dd_fake({CREATE: DatadogRequestRejectedError("Datadog rejected the request: bad")}, "monitors")
    with pytest.raises(DatadogRequestRejectedError):
        await create_monitor(**DEFINITION)


async def test_create_connect_error_is_plain_transport_error(dd_fake, policy):
    policy(WRITE)
    dd_fake(
        {CREATE: DatadogTransportError("could not be completed", transmitted=False)}, "monitors"
    )
    with pytest.raises(DatadogTransportError):
        await create_monitor(**DEFINITION)


async def test_update_read_error_on_put_is_indeterminate(dd_fake, policy):
    policy(WRITE)
    failure = DatadogTransportError("uncertain", transmitted=True)
    dd_fake({GET42: OWNED, PUT42: failure}, "monitors")
    with pytest.raises(DatadogMutationIndeterminateError):
        await update_monitor(42, name="y")


async def test_update_5xx_on_put_is_indeterminate(dd_fake, policy):
    policy(WRITE)
    dd_fake({GET42: OWNED, PUT42: DatadogResponseError("Datadog returned HTTP 502")}, "monitors")
    with pytest.raises(DatadogMutationIndeterminateError):
        await update_monitor(42, name="y")


async def test_update_success_with_odd_body_says_updated(dd_fake, policy):
    policy(WRITE)
    dd_fake({GET42: OWNED, PUT42: {"unexpected": True}}, "monitors")
    with pytest.raises(DatadogResponseError, match="was updated"):
        await update_monitor(42, name="y")


async def test_deadline_during_write_is_indeterminate(monkeypatch, policy):
    from archie_plugins.datadog import client as client_module

    policy(WRITE)
    monkeypatch.setattr(client_module, "_DEADLINE_SECONDS", 0.05)

    class SlowClient:
        async def request(self, method, path, *, params=None, json=None):
            if method == "GET":
                return OWNED
            await asyncio.sleep(1)

    @asynccontextmanager
    async def factory():
        async with client_module.datadog_client():
            yield SlowClient()

    monkeypatch.setattr(sys.modules["archie_plugins.datadog.monitors"], "datadog_client", factory)
    with pytest.raises(DatadogMutationIndeterminateError):
        await update_monitor(42, name="y")
    with pytest.raises(DatadogMutationIndeterminateError):
        await create_monitor(**DEFINITION)


async def test_deadline_before_write_is_transport_error(monkeypatch, policy):
    from archie_plugins.datadog import client as client_module

    policy(WRITE)
    monkeypatch.setattr(client_module, "_DEADLINE_SECONDS", 0.05)

    class SlowGet:
        async def request(self, method, path, *, params=None, json=None):
            assert method == "GET", "no write may be sent"
            await asyncio.sleep(1)

    @asynccontextmanager
    async def factory():
        async with client_module.datadog_client():
            yield SlowGet()

    monkeypatch.setattr(sys.modules["archie_plugins.datadog.monitors"], "datadog_client", factory)
    with pytest.raises(DatadogTransportError):
        await update_monitor(42, name="y")


PUBLIC = {
    "create_monitor",
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
    "update_monitor",
    "validate_monitor",
}


def test_registration_is_exactly_the_public_surface():
    tools = plugin_tools()
    keys = {key.removeprefix("datadog.") for key in tools if key.startswith("datadog.")}
    assert keys == PUBLIC
    core = get_all_tools()
    for name in PUBLIC:
        assert tools[f"datadog.{name}"]._plugin == "datadog"
        assert f"datadog.{name}" not in core


async def test_every_public_function_stays_inside_the_allowlist(monkeypatch, policy):
    """Drive every function through the real client and a recording transport."""
    import archie_plugins.datadog as package
    import httpx
    from archie_plugins.datadog.client import DatadogClient, allowed
    from archie_plugins.datadog.discovery import SOURCES

    policy(WRITE)
    seen = []
    bodies = {
        "/api/v1/query": {"series": []},
        "/api/v1/search": {"results": {"metrics": []}},
        "/api/v1/metrics": {"metrics": []},
        "/api/v1/metrics/x": {"type": "gauge"},
        "/api/v2/metrics/x/all-tags": {"data": {"attributes": {"tags": []}}},
        "/api/v2/metrics/x/assets": {"included": []},
        "/api/v2/logs/events/search": {"data": []},
        "/api/v2/logs/analytics/aggregate": {"data": {"buckets": []}},
        "/api/v2/spans/analytics/aggregate": {"data": []},
        "/api/v2/spans/events/search": {"data": []},
        "/api/v2/events": {"data": []},
        "/api/v1/monitor/search": {"monitors": []},
        "/api/v1/monitor/validate": {},
        "/api/v1/monitor": OWNED,
        "/api/v1/monitor/42": OWNED,
        "/api/v1/dashboard": {"dashboards": []},
        "/api/v1/dashboard/abc-def-ghi": {"id": "abc-def-ghi", "widgets": []},
        "/api/v2/catalog/entity": {"data": []},
        "/api/v2/team": {"data": []},
    }

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json=bodies[request.url.path])

    @asynccontextmanager
    async def factory():
        yield DatadogClient("k", "a", "https://api.datadoghq.eu", httpx.MockTransport(handler))

    modules = ("metrics", "logs", "traces", "events", "monitors", "dashboards", "catalog")
    for module in (*modules, "discovery"):
        monkeypatch.setattr(
            sys.modules[f"archie_plugins.datadog.{module}"], "datadog_client", factory
        )
    calls = {
        "query_metrics": ("avg:x{*}",),
        "search_metrics": ("x",),
        "describe_metric": ("x",),
        "search_logs": ("*",),
        "search_spans": ("*",),
        "list_events": (),
        "search_monitors": (),
        "get_monitor": (42,),
        "validate_monitor": tuple(DEFINITION.values()),
        "create_monitor": tuple(DEFINITION.values()),
        "update_monitor": (42, "renamed"),
        "list_dashboards": (),
        "get_dashboard": ("abc-def-ghi",),
        "list_catalog_entities": (),
        "list_teams": (),
        "search": ("queue:x", list(SOURCES)),
    }
    assert set(calls) == PUBLIC
    for name, args in calls.items():
        await getattr(package, name)(*args)
    await package.search_metrics(tag="queue:*", active_within="1d")
    for path in ("/api/v1/metrics", "/api/v2/logs/analytics/aggregate"):
        assert ("GET", path) in seen or ("POST", path) in seen
    assert seen
    assert all(allowed(method, path) for method, path in seen)
    assert all(method != "DELETE" for method, _ in seen)
    assert {method for method, _ in seen} == {"GET", "POST", "PUT"}

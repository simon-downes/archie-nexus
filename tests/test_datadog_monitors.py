"""Datadog monitor tool tests with a fake client (no network)."""

import contextvars
import json
import sys
from contextlib import asynccontextmanager

import pytest
from archie_agent.exec.envelope import Envelope
from archie_agent.exec.runner import run
from archie_agent.exec.tools.datadog.client import (
    DatadogInputError,
    DatadogPolicyError,
    DatadogRequestRejectedError,
)
from archie_agent.exec.tools.datadog.monitors import (
    get_monitor,
    search_monitors,
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


async def test_search_monitors_sends_query_and_normalises(fake):
    summary = {**MONITOR, "status": "Alert", "created": 1_700_000_000}
    summary.pop("overall_state")
    client = fake({SEARCH: {"monitors": [summary, summary]}}, "monitors")
    result = await search_monitors("status:alert", limit=1)
    assert client.calls[0][2] == {"per_page": 1, "page": 0, "query": "status:alert"}
    assert len(result) == 1
    assert result[0]["status"] == "Alert"
    assert result[0]["managed"] is False
    assert result[0]["created"] == "2023-11-14T22:13:20Z"


async def test_search_monitors_without_query(fake):
    client = fake({SEARCH: {"monitors": []}}, "monitors")
    assert await search_monitors() == []
    assert "query" not in client.calls[0][2]


async def test_get_monitor_detail_keeps_only_allowed_options(fake):
    fake({("GET", "/api/v1/monitor/42"): MONITOR}, "monitors")
    result = await get_monitor(42)
    assert result["message"] == "Page @oncall"
    assert result["options"] == {"thresholds": {"critical": 10}}
    assert result["status"] == "Alert"


async def test_managed_flag_reflects_ownership_tag(fake):
    fake(
        {("GET", "/api/v1/monitor/7"): {**MONITOR, "id": 7, "tags": ["managedby:archie"]}},
        "monitors",
    )
    assert (await get_monitor(7))["managed"] is True


@pytest.mark.parametrize("value", [0, -1, True, "42", 4.2])
async def test_get_monitor_rejects_bad_ids(fake, value):
    client = fake({}, "monitors")
    with pytest.raises(DatadogInputError):
        await get_monitor(value)
    assert client.calls == []


async def test_validate_monitor_adds_tag_and_needs_only_read(fake):
    client = fake({VALIDATE: {}}, "monitors")
    assert await validate_monitor(**DEFINITION, tags=["team:x"], priority=3) == {"valid": True}
    body = client.calls[0][3]
    assert body["tags"] == ["team:x", "managedby:archie"]
    assert body["priority"] == 3


async def test_validate_monitor_rejection_surfaces(fake):
    fake(
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
async def test_validate_monitor_input_errors(fake, change):
    client = fake({}, "monitors")
    with pytest.raises(DatadogInputError):
        await validate_monitor(**{**DEFINITION, **change})
    assert client.calls == []


async def test_read_disabled_blocks_validate(fake, policy):
    policy({"datadog": {"read": {"enabled": False}}})
    client = fake({}, "monitors")
    with pytest.raises(DatadogPolicyError):
        await validate_monitor(**DEFINITION)
    assert client.calls == []


def test_runner_exposes_datadog_namespace(tmp_path, monkeypatch):
    from archie_agent.exec.tools.datadog import monitors as monitors_module

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
    (tmp_path / "context.json").write_text(json.dumps({"datadog": {"read": {"enabled": True}}}))
    # run() installs the policy snapshot without resetting it; isolate it from later tests.
    contextvars.copy_context().run(run, tmp_path)
    envelope = Envelope.read(tmp_path)
    assert envelope.ok is True, envelope.error
    assert envelope.return_value[0]["id"] == 42

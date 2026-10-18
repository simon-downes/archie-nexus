"""Datadog client, credential, host, policy and error-mapping tests (no network)."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx
import pytest
from archie_plugins.datadog import client as client_module
from archie_plugins.datadog.client import (
    DatadogAuthenticationError,
    DatadogAuthorizationError,
    DatadogClient,
    DatadogConfigurationError,
    DatadogNotFoundError,
    DatadogPolicyError,
    DatadogRateLimitError,
    DatadogRequestRejectedError,
    DatadogResponseError,
    DatadogTransportError,
    api_host,
    datadog_client,
)
from archie_plugins.datadog.metrics import query_metrics
from archie_plugins.datadog.policy import require_read, require_write
from archie_shared.credentials.models import CREDENTIAL_TYPES

API_KEY = "api-secret-123"
APP_KEY = "app-secret-456"
HOST = "https://api.datadoghq.eu"
DatadogCredential = CREDENTIAL_TYPES["datadog"]


def _client(handler, seen=None) -> DatadogClient:
    def record(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    return DatadogClient(API_KEY, APP_KEY, HOST, transport=httpx.MockTransport(record))


async def test_request_sends_keys_as_headers_to_host():
    seen = []
    client = _client(lambda request: httpx.Response(200, json={"ok": True}), seen)
    assert await client.request("GET", "/api/v1/query", params={"query": "x"}) == {"ok": True}
    assert str(seen[0].url) == f"{HOST}/api/v1/query?query=x"
    assert seen[0].headers["DD-API-KEY"] == API_KEY
    assert seen[0].headers["DD-APPLICATION-KEY"] == APP_KEY


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("DELETE", "/api/v1/monitor/1"),
        ("DELETE", "/api/v1/dashboard/abc-def-ghi"),
        ("PUT", "/api/v1/dashboard/abc-def-ghi"),
        ("POST", "/api/v1/dashboard"),
        ("POST", "/api/v1/events"),
        ("GET", "/api/v1/monitor/1/../../dashboard"),
        ("GET", "/api/v2/downtime"),
        ("POST", "/api/v1/monitor/1/mute"),
        ("PUT", "/api/v1/metrics/x"),
        ("DELETE", "/api/v2/metrics/x/tags"),
        ("GET", "/api/v1/metrics/a/../b"),
        ("GET", "/api/v1/metrics/a%2Fb"),
        ("GET", "/api/v2/metrics/x/tags"),
        ("GET", "/api/v2/metrics/x/volumes"),
        ("GET", "/api/v1/metrics/1abc"),
    ],
)
async def test_requests_outside_allowlist_are_refused_without_sending(method, path):
    seen = []
    client = _client(lambda request: httpx.Response(200, json={}), seen)
    with pytest.raises(DatadogPolicyError):
        await client.request(method, path)
    assert seen == []


def test_allowlist_has_no_delete():
    assert all(method != "DELETE" for method, _ in client_module._ALLOWED)


@pytest.mark.parametrize(
    ("site", "host"),
    [
        ("app.datadoghq.eu", "https://api.datadoghq.eu"),
        ("datadoghq.eu", "https://api.datadoghq.eu"),
        ("us5.datadoghq.com", "https://api.us5.datadoghq.com"),
        ("app.datadoghq.com", "https://api.datadoghq.com"),
        ("ddog-gov.com", "https://api.ddog-gov.com"),
    ],
)
def test_api_host_from_known_site(site, host):
    assert api_host(site) == host


@pytest.mark.parametrize(
    "site", ["evil.example", "app.evil.example", "datadoghq.eu.evil", None, ""]
)
def test_unknown_site_is_configuration_error(site):
    with pytest.raises(DatadogConfigurationError):
        api_host(site)


@pytest.mark.parametrize(
    "credential",
    [None, DatadogCredential(api_key="a"), DatadogCredential(api_key=" ", app_key="b")],
)
async def test_missing_credentials_fail_before_request(credential):
    with patch.object(client_module, "get_credential", return_value=credential):
        with pytest.raises(DatadogConfigurationError):
            async with datadog_client():
                pass


async def test_unreadable_credential_store_is_configuration_error():
    with patch.object(client_module, "get_credential", side_effect=RuntimeError("bad")):
        with pytest.raises(DatadogConfigurationError):
            async with datadog_client():
                pass


async def test_config_load_failure_is_configuration_error():
    credential = DatadogCredential(api_key=API_KEY, app_key=APP_KEY)
    with (
        patch.object(client_module, "get_credential", return_value=credential),
        patch("archie_shared.schemas.load_nexus_config", side_effect=ValueError("broken yaml")),
    ):
        with pytest.raises(DatadogConfigurationError, match="site configuration"):
            async with datadog_client():
                pass


async def test_default_site_sends_to_eu_host(monkeypatch):
    credential = DatadogCredential(api_key=API_KEY, app_key=APP_KEY)
    seen = []
    real = client_module.DatadogClient

    def build(api_key, app_key, host):
        return real(
            api_key,
            app_key,
            host,
            transport=httpx.MockTransport(
                lambda request: seen.append(str(request.url)) or httpx.Response(200, json={})
            ),
        )

    monkeypatch.setattr(client_module, "DatadogClient", build)
    with patch.object(client_module, "get_credential", return_value=credential):
        async with datadog_client() as client:
            await client.request("GET", "/api/v2/team")
    assert seen == [f"{HOST}/api/v2/team"]


async def test_configured_site_override_changes_host(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  datadog:\n    settings:\n      site: us5.datadoghq.com\n"
    )
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    assert client_module.api_host(client_module._site()) == "https://api.us5.datadoghq.com"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, DatadogAuthenticationError),
        (403, DatadogAuthorizationError),
        (404, DatadogNotFoundError),
        (400, DatadogRequestRejectedError),
        (422, DatadogRequestRejectedError),
        (500, DatadogResponseError),
        (503, DatadogResponseError),
    ],
)
async def test_status_mapping_never_leaks_keys(status, error):
    body = {"errors": [f"bad key {API_KEY} and {APP_KEY}"]}
    with pytest.raises(error) as caught:
        await _client(lambda request: httpx.Response(status, json=body)).request(
            "GET", "/api/v1/query"
        )
    assert API_KEY not in str(caught.value)
    assert APP_KEY not in str(caught.value)


async def test_rejected_request_carries_provider_message():
    body = {"errors": ["invalid_argument(Cannot parse query: 'status:((')" + "x" * 500]}
    with pytest.raises(DatadogRequestRejectedError) as caught:
        await _client(lambda request: httpx.Response(400, json=body)).request(
            "POST", "/api/v2/logs/events/search"
        )
    message = str(caught.value)
    assert "Cannot parse query: 'status:(('" in message
    assert len(message) < 340


async def test_v2_error_objects_use_detail():
    body = {"errors": [{"status": "400", "title": "Bad Request", "detail": "invalid filter"}]}
    with pytest.raises(DatadogRequestRejectedError, match="invalid filter"):
        await _client(lambda request: httpx.Response(400, json=body)).request(
            "GET", "/api/v2/events"
        )


@pytest.mark.parametrize(("header", "expected"), [("7", 7), ("soon", None), (None, None)])
async def test_rate_limit_reports_reset_seconds(header, expected):
    headers = {"X-RateLimit-Reset": header} if header else {}
    with pytest.raises(DatadogRateLimitError) as caught:
        await _client(lambda request: httpx.Response(429, headers=headers)).request(
            "GET", "/api/v1/query"
        )
    assert caught.value.retry_after_seconds == expected


async def test_malformed_json_is_response_error():
    with pytest.raises(DatadogResponseError):
        await _client(lambda request: httpx.Response(200, text="<html>")).request(
            "GET", "/api/v1/query"
        )
    with pytest.raises(DatadogResponseError):
        await _client(lambda request: httpx.Response(200, json="x")).request("GET", "/api/v1/query")


async def test_connect_error_is_untransmitted_transport_error():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(DatadogTransportError) as caught:
        await _client(handler).request("GET", "/api/v1/query")
    assert caught.value.transmitted is False


async def test_read_error_is_transmitted_transport_error():
    def handler(request):
        raise httpx.ReadError("boom", request=request)

    with pytest.raises(DatadogTransportError) as caught:
        await _client(handler).request("PUT", "/api/v1/monitor/5")
    assert caught.value.transmitted is True


async def test_overall_deadline_is_transport_error(monkeypatch):
    credential = DatadogCredential(api_key=API_KEY, app_key=APP_KEY)
    monkeypatch.setattr(client_module, "_DEADLINE_SECONDS", 0.01)
    with patch.object(client_module, "get_credential", return_value=credential):
        with pytest.raises(DatadogTransportError, match="time limit"):
            async with datadog_client():
                await asyncio.sleep(1)


def test_policy_defaults_read_on_write_off(policy):
    require_read()
    with pytest.raises(DatadogPolicyError, match="writes are disabled"):
        require_write()


def test_policy_read_disabled(policy):
    policy({"datadog": {"read": {"enabled": False}}})
    with pytest.raises(DatadogPolicyError, match="reads are disabled"):
        require_read()


def test_policy_write_enabled(policy):
    policy({"datadog": {"write": {"enabled": True}}})
    require_write()


@pytest.mark.parametrize("block", ["read", "write"])
@pytest.mark.parametrize("gate", [require_read, require_write])
def test_policy_scope_in_either_block_fails_every_gate(policy, block, gate):
    policy(
        {
            "datadog": {
                "read": {"enabled": True},
                "write": {"enabled": True},
                block: {"enabled": True, "scope": ["team:x"]},
            }
        }
    )
    with pytest.raises(DatadogPolicyError, match="scope is not supported"):
        gate()


async def test_disabled_read_makes_no_request(policy, monkeypatch):
    policy({"datadog": {"read": {"enabled": False}}})
    calls = []

    @asynccontextmanager
    async def factory():
        calls.append(1)
        yield None

    monkeypatch.setattr("archie_plugins.datadog.metrics.datadog_client", factory)
    with pytest.raises(DatadogPolicyError):
        await query_metrics("avg:system.cpu.user{*}")
    assert calls == []

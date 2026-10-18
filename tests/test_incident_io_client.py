"""incident.io client, credential, policy and error-mapping tests (no network)."""

import asyncio
from unittest.mock import patch

import httpx
import pytest
from archie_agent.exec.tools.incident_io import client as client_module
from archie_agent.exec.tools.incident_io.client import (
    IncidentIoAuthenticationError,
    IncidentIoAuthorizationError,
    IncidentIoClient,
    IncidentIoConfigurationError,
    IncidentIoNotFoundError,
    IncidentIoPolicyError,
    IncidentIoRateLimitError,
    IncidentIoRequestRejectedError,
    IncidentIoResponseError,
    IncidentIoTransportError,
    incident_io_client,
)
from archie_agent.exec.tools.incident_io.incidents import list_incidents
from archie_agent.exec.tools.incident_io.policy import require_read
from archie_shared.credentials.models import CREDENTIAL_TYPES
from archie_shared.tool_policy import reset_policy_snapshot, set_policy_snapshot

KEY = "secret-test-key-123"


def _client(handler) -> IncidentIoClient:
    def guarded(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "incident.io client must only send GET"
        return handler(request)

    return IncidentIoClient(KEY, transport=httpx.MockTransport(guarded))


@pytest.fixture
def policy():
    tokens = []

    def install(snapshot):
        tokens.append(set_policy_snapshot(snapshot))

    yield install
    for token in reversed(tokens):
        reset_policy_snapshot(token)


async def test_get_sends_bearer_key_to_fixed_host():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={"ok": True})

    assert await _client(handler).get("/v1/identity") == {"ok": True}
    assert seen == {"url": "https://api.incident.io/v1/identity", "auth": f"Bearer {KEY}"}


def test_client_exposes_no_non_get_request_method():
    public = {name for name in dir(IncidentIoClient) if not name.startswith("_")}
    assert public == {"get", "items", "paginate"}


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, IncidentIoAuthenticationError),
        (403, IncidentIoAuthorizationError),
        (404, IncidentIoNotFoundError),
        (400, IncidentIoRequestRejectedError),
        (422, IncidentIoRequestRejectedError),
        (500, IncidentIoResponseError),
    ],
)
async def test_status_mapping_is_sanitized(status, error):
    body = {"errors": [{"code": "invalid_range", "message": f"echo {KEY}"}]}
    with pytest.raises(error) as caught:
        await _client(lambda request: httpx.Response(status, json=body)).get("/v2/incidents")
    assert KEY not in str(caught.value)
    assert "echo" not in str(caught.value)


async def test_rejected_request_reports_provider_codes_only():
    body = {"errors": [{"code": "invalid_range", "message": "page_size must be <= 50"}]}
    with pytest.raises(IncidentIoRequestRejectedError, match=r"\(invalid_range\)$"):
        await _client(lambda request: httpx.Response(422, json=body)).get("/v2/alerts")


@pytest.mark.parametrize(("header", "expected"), [("30", 30), ("soon", None), (None, None)])
async def test_rate_limit_reports_retry_after(header, expected):
    headers = {"Retry-After": header} if header else {}
    with pytest.raises(IncidentIoRateLimitError) as caught:
        await _client(lambda request: httpx.Response(429, headers=headers)).get("/v2/incidents")
    assert caught.value.retry_after_seconds == expected


async def test_malformed_json_is_response_error():
    with pytest.raises(IncidentIoResponseError):
        await _client(lambda request: httpx.Response(200, text="<html>")).get("/v2/incidents")
    with pytest.raises(IncidentIoResponseError):
        await _client(lambda request: httpx.Response(200, json=[1])).get("/v2/incidents")


async def test_network_failure_is_transport_error():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(IncidentIoTransportError):
        await _client(handler).get("/v2/incidents")


async def test_paginate_follows_cursor_and_caps_page_size():
    calls = []

    def handler(request):
        params = dict(request.url.params)
        calls.append(params)
        if "after" not in params:
            items = [{"id": str(i)} for i in range(int(params["page_size"]))]
            return httpx.Response(200, json={"things": items, "pagination_meta": {"after": "c1"}})
        items = [{"id": f"b{i}"} for i in range(int(params["page_size"]))]
        return httpx.Response(200, json={"things": items, "pagination_meta": {"after": "c2"}})

    result = await _client(handler).paginate("/x", "things", 30, max_page_size=25)
    assert len(result) == 30
    assert [c["page_size"] for c in calls] == ["25", "5"]
    assert calls[1]["after"] == "c1"


async def test_paginate_stops_when_cursor_missing():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"things": [{"id": "1"}], "pagination_meta": {}})

    assert len(await _client(handler).paginate("/x", "things", 100)) == 1
    assert len(calls) == 1


async def test_paginate_keep_filters_locally_with_full_pages():
    calls = []

    def handler(request):
        params = dict(request.url.params)
        calls.append(params)
        page = 2 if "after" in params else 1
        items = [{"id": f"{page}-{i}", "ok": i % 2 == 0} for i in range(4)]
        meta = {"after": "c1"} if page == 1 else {}
        return httpx.Response(200, json={"things": items, "pagination_meta": meta})

    result = await _client(handler).paginate(
        "/x", "things", 3, max_page_size=4, keep=lambda item: item["ok"]
    )
    assert [item["id"] for item in result] == ["1-0", "1-2", "2-0"]
    assert [c["page_size"] for c in calls] == ["4", "4"]


async def test_paginate_reads_at_most_max_pages():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            200, json={"things": [{"ok": False}], "pagination_meta": {"after": "more"}}
        )

    with patch.object(client_module, "_MAX_PAGES", 3):
        result = await _client(handler).paginate("/x", "things", 5, keep=lambda item: item["ok"])
    assert result == []
    assert len(calls) == 3


async def test_paginate_rejects_missing_collection():
    with pytest.raises(IncidentIoResponseError):
        await _client(lambda request: httpx.Response(200, json={})).paginate("/x", "things", 5)


def test_missing_or_blank_credential_is_configuration_error():
    for value in (
        None,
        CREDENTIAL_TYPES["incident_io"](api_key=None),
        CREDENTIAL_TYPES["incident_io"](api_key="  "),
    ):
        with patch.object(client_module, "get_credential", return_value=value):
            with pytest.raises(IncidentIoConfigurationError):
                client_module._credential()
    with patch.object(client_module, "get_credential", side_effect=ValueError("bad yaml")):
        with pytest.raises(IncidentIoConfigurationError):
            client_module._credential()


async def test_missing_credential_makes_no_request():
    token = set_policy_snapshot({})
    try:
        with (
            patch.object(client_module, "get_credential", return_value=None),
            patch("httpx.AsyncClient.get") as request,
        ):
            with pytest.raises(IncidentIoConfigurationError):
                await list_incidents()
            request.assert_not_called()
    finally:
        reset_policy_snapshot(token)


async def test_deadline_is_transport_error():
    with (
        patch.object(client_module, "_credential", return_value=KEY),
        patch.object(client_module, "_DEADLINE_SECONDS", 0.01),
    ):
        with pytest.raises(IncidentIoTransportError, match="time limit"):
            async with incident_io_client():
                await asyncio.sleep(1)


def test_policy_defaults_to_read_enabled(policy):
    policy({})
    require_read()


def test_policy_disabled_read_is_rejected(policy):
    policy({"incident_io": {"read": {"enabled": False}}})
    with pytest.raises(IncidentIoPolicyError, match="disabled"):
        require_read()


def test_policy_scope_is_unsupported(policy):
    policy({"incident_io": {"read": {"enabled": True, "scope": ["team-1"]}}})
    with pytest.raises(IncidentIoPolicyError, match="scope"):
        require_read()


async def test_disabled_policy_makes_no_request():
    token = set_policy_snapshot({"incident_io": {"read": {"enabled": False}}})
    try:
        with patch.object(client_module, "_credential") as credential:
            with pytest.raises(IncidentIoPolicyError):
                await list_incidents()
            credential.assert_not_called()
    finally:
        reset_policy_snapshot(token)

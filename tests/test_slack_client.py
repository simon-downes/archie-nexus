"""Slack transport request-shape tests."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from archie_agent.exec.tools.slack.client import api_call


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        ("users.conversations", {"exclude_archived": True, "limit": 200, "types": "im"}),
        ("users.list", {"limit": 200}),
        ("conversations.history", {"channel": "D1", "limit": 5}),
        ("conversations.replies", {"channel": "D1", "ts": "1.000000", "limit": 5}),
        ("reactions.get", {"channel": "D1", "timestamp": "1.000000", "full": True}),
    ],
)
async def test_read_endpoints_use_get_query_parameters(method, payload):
    response = httpx.Response(200, json={"ok": True, "channels": [], "members": [], "messages": []})
    transport = AsyncMock(return_value=response)
    with (
        patch(
            "archie_agent.exec.tools.slack.client._credential",
            return_value=type("C", (), {"access_token": "token"})(),
        ),
        patch("httpx.AsyncClient.get", transport),
    ):
        await api_call(method, payload)
    request = transport.await_args
    assert request.args[0] == f"https://slack.com/api/{method}"
    expected = {
        key: str(value).lower() if isinstance(value, bool) else value
        for key, value in payload.items()
    }
    if method == "users.conversations":
        expected["exclude_muted"] = "false"
    assert request.kwargs["params"] == expected
    assert request.kwargs["headers"]["Authorization"] == "Bearer token"


@pytest.mark.parametrize(
    "method", ["assistant.search.context", "chat.postMessage", "reactions.add", "reactions.remove"]
)
async def test_search_and_mutations_use_post_json(method):
    response = httpx.Response(200, json={"ok": True})
    transport = AsyncMock(return_value=response)
    with (
        patch(
            "archie_agent.exec.tools.slack.client._credential",
            return_value=type("C", (), {"access_token": "token"})(),
        ),
        patch("httpx.AsyncClient.post", transport),
    ):
        await api_call(method, {"value": "x"})
    request = transport.await_args
    assert request.args[0] == f"https://slack.com/api/{method}"
    assert request.kwargs["json"] == {"value": "x"}

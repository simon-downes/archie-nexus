"""Slack provider error classification tests."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from archie_agent.exec.tools.slack.client import api_call
from archie_agent.exec.tools.slack.errors import (
    SlackAuthenticationError,
    SlackReauthorizationRequiredError,
)


@pytest.mark.parametrize("error", ["token_expired", "token_revoked", "invalid_auth", "not_authed"])
async def test_authentication_errors_are_typed(error):
    response = httpx.Response(200, json={"ok": False, "error": error})
    with (
        patch(
            "archie_agent.exec.tools.slack.client._credential",
            return_value=type("C", (), {"access_token": "token"})(),
        ),
        patch("httpx.AsyncClient.get", new=AsyncMock(return_value=response)),
    ):
        with pytest.raises(SlackAuthenticationError):
            await api_call("conversations.history", {"channel": "D1", "limit": 5})


async def test_missing_scope_is_typed_reauthorization_error():
    response = httpx.Response(
        200, json={"ok": False, "error": "missing_scope", "needed": "im:history"}
    )
    with (
        patch(
            "archie_agent.exec.tools.slack.client._credential",
            return_value=type("C", (), {"access_token": "token"})(),
        ),
        patch("httpx.AsyncClient.get", new=AsyncMock(return_value=response)),
    ):
        with pytest.raises(SlackReauthorizationRequiredError) as error:
            await api_call("conversations.history", {"channel": "D1", "limit": 5})
    assert error.value.scopes == ("im:history",)

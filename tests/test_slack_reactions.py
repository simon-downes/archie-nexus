"""Slack reaction read tests."""

from unittest.mock import AsyncMock, patch

import pytest
from archie_agent.exec.tools.slack.errors import SlackResponseError, SlackValidationError
from archie_agent.exec.tools.slack.reactions import reactions


async def test_reactions_returns_safe_normalized_projection():
    payload = {
        "ok": True,
        "message": {
            "reactions": [
                {"name": "thumbsup", "count": 2, "users": ["U1", "U2"], "extra": "ignored"},
                {"name": "eyes", "count": 1, "users": ["U3"]},
            ],
            "text": "hidden",
        },
    }
    with patch(
        "archie_agent.exec.tools.slack.reactions.api_call", new=AsyncMock(return_value=payload)
    ) as call:
        result = await reactions("D1", "1.000000")
    assert call.await_args.args == (
        "reactions.get",
        {"channel": "D1", "timestamp": "1.000000", "full": True},
    )
    assert result == {
        "conversation": "D1",
        "timestamp": "1.000000",
        "reactions": [
            {"name": "thumbsup", "count": 2, "users": ["U1", "U2"]},
            {"name": "eyes", "count": 1, "users": ["U3"]},
        ],
    }


@pytest.mark.parametrize("timestamp", ["1", "1.1", "0.000000", "bad"])
async def test_invalid_timestamp_makes_no_request(timestamp):
    with patch("archie_agent.exec.tools.slack.reactions.api_call", new=AsyncMock()) as call:
        with pytest.raises(SlackValidationError):
            await reactions("D1", timestamp)
    call.assert_not_awaited()


async def test_malformed_reactions_raise_response_error():
    with patch(
        "archie_agent.exec.tools.slack.reactions.api_call",
        new=AsyncMock(return_value={"ok": True, "message": {"reactions": [{"name": "x"}]}}),
    ):
        with pytest.raises(SlackResponseError):
            await reactions("D1", "1.000000")

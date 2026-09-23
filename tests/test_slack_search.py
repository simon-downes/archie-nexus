"""Focused M1 Slack search boundary tests."""

from unittest.mock import AsyncMock, patch

import pytest
from archie_agent.exec.tools import get_all_tools
from archie_agent.exec.tools.slack.errors import SlackPolicyError, SlackValidationError
from archie_agent.exec.tools.slack.search import build_rts_request, search


def test_slack_registration_and_signature_boundary():
    tools = get_all_tools()
    function = tools["slack.search"]
    assert function._namespace == "slack"
    assert function._native is False
    assert function._exec_docs is False


def test_request_defaults_and_exact_timestamp_mapping():
    request, after, before = build_rts_request(
        "  hello  ", after="2025-01-01T01:00:00.123456+01:00", sort="timestamp"
    )
    assert request == {
        "query": "hello",
        "channel_types": ["public_channel", "private_channel", "im", "mpim"],
        "content_types": ["messages"],
        "limit": 20,
        "sort": "timestamp",
        "sort_dir": "desc",
        "include_context_messages": True,
        "after": "1735689600",
    }
    assert after is not None and before is None


@pytest.mark.parametrize("value", ["", "   ", True, 1])
def test_invalid_query_makes_no_request(value):
    with patch("archie_agent.exec.tools.slack.search.search_context", new=AsyncMock()) as call:
        with pytest.raises(SlackValidationError):
            import asyncio

            asyncio.run(search(value))
        call.assert_not_awaited()


async def test_direct_deny_makes_no_request():
    with patch("archie_agent.exec.tools.slack.search.search_context", new=AsyncMock()) as call:
        with patch(
            "archie_agent.exec.tools.slack.search.require_read",
            return_value={
                "enabled": True,
                "read": {"enabled": True, "scope": {"deny": ["c123"]}},
                "write": {"enabled": False, "scope": {"deny": []}},
            },
        ):
            with pytest.raises(SlackPolicyError):
                await search("hello", conversation="C123")
        call.assert_not_awaited()


async def test_search_accepts_documented_live_rts_message_envelope():
    payload = {
        "ok": True,
        "results": {
            "messages": [
                {
                    "author_user_id": "U1",
                    "channel_id": "D1",
                    "channel_name": "Henry Dennis",
                    "message_ts": "123456.7890",
                    "content": "hello",
                    "permalink": "https://slack.test/archives/D1/p123456789",
                }
            ],
            "context_messages": {"before": [], "after": []},
        },
    }
    with patch(
        "archie_agent.exec.tools.slack.search.search_context", new=AsyncMock(return_value=payload)
    ):
        result = await search("hello", channel_types=["im"], include_context=False)
    assert result[0]["id"] == "123456.7890"
    assert result[0]["conversation"] == {"id": "D1", "name": "Henry Dennis", "type": "dm"}
    assert result[0]["text"] == "hello"

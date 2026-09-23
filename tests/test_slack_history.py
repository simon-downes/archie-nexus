"""Slack history and thread behavior tests."""

from unittest.mock import AsyncMock, patch

import pytest
from archie_agent.exec.tools.slack.errors import SlackValidationError
from archie_agent.exec.tools.slack.messages import messages
from archie_agent.exec.tools.slack.thread import thread


def _message(ts, text):
    return {"ts": ts, "text": text, "user": "U1"}


async def test_messages_filters_exclusive_bounds_and_orders_newest_first():
    payload = {
        "ok": True,
        "messages": [
            _message("3.000000", "new"),
            _message("2.000000", "equal"),
            _message("1.000000", "old"),
        ],
    }
    with patch(
        "archie_agent.exec.tools.slack.messages.api_call", new=AsyncMock(return_value=payload)
    ):
        result = await messages("C1", after="1.000000", before="3.000000")
    assert [item["text"] for item in result] == ["equal"]


async def test_thread_parent_first_and_reply_chronological():
    payload = {
        "ok": True,
        "messages": [
            _message("3.000000", "late"),
            _message("1.000000", "parent"),
            _message("2.000000", "early"),
        ],
    }
    with patch(
        "archie_agent.exec.tools.slack.thread.api_call", new=AsyncMock(return_value=payload)
    ):
        result = await thread("C1", "1.000000")
    assert [item["text"] for item in result] == ["parent", "early", "late"]


async def test_invalid_history_bound_makes_no_request():
    with patch("archie_agent.exec.tools.slack.messages.api_call", new=AsyncMock()) as call:
        with pytest.raises(SlackValidationError):
            await messages("C1", after="not-a-timestamp")
    call.assert_not_awaited()

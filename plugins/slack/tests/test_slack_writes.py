"""Slack mutation contract tests."""

from unittest.mock import AsyncMock, patch

import pytest
from archie_plugins.slack.errors import (
    SlackMutationIndeterminateError,
    SlackPolicyError,
    SlackValidationError,
)
from archie_plugins.slack.writes import react, send_message

_WRITE_POLICY = {"slack": {"write": {"enabled": True}}}


async def test_send_message_exact_payload_and_projection(policy):
    policy(_WRITE_POLICY)
    with (
        patch(
            "archie_plugins.slack.writes.api_call",
            new=AsyncMock(
                return_value={"ok": True, "ts": "1.000001", "permalink": "https://slack.test/p/1"}
            ),
        ) as call,
    ):
        result = await send_message(
            "C1",
            "  unchanged  ",
            thread_ts="1.000000",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "*formatted*"}}],
        )
    assert call.await_args.args == (
        "chat.postMessage",
        {
            "channel": "C1",
            "text": ":archie:   unchanged  ",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "*formatted*"}}],
            "thread_ts": "1.000000",
        },
    )
    assert result == {
        "conversation_id": "C1",
        "message_ts": "1.000001",
        "thread_ts": "1.000000",
        "permalink": "https://slack.test/p/1",
    }


async def test_react_exact_add_and_result(policy):
    policy(_WRITE_POLICY)
    with (
        patch(
            "archie_plugins.slack.writes.api_call",
            new=AsyncMock(return_value={"ok": True}),
        ) as call,
    ):
        result = await react("C1", "1.000001", "thumbsup")
    assert call.await_args.args == (
        "reactions.add",
        {"channel": "C1", "timestamp": "1.000001", "name": "thumbsup"},
    )
    assert result == {
        "conversation": "C1",
        "timestamp": "1.000001",
        "reaction": "thumbsup",
        "action": "add",
    }


@pytest.mark.parametrize("value", [":thumbsup:", " bad", "bad ", "bad/name"])
async def test_invalid_reaction_makes_no_request(value):
    with patch("archie_plugins.slack.writes.api_call", new=AsyncMock()) as call:
        with pytest.raises(SlackValidationError):
            await react("C1", "1.000001", value)
    call.assert_not_awaited()


async def test_disabled_write_fails_before_provider():
    with patch("archie_plugins.slack.writes.api_call", new=AsyncMock()) as call:
        with pytest.raises(SlackPolicyError):
            await send_message("C1", "hello")
    call.assert_not_awaited()


async def test_malformed_send_success_is_indeterminate(policy):
    policy(_WRITE_POLICY)
    with (
        patch(
            "archie_plugins.slack.writes.api_call",
            new=AsyncMock(return_value={"ok": True}),
        ),
    ):
        with pytest.raises(SlackMutationIndeterminateError):
            await send_message("C1", "hello")

"""Slack credential and policy come from the plugin context (M11)."""

from unittest.mock import AsyncMock, patch

import pytest
from archie_agent.exec.plugin import PluginContextError, install_context
from archie_plugins.slack.client import api_call
from archie_plugins.slack.conversations import conversations
from archie_plugins.slack.errors import (
    SlackConfigurationError,
    SlackPolicyError,
    SlackValidationError,
)
from archie_plugins.slack.policy import policy as slack_policy
from archie_plugins.slack.policy import require_read
from archie_plugins.slack.writes import send_message
from slack_fakes import fake_credential, slack_context


def test_default_policy_reads_on_writes_off_deny_nothing():
    assert slack_policy() == {
        "read": {"enabled": True, "scope": {"deny": []}},
        "write": {"enabled": False, "scope": {"deny": []}},
    }


def test_policy_returns_both_deny_lists(policy):
    policy(
        {
            "slack": {
                "read": {"enabled": True, "scope": {"deny": ["#random"]}},
                "write": {"enabled": True, "scope": {"deny": ["C9"]}},
            }
        }
    )
    assert slack_policy() == {
        "read": {"enabled": True, "scope": {"deny": ["#random"]}},
        "write": {"enabled": True, "scope": {"deny": ["C9"]}},
    }


@pytest.mark.parametrize("scope", [None, {}])
def test_empty_scope_denies_nothing(policy, scope):
    policy({"slack": {"read": {"enabled": True, "scope": scope}}})
    assert require_read()["read"]["scope"] == {"deny": []}


def test_list_scope_is_still_invalid(policy):
    # Unchanged ``_block`` validation: the Slack scope is an object, never a bare list.
    policy({"slack": {"read": {"enabled": True, "scope": []}}})
    with pytest.raises(SlackValidationError, match="^Invalid Slack policy$"):
        require_read()


def test_disabled_read_raises_existing_message(policy):
    policy({"slack": {"read": {"enabled": False}}})
    with pytest.raises(SlackPolicyError, match="^Slack reads are disabled by policy$"):
        require_read()


@pytest.mark.parametrize("scope", [{"deny": "C1"}, {"deny": [1]}, {"allow": ["C1"]}, "C1"])
@pytest.mark.parametrize("block", ["read", "write"])
def test_invalid_scope_in_either_block_fails_closed(policy, scope, block):
    policy({"slack": {block: {"enabled": True, "scope": scope}}})
    with pytest.raises(SlackValidationError, match="^Invalid Slack policy$"):
        require_read()


async def test_read_disabled_makes_no_request(policy):
    policy({"slack": {"read": {"enabled": False}}})
    with patch("archie_plugins.slack.conversations.api_call", new=AsyncMock()) as call:
        with pytest.raises(SlackPolicyError):
            await conversations()
    call.assert_not_awaited()


async def test_read_deny_filters_conversations(policy):
    policy({"slack": {"read": {"enabled": True, "scope": {"deny": ["#general"]}}}})
    payload = {
        "ok": True,
        "channels": [
            {"id": "C1", "name": "general", "is_channel": True},
            {"id": "C2", "name": "eng", "is_channel": True},
        ],
    }
    with patch("archie_plugins.slack.conversations.api_call", new=AsyncMock(return_value=payload)):
        result = await conversations()
    assert [item["id"] for item in result] == ["C2"]


async def test_write_deny_blocks_send_before_provider(policy):
    policy({"slack": {"write": {"enabled": True, "scope": {"deny": ["C1"]}}}})
    with patch("archie_plugins.slack.writes.api_call", new=AsyncMock()) as call:
        with pytest.raises(SlackPolicyError, match="^Slack conversation is denied by policy$"):
            await send_message("C1", "hello")
    call.assert_not_awaited()


async def test_disabled_write_keeps_existing_message():
    with patch("archie_plugins.slack.writes.api_call", new=AsyncMock()) as call:
        with pytest.raises(SlackPolicyError, match="^Slack writes are disabled by policy$"):
            await send_message("C1", "hello")
    call.assert_not_awaited()


@pytest.mark.parametrize("credential", [None, fake_credential(access_token="  ")])
async def test_missing_or_blank_token_is_incomplete(tmp_path, credential):
    install_context(slack_context(tmp_path, credential=credential))
    with pytest.raises(SlackConfigurationError, match="^Slack credentials are incomplete$"):
        await api_call("users.list", {"limit": 200})


async def test_unreadable_credential_is_unavailable(tmp_path):
    ctx = slack_context(tmp_path)

    def broken():
        raise ValueError("bad store")

    object.__setattr__(ctx.credentials, "_loader", broken)
    install_context(ctx)
    with pytest.raises(
        SlackConfigurationError, match="^Slack credentials are unavailable or invalid$"
    ):
        await api_call("users.list", {"limit": 200})


async def test_token_comes_from_context_credential(tmp_path):
    import httpx

    install_context(slack_context(tmp_path, credential=fake_credential(" tok ")))
    transport = AsyncMock(return_value=httpx.Response(200, json={"ok": True, "members": []}))
    with patch("httpx.AsyncClient.get", transport):
        await api_call("users.list", {"limit": 200})
    assert transport.await_args.kwargs["headers"]["Authorization"] == "Bearer tok"


async def test_no_context_fails_closed():
    install_context(None)
    with pytest.raises(PluginContextError):
        await api_call("users.list", {"limit": 200})

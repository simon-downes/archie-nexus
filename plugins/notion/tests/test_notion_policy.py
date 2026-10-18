"""Notion policy gates and credentials read the plugin context, messages unchanged (070 M10)."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from archie_agent.exec.plugin import PluginContextError, install_context
from archie_plugins.notion.client import (
    NotionConfigurationError,
    NotionTransportError,
    _session,
)
from archie_plugins.notion.policy import (
    NotionPolicyError,
    NotionValidationError,
    NotionWritePolicyError,
    require_read,
    require_write,
)
from archie_plugins.notion.search import search
from archie_shared.credentials.models import OAuthCredential

PAGE = "11111111-1111-1111-1111-111111111111"
DB = "33333333-3333-3333-3333-333333333333"


def test_default_policy_allows_reads_and_blocks_writes():
    assert require_read() == {"enabled": True, "scope": None}
    with pytest.raises(NotionWritePolicyError, match="^Notion writes are disabled by policy$"):
        require_write()


def test_read_disabled(policy):
    policy({"notion": {"read": {"enabled": False}}})
    with pytest.raises(NotionPolicyError, match="^Notion reads are disabled by policy$"):
        require_read()


def test_write_policy_error_is_a_policy_error():
    assert issubclass(NotionWritePolicyError, NotionPolicyError)


def test_scopes_are_parsed_from_the_context(policy):
    policy(
        {
            "notion": {
                "read": {"scope": {"pages": [PAGE]}},
                "write": {"enabled": True, "scope": {"databases": [DB]}},
            }
        }
    )
    assert require_read()["scope"] == {"pages": {PAGE.replace("-", "")}, "databases": set()}
    assert require_write()["scope"] == {"pages": set(), "databases": {DB.replace("-", "")}}


@pytest.mark.parametrize("scope", [None, {}, {"pages": [], "databases": []}])
def test_empty_scope_is_unrestricted(policy, scope):
    policy({"notion": {"read": {"enabled": True, "scope": scope}}})
    assert require_read()["scope"] is None


def test_invalid_scope_in_either_block_fails_closed(policy):
    policy({"notion": {"write": {"enabled": False, "scope": {"pages": ["nope"]}}}})
    with pytest.raises(NotionValidationError, match="^Invalid Notion resource ID$"):
        require_read()
    policy({"notion": {"read": {"scope": {"unknown": []}}}})
    with pytest.raises(NotionValidationError, match="^Invalid Notion scope$"):
        require_read()


async def test_disabled_read_makes_no_request(policy):
    policy({"notion": {"read": {"enabled": False}}})
    with patch("archie_plugins.notion.client.notion_call", new=AsyncMock()) as call:
        with pytest.raises(NotionPolicyError):
            await search("notes")
    call.assert_not_awaited()


def test_gates_without_context_fail_closed():
    install_context(None)
    with pytest.raises(PluginContextError):
        require_read()
    with pytest.raises(PluginContextError):
        asyncio.run(_session().__aenter__())


@pytest.mark.parametrize(
    "credential", [None, OAuthCredential(), OAuthCredential(access_token="  ")]
)
def test_incomplete_credential_makes_no_connection(policy, credential):
    policy({}, credential=credential)
    with patch("importlib.import_module") as importer:
        with pytest.raises(NotionConfigurationError, match="^Notion credentials are incomplete$"):
            asyncio.run(_session().__aenter__())
    importer.assert_not_called()


def test_session_sends_the_context_access_token(policy):
    policy({}, credential=OAuthCredential(access_token=" tok "))
    seen = {}

    class StopError(Exception):
        pass

    def transport(url, headers, **kwargs):
        seen.update(url=url, headers=headers)
        raise StopError

    module = type("M", (), {"streamablehttp_client": staticmethod(transport)})
    session_module = type("S", (), {"ClientSession": object})
    modules = {"mcp.client.streamable_http": module, "mcp.client.session": session_module}
    with patch("importlib.import_module", side_effect=modules.__getitem__):
        with pytest.raises(NotionTransportError, match="could not be completed"):
            asyncio.run(_session().__aenter__())
    assert seen == {"url": "https://mcp.notion.com/mcp", "headers": {"Authorization": "Bearer tok"}}

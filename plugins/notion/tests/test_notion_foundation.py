"""Foundation tests for Notion registration, policy, and search."""

from unittest.mock import AsyncMock, patch

import pytest
from archie_agent.exec.plugin import plugin_tools
from archie_plugins.notion.policy import (
    NotionValidationError,
    in_scope,
    normalize_id,
    policies,
)
from archie_plugins.notion.search import search

PAGE = "11111111-1111-1111-1111-111111111111"
CHILD = "22222222-2222-2222-2222-222222222222"
DB = "33333333-3333-3333-3333-333333333333"


def test_notion_tools_are_namespaced_plugin_tools():
    tools = plugin_tools()
    assert "notion.search" in tools
    assert tools["notion.search"]._namespace == "notion"
    assert tools["notion.search"]._plugin == "notion"


def test_notion_policy_defaults_and_root_scope(policy):
    read, write = policies()
    assert read == {"enabled": True, "scope": None}
    assert write == {"enabled": False, "scope": None}
    scoped = {"pages": [PAGE]}
    policy({"notion": {"read": {"scope": scoped}}})
    read, _ = policies()
    assert in_scope(PAGE, "page", [], read["scope"])
    assert in_scope(CHILD, "page", [PAGE], read["scope"])
    assert not in_scope(DB, "database", [CHILD], read["scope"])


def test_notion_scope_rejects_unknown_fields_and_bad_ids(policy):
    policy({"notion": {"read": {"scope": {"unknown": []}}}})
    with pytest.raises(NotionValidationError):
        policies()
    with pytest.raises(NotionValidationError):
        normalize_id("not-a-notion-id")


async def test_search_filters_scoped_results_and_deduplicates():
    payload = {
        "results": [
            {"id": PAGE, "type": "page", "title": "allowed", "ancestors": []},
            {"id": PAGE, "type": "page", "title": "duplicate", "ancestors": []},
            {"id": CHILD, "type": "page", "title": "child", "ancestors": [PAGE]},
            {"id": DB, "type": "database", "title": "blocked", "ancestors": []},
        ]
    }
    scope = {"pages": {PAGE.replace("-", "")}, "databases": set()}
    with (
        patch(
            "archie_plugins.notion.search.require_read",
            return_value={"enabled": True, "scope": scope},
        ),
        patch("archie_plugins.notion.client.notion_call", new=AsyncMock(return_value=payload)),
    ):
        result = await search("notes", limit=10)
    assert [item["title"] for item in result] == ["allowed", "child"]


def test_notion_credential_error_is_sanitized():
    import asyncio

    from archie_agent.exec.plugin import CredentialSource, context
    from archie_plugins.notion.client import NotionConfigurationError, _session

    with patch.object(CredentialSource, "get", side_effect=RuntimeError("secret path")) as get:
        with pytest.raises(NotionConfigurationError, match="unavailable"):
            asyncio.run(_session().__aenter__())
    get.assert_called_once()
    assert context().plugin == "notion"

"""Foundation tests for Notion registration, policy, and search."""

from unittest.mock import AsyncMock, patch

import pytest
from archie_agent.exec.tools import get_all_tools
from archie_agent.exec.tools.notion.policy import (
    NotionValidationError,
    in_scope,
    normalize_id,
    policies,
)
from archie_agent.exec.tools.notion.search import search

PAGE = "11111111-1111-1111-1111-111111111111"
CHILD = "22222222-2222-2222-2222-222222222222"
DB = "33333333-3333-3333-3333-333333333333"


def test_notion_tools_are_namespaced_and_exec_only():
    tools = get_all_tools()
    assert "notion.search" in tools
    assert tools["notion.search"]._namespace == "notion"
    assert tools["notion.search"]._native is False
    assert tools["notion.search"]._exec_docs is False


def test_notion_policy_defaults_and_root_scope():
    read, write = policies({})
    assert read == {"enabled": True, "scope": None}
    assert write == {"enabled": False, "scope": None}
    scoped = {"pages": [PAGE]}
    read, _ = policies({"notion": {"read": {"scope": scoped}}})
    assert in_scope(PAGE, "page", [], read["scope"])
    assert in_scope(CHILD, "page", [PAGE], read["scope"])
    assert not in_scope(DB, "database", [CHILD], read["scope"])


def test_notion_scope_rejects_unknown_fields_and_bad_ids():
    with pytest.raises(NotionValidationError):
        policies({"notion": {"read": {"scope": {"unknown": []}}}})
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
            "archie_agent.exec.tools.notion.search.require_read",
            return_value={"enabled": True, "scope": scope},
        ),
        patch(
            "archie_agent.exec.tools.notion.client.notion_call", new=AsyncMock(return_value=payload)
        ),
    ):
        result = await search("notes", limit=10)
    assert [item["title"] for item in result] == ["allowed", "child"]


def test_notion_credential_error_is_sanitized():
    import asyncio

    from archie_agent.exec.tools.notion.client import NotionConfigurationError, _session

    with patch(
        "archie_agent.exec.tools.notion.client.get_credential",
        side_effect=RuntimeError("secret path"),
    ):
        with pytest.raises(NotionConfigurationError, match="unavailable"):
            asyncio.run(_session().__aenter__())

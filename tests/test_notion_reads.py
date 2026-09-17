"""Tests for Notion page/database read projections and scope."""

from unittest.mock import AsyncMock, patch

import pytest
from archie_agent.exec.tools.notion.policy import NotionPolicyError
from archie_agent.exec.tools.notion.reads import get_database, get_page, resource_id

ROOT = "11111111-1111-1111-1111-111111111111"
CHILD = "22222222-2222-2222-2222-222222222222"


def test_resource_id_accepts_supported_urls():
    assert resource_id(f"https://www.notion.so/Page-{ROOT}") == ROOT.replace("-", "")
    assert resource_id(f"https://team.notion.site/Page-{ROOT}?v=1") == ROOT.replace("-", "")


async def test_get_page_returns_bounded_projection_and_properties():
    raw = {
        "id": ROOT,
        "type": "page",
        "title": "Page",
        "content": "body",
        "properties": {"Status": "Open"},
    }
    with patch("archie_agent.exec.tools.notion.reads.notion_call", new=AsyncMock(return_value=raw)):
        result = await get_page(ROOT, include_properties=True)
    assert result == {
        "id": ROOT,
        "type": "page",
        "title": "Page",
        "content": "body",
        "properties": {"Status": "Open"},
    }


async def test_get_page_denies_unrelated_resource():
    raw = {"id": CHILD, "type": "page", "title": "Hidden", "ancestors": []}
    with (
        patch(
            "archie_agent.exec.tools.notion.reads.require_read",
            return_value={
                "enabled": True,
                "scope": {"pages": {ROOT.replace("-", "")}, "databases": set()},
            },
        ),
        patch("archie_agent.exec.tools.notion.reads.notion_call", new=AsyncMock(return_value=raw)),
    ):
        with pytest.raises(NotionPolicyError):
            await get_page(CHILD)


async def test_get_database_returns_views():
    raw = {"id": ROOT, "type": "database", "title": "Tasks", "views": [{"name": "All"}]}
    with patch("archie_agent.exec.tools.notion.reads.notion_call", new=AsyncMock(return_value=raw)):
        result = await get_database(ROOT)
    assert result["views"] == [{"name": "All"}]

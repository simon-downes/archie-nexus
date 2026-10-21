"""Tests for Notion page/database read projections and scope."""

from unittest.mock import AsyncMock, patch

import pytest
from archie_plugins.notion.policy import NotionPolicyError
from archie_plugins.notion.reads import get_database, get_page, resource_id

ROOT = "11111111-1111-1111-1111-111111111111"
CHILD = "22222222-2222-2222-2222-222222222222"


def test_resource_id_accepts_supported_urls():
    assert resource_id(f"https://www.notion.so/Page-{ROOT}") == ROOT.replace("-", "")
    assert resource_id(f"https://team.notion.site/Page-{ROOT}?v=1") == ROOT.replace("-", "")
    assert resource_id(f"https://app.notion.com/p/{ROOT}") == ROOT.replace("-", "")


async def test_get_page_returns_bounded_projection_and_properties():
    raw = {
        "id": ROOT,
        "type": "page",
        "title": "Page",
        "content": "body",
        "properties": {"Status": "Open"},
    }
    with patch("archie_plugins.notion.reads.notion_call", new=AsyncMock(return_value=raw)):
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
            "archie_plugins.notion.reads.require_read",
            return_value={
                "enabled": True,
                "scope": {"pages": {ROOT.replace("-", "")}, "databases": set()},
            },
        ),
        patch("archie_plugins.notion.reads.notion_call", new=AsyncMock(return_value=raw)),
    ):
        with pytest.raises(NotionPolicyError):
            await get_page(CHILD)


def test_normalize_fetch_preserves_data_source_and_view_urls():
    from archie_plugins.notion.normalization import normalize_fetch

    result = normalize_fetch(
        {
            "metadata": {"type": "database"},
            "text": '<data-source url="collection://source-1"><view url="view://view-1">',
        },
        ROOT,
    )
    assert result["data_sources"] == ["collection://source-1"]
    assert result["views"] == [{"url": "view://view-1"}]


async def test_get_database_returns_views():
    raw = {"id": ROOT, "type": "database", "title": "Tasks", "views": [{"name": "All"}]}
    with patch("archie_plugins.notion.reads.notion_call", new=AsyncMock(return_value=raw)):
        result = await get_database(ROOT)
    assert result["views"] == [{"name": "All"}]


async def test_get_database_returns_data_source_properties():
    properties = {"Tag": {"type": "select", "options": ["Now"]}}
    raw = {
        "id": ROOT,
        "type": "database",
        "data_sources": ["collection://source-1", "collection://source-2"],
        "schemas": {"collection://source-1": {"name": "Tasks", "properties": properties}},
    }
    with patch("archie_plugins.notion.reads.notion_call", new=AsyncMock(return_value=raw)):
        result = await get_database(ROOT)
    assert result["data_sources"] == [
        {"url": "collection://source-1", "name": "Tasks", "properties": properties},
        {"url": "collection://source-2", "properties": None},
    ]

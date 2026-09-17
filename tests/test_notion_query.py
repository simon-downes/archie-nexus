"""Tests for Notion database queries and comments."""

from unittest.mock import AsyncMock, patch

import pytest
from archie_agent.exec.tools.notion.policy import NotionPolicyError
from archie_agent.exec.tools.notion.query import get_comments, query_database

ROOT = "11111111-1111-1111-1111-111111111111"


async def test_query_authorizes_database_before_rows():
    schema = {"id": ROOT, "type": "database", "ancestors": []}
    rows = {"results": [{"id": "row-1", "Status": "Done"}]}
    call = AsyncMock(side_effect=[schema, rows])
    with patch("archie_agent.exec.tools.notion.query.notion_call", call):
        result = await query_database(ROOT, limit=1)
    assert result == rows["results"]
    assert call.await_args_list[0].args[0] == "notion-fetch"
    assert call.await_args_list[1].args[0] == "notion-query-data-sources"


async def test_query_denies_database_before_row_request():
    schema = {"id": ROOT, "type": "database", "ancestors": []}
    call = AsyncMock(return_value=schema)
    with (
        patch("archie_agent.exec.tools.notion.query.notion_call", call),
        patch(
            "archie_agent.exec.tools.notion.query.require_read",
            return_value={
                "enabled": True,
                "scope": {"pages": set(), "databases": {"22222222222222222222222222222222"}},
            },
        ),
    ):
        with pytest.raises(NotionPolicyError):
            await query_database(ROOT)
    assert call.await_count == 1


async def test_get_comments_authorizes_page_before_comments():
    page = {"id": ROOT, "type": "page", "ancestors": []}
    comments = {"results": [{"id": "comment-1", "text": "hello"}]}
    call = AsyncMock(side_effect=[page, comments])
    with patch("archie_agent.exec.tools.notion.query.notion_call", call):
        result = await get_comments(ROOT, limit=1)
    assert result == comments["results"]
    assert call.await_args_list[1].args[0] == "notion-get-comments"

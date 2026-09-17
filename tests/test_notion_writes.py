"""Tests for Notion mutation gates and bounded calls."""

from unittest.mock import AsyncMock, patch

import pytest
from archie_agent.exec.tools.notion.policy import NotionWritePolicyError
from archie_agent.exec.tools.notion.writes import add_comment, create_page, update_page

ROOT = "11111111-1111-1111-1111-111111111111"


async def test_create_page_requires_write_and_calls_provider():
    with (
        patch(
            "archie_agent.exec.tools.notion.writes.require_write",
            return_value={
                "enabled": True,
                "scope": {"pages": {ROOT.replace("-", "")}, "databases": set()},
            },
        ),
        patch(
            "archie_agent.exec.tools.notion.writes.notion_call",
            new=AsyncMock(return_value={"id": ROOT, "type": "page"}),
        ) as call,
    ):
        result = await create_page(ROOT, title="New", content="Body")
    assert result["id"] == ROOT
    assert call.await_args.args[0] == "notion-create-pages"


async def test_create_page_denied_before_mutation():
    with (
        patch(
            "archie_agent.exec.tools.notion.writes.require_write",
            side_effect=NotionWritePolicyError("disabled"),
        ),
        patch("archie_agent.exec.tools.notion.writes.notion_call", new=AsyncMock()) as call,
    ):
        with pytest.raises(NotionWritePolicyError):
            await create_page(ROOT, title="New")
    call.assert_not_awaited()


async def test_update_requires_a_change():
    with patch(
        "archie_agent.exec.tools.notion.writes.require_write",
        return_value={"enabled": True, "scope": None},
    ):
        with pytest.raises(Exception, match="requires"):
            await update_page(ROOT)


async def test_comment_payload_is_bounded_and_explicit():
    with (
        patch(
            "archie_agent.exec.tools.notion.writes.require_write",
            return_value={"enabled": True, "scope": None},
        ),
        patch(
            "archie_agent.exec.tools.notion.writes.notion_call",
            new=AsyncMock(return_value={"id": "comment"}),
        ) as call,
    ):
        await add_comment(ROOT, "hello")
    assert call.await_args.args[0] == "notion-create-comment"
    assert call.await_args.args[1]["rich_text"][0]["text"]["content"] == "hello"

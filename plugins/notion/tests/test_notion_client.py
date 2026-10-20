"""Tests for Notion MCP result classification."""

from types import SimpleNamespace

import pytest
from archie_plugins.notion.client import NotionResponseError, NotionToolError, _call


@pytest.mark.asyncio
async def test_call_surfaces_mcp_tool_errors_without_json_decoding():
    async def call_tool(name, arguments):
        return _result(is_error=True, text="data: Invalid input")

    client = SimpleNamespace(call_tool=call_tool)
    with pytest.raises(NotionToolError, match="data: Invalid input"):
        await _call(client, "notion-query-data-sources", {})


@pytest.mark.asyncio
async def test_call_rejects_successful_non_json_response():
    async def call_tool(name, arguments):
        return _result(is_error=False, text="not JSON")

    client = SimpleNamespace(call_tool=call_tool)
    with pytest.raises(NotionResponseError, match="non-JSON"):
        await _call(client, "notion-fetch", {})


def _result(*, is_error: bool, text: str):
    return SimpleNamespace(isError=is_error, content=[SimpleNamespace(text=text)])

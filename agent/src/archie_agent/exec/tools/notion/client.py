"""Async Notion MCP adapter."""

from __future__ import annotations

import asyncio
import importlib
import json
from contextlib import asynccontextmanager
from typing import Any

from archie_shared.credentials.models import OAuthCredential
from archie_shared.credentials.store import get_credential

from archie_agent.exec.tools import ToolError

NOTION_MCP_URL = "https://mcp.notion.com/mcp"


class NotionConfigurationError(ToolError):
    pass


class NotionAuthenticationError(ToolError):
    pass


class NotionTransportError(ToolError):
    pass


@asynccontextmanager
async def _session():
    try:
        credential = get_credential("notion")
    except Exception as exc:
        raise NotionConfigurationError("Notion credentials are unavailable or invalid") from exc
    if (
        not isinstance(credential, OAuthCredential)
        or not credential.access_token
        or not credential.access_token.strip()
    ):
        raise NotionConfigurationError("Notion credentials are incomplete")
    try:
        transport_mod = importlib.import_module("mcp.client.streamable_http")
        session_type = importlib.import_module("mcp.client.session").ClientSession
        transport = transport_mod.streamablehttp_client(
            NOTION_MCP_URL,
            {"Authorization": f"Bearer {credential.access_token.strip()}"},
            timeout=30,
            sse_read_timeout=30,
        )
        async with transport as (read_stream, write_stream, _):
            async with session_type(read_stream, write_stream) as client:
                await client.initialize()
                yield client
    except NotionConfigurationError:
        raise
    except Exception as exc:
        raise NotionTransportError("Notion MCP request could not be completed") from exc


async def _call(client: Any, name: str, arguments: dict[str, Any]) -> Any:
    try:
        result = await client.call_tool(name, arguments)
        text = "\n".join(getattr(block, "text", "") for block in getattr(result, "content", []))
        return json.loads(text) if text else {}
    except Exception as exc:
        raise NotionTransportError("Notion MCP request failed") from exc


async def notion_call(name: str, arguments: dict[str, Any]) -> Any:
    async with asyncio.timeout(60):
        async with _session() as client:
            return await _call(client, name, arguments)

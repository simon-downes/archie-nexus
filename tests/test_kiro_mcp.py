"""ArchieMcpServer over real streamable HTTP MCP (077)."""

from __future__ import annotations

import asyncio
import io
import os
import time

import pytest
from archie_agent.exec.tool import create_registry
from archie_agent.kiro.mcp_server import KIRO_RESULT_CHARS, ArchieMcpServer
from archie_agent.tools import ToolSpec
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from PIL import Image


async def _context() -> dict:
    return {"policy": {}, "settings": {}, "enabled": []}


@pytest.fixture
async def server():
    registry = create_registry()

    async def big(**_):
        return "x" * 40_000

    registry.register(
        ToolSpec(
            name="big",
            description="big",
            schema={"type": "object", "properties": {}},
            handler=big,
        )
    )
    srv = ArchieMcpServer(registry, _context)
    await srv.start()
    yield srv
    await srv.stop()


async def _call(url: str, name: str, args: dict):
    async with streamable_http_client(url) as (read, write, _), ClientSession(read, write) as s:
        await s.initialize()
        return await s.call_tool(name, args)


async def test_lists_registry_tools_without_skill_or_task(server):
    async with (
        streamable_http_client(server.url) as (read, write, _),
        ClientSession(read, write) as s,
    ):
        await s.initialize()
        tools = (await s.list_tools()).tools
    names = {t.name for t in tools}
    expected = {t["name"] for t in create_registry().to_tool_config()} | {"big"}
    assert names == expected - {"skill", "task"}
    shell = next(t for t in tools if t.name == "shell")
    assert "command" in shell.inputSchema["properties"]


async def test_read_png_returns_image_content(server, tmp_path):
    path = tmp_path / "x.png"
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "green").save(buf, "PNG")
    path.write_bytes(buf.getvalue())
    result = await _call(server.url, "read", {"path": str(path)})
    kinds = [c.type for c in result.content]
    assert "image" in kinds
    image = next(c for c in result.content if c.type == "image")
    assert image.mimeType == "image/png"


async def test_shell_nonzero_exit_is_error(server):
    result = await _call(server.url, "shell", {"command": "exit 3"})
    assert result.isError is True
    assert result.content[0].text.startswith("[exit: 3]")


async def test_shell_success_is_not_error(server):
    result = await _call(server.url, "shell", {"command": "echo hi"})
    assert result.isError is False
    assert "hi" in result.content[0].text


async def test_large_result_is_capped(server):
    result = await _call(server.url, "big", {})
    text = result.content[0].text
    assert text.startswith("x" * KIRO_RESULT_CHARS)
    assert len(text) < KIRO_RESULT_CHARS + 200
    assert "truncated: showed 29,000 of 40,000" in text


async def test_cancel_all_kills_running_shell(server, tmp_path):
    pid_file = tmp_path / "pid"
    call = asyncio.create_task(
        _call(server.url, "shell", {"command": f"echo $$ > {pid_file}; sleep 30"})
    )
    deadline = time.monotonic() + 10
    while not pid_file.exists() or not pid_file.read_text().strip():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.05)
    pid = int(pid_file.read_text())
    server.cancel_all()
    result = await asyncio.wait_for(call, 5)
    assert result.isError is True
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_stop_closes_port():
    srv = ArchieMcpServer(create_registry(), _context)
    url = await srv.start()
    port = int(url.rsplit(":", 1)[1].split("/")[0])
    await srv.stop()
    with pytest.raises(OSError):
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()

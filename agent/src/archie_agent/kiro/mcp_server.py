"""In-process MCP server that runs Archie's tools for Kiro (077 D7).

Kiro reaches it on ``127.0.0.1`` through the generated agent files. The server only
executes tools; canonical tool events come from the ACP stream (D9).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import logging
import signal
import socket
from collections.abc import Awaitable, Callable
from typing import Any

import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from archie_agent.exec.tool import format_result, run_exec, truncate_result
from archie_agent.exec.tools._subprocess import kill_process_group
from archie_agent.exec.tools.images import ToolImage
from archie_agent.tools import ToolRegistry

log = logging.getLogger(__name__)

# Kiro replaces results over 30,000 characters with a short preview (spike).
KIRO_RESULT_CHARS = 29_000
# Tools the Kiro backend replaces with its own skill and subagent support.
_EXCLUDED = frozenset({"skill", "task"})


def _shell_is_error(content: str) -> bool:
    lines = content.splitlines()
    first = lines[0] if lines else ""
    if first.startswith("[error:"):
        return True
    if not first.startswith("[exit:"):
        return False
    try:
        return int(first.removeprefix("[exit:").removesuffix("]").strip()) != 0
    except ValueError:
        return False


class ArchieMcpServer:
    """Serve a tool registry over streamable HTTP MCP on loopback."""

    def __init__(
        self,
        registry: ToolRegistry,
        runner_context: Callable[[], Awaitable[dict]],
        *,
        exec_kwargs: dict[str, Any] | None = None,
        host: str = "127.0.0.1",
    ) -> None:
        self._registry = registry
        self._runner_context = runner_context
        self._exec_kwargs = exec_kwargs or {}
        self._host = host
        self._port: int | None = None
        self._procs: set[asyncio.subprocess.Process] = set()
        # Set by cancel_all until reset(): processes started meanwhile die at once, so a
        # call that reaches the server just after an interrupt can't outlive the turn.
        self._cancelling = False
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self._mcp = Server("archie")
        self._register()

    # -- MCP handlers ----------------------------------------------------------

    def tool_names(self) -> list[str]:
        return [t["name"] for t in self._registry.to_tool_config() if t["name"] not in _EXCLUDED]

    def _register(self) -> None:
        @self._mcp.list_tools()
        async def list_tools() -> list[types.Tool]:
            return [
                types.Tool(
                    name=t["name"], description=t["description"], inputSchema=t["input_schema"]
                )
                for t in self._registry.to_tool_config()
                if t["name"] not in _EXCLUDED
            ]

        @self._mcp.call_tool()
        async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
            return await self.call(name, arguments)

    async def call(self, name: str, arguments: dict) -> types.CallToolResult:
        """Run one tool and shape its result for MCP."""
        spec = self._registry.get(name)
        if spec is None or name in _EXCLUDED:
            return _text_result(f"Unknown tool: {name}", True)
        procs: list[asyncio.subprocess.Process] = []

        def on_start(proc: asyncio.subprocess.Process) -> None:
            procs.append(proc)
            self._procs.add(proc)
            if self._cancelling:
                _terminate(proc)

        image = None
        try:
            if name == "exec":
                envelope = await run_exec(
                    arguments.get("source", ""),
                    on_start=on_start,
                    runner_context=await self._runner_context(),
                    **self._exec_kwargs,
                )
                content, is_error = format_result(envelope), not envelope.ok
            else:
                kwargs = dict(arguments)
                if "on_start" in inspect.signature(spec.handler).parameters:
                    kwargs["on_start"] = on_start
                result = await spec.handler(**kwargs)
                if isinstance(result, ToolImage):
                    image = result.image
                    content = result.caption
                else:
                    content = str(result)
                is_error = name == "shell" and _shell_is_error(content)
        except Exception as exc:  # noqa: BLE001 — tool failures go back to the model
            content, is_error = f"{type(exc).__name__}: {exc}", True
        finally:
            for proc in procs:
                self._procs.discard(proc)
        blocks: list[types.ContentBlock] = [
            types.TextContent(type="text", text=truncate_result(content, KIRO_RESULT_CHARS))
        ]
        if image is not None:
            blocks.append(
                types.ImageContent(
                    type="image",
                    data=base64.b64encode(image.data).decode("ascii"),
                    mimeType=f"image/{image.format}",
                )
            )
        return types.CallToolResult(content=blocks, isError=is_error)

    # -- lifecycle -------------------------------------------------------------

    @property
    def url(self) -> str:
        if self._port is None:
            raise RuntimeError("MCP server not started")
        return f"http://{self._host}:{self._port}/mcp"

    async def start(self) -> str:
        """Bind a free loopback port, serve, and return the MCP URL."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._host, 0))
        self._port = sock.getsockname()[1]
        manager = StreamableHTTPSessionManager(
            app=self._mcp,
            stateless=False,
            security_settings=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[f"{self._host}:{self._port}", f"localhost:{self._port}"],
                allowed_origins=[],
            ),
        )

        async def app(scope, receive, send):
            if scope["type"] == "lifespan":
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                message = await receive()
                if message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                return
            if scope["type"] == "http" and scope["path"].rstrip("/") == "/mcp":
                await manager.handle_request(scope, receive, send)
                return
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        config = uvicorn.Config(app, log_level="warning", lifespan="on", access_log=False)
        self._server = uvicorn.Server(config)
        started = asyncio.Event()

        async def serve() -> None:
            async with manager.run():
                server_task = asyncio.create_task(self._server.serve(sockets=[sock]))
                while not self._server.started and not server_task.done():
                    await asyncio.sleep(0.01)
                started.set()
                await server_task

        self._task = asyncio.create_task(serve())
        waiter = asyncio.create_task(started.wait())
        await asyncio.wait({waiter, self._task}, return_when=asyncio.FIRST_COMPLETED)
        if self._task.done():
            waiter.cancel()
            self._task.result()
            raise RuntimeError("MCP server stopped during startup")
        return self.url

    def cancel_all(self) -> None:
        """Kill every running tool process group, and any started before ``reset()``."""
        self._cancelling = True
        for proc in list(self._procs):
            _terminate(proc)

    def reset(self) -> None:
        """Allow tool processes again (start of the next turn)."""
        self._cancelling = False

    async def stop(self) -> None:
        self.cancel_all()
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, 5)
            except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                self._task.cancel()
        self._task = None


def _terminate(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM the process group, then SIGKILL it after 2 s."""
    if proc.returncode is None:
        kill_process_group(proc, signal.SIGTERM)
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().call_later(2.0, _kill, proc)


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        kill_process_group(proc, signal.SIGKILL)


def _text_result(text: str, is_error: bool) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], isError=is_error
    )

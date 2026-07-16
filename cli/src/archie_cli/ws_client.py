"""Async WebSocket client wrapper for communicating with the agent.

Connects to the agent's /stream endpoint and provides typed send/receive
over the wire protocol defined in archie_shared.events.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

import websockets
from archie_shared.events import (
    ClientCommand,
    InterruptCommand,
    MessageCommand,
    ServerEvent,
    deserialize_event,
    serialize_command,
)
from websockets import ClientConnection

log = logging.getLogger(__name__)


class WSClient:
    """WebSocket client for the archie agent stream endpoint."""

    def __init__(self) -> None:
        self._ws: ClientConnection | None = None

    @property
    def connected(self) -> bool:
        """True if a connection object is currently held (may still be closing)."""
        return self._ws is not None

    async def connect(self, url: str) -> WSClient:
        """Connect to the agent WebSocket endpoint.

        Enables keepalive pings so a dropped connection is detected promptly
        rather than silently going stale during a long turn.

        Raises websockets.exceptions.WebSocketException on failure.
        """
        self._ws = await websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        )
        return self

    async def disconnect(self) -> None:
        """Close the WebSocket connection cleanly."""
        if self._ws is not None:
            ws, self._ws = self._ws, None
            await ws.close()

    async def send_message(self, content: str) -> None:
        """Send a MessageCommand to the agent."""
        if self._ws is None:
            raise RuntimeError("Not connected")
        cmd = MessageCommand(content=content)
        await self._ws.send(serialize_command(cmd))

    async def send_interrupt(self) -> None:
        """Send an InterruptCommand to the agent."""
        if self._ws is None:
            raise RuntimeError("Not connected")
        cmd = InterruptCommand()
        await self._ws.send(serialize_command(cmd))

    async def send_command(self, command: ClientCommand) -> None:
        """Send any ClientCommand to the agent."""
        if self._ws is None:
            raise RuntimeError("Not connected")
        await self._ws.send(serialize_command(command))

    async def receive(self) -> AsyncGenerator[ServerEvent]:
        """Async generator yielding deserialized ServerEvent objects.

        Yields events until the connection is closed. On an unexpected close
        (keepalive timeout, network drop, server crash), re-raises
        ``ConnectionClosed`` so the caller can surface it and reconnect. The
        connection reference is cleared first so ``connected`` reflects reality.
        """
        if self._ws is None:
            raise RuntimeError("Not connected")
        try:
            async for raw in self._ws:
                try:
                    event = deserialize_event(raw)
                    yield event
                except (ValueError, KeyError) as e:
                    log.warning("Malformed event from server: %s", e)
        except websockets.exceptions.ConnectionClosedOK:
            # Clean close (e.g. we called disconnect) — terminate quietly.
            self._ws = None
        except websockets.exceptions.ConnectionClosed:
            # Unexpected close — surface it so the app can reconnect.
            self._ws = None
            raise

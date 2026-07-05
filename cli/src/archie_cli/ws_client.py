"""Async WebSocket client wrapper for communicating with the agent.

Connects to the agent's /stream endpoint and provides typed send/receive
over the wire protocol defined in archie_shared.events.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

import websockets
from archie_shared.events import (
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

    async def connect(self, url: str) -> WSClient:
        """Connect to the agent WebSocket endpoint.

        Raises websockets.exceptions.WebSocketException on failure.
        """
        self._ws = await websockets.connect(url)
        return self

    async def disconnect(self) -> None:
        """Close the WebSocket connection cleanly."""
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

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

    async def receive(self) -> AsyncGenerator[ServerEvent]:
        """Async generator yielding deserialized ServerEvent objects.

        Yields events until the connection is closed.
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
        except websockets.exceptions.ConnectionClosed:
            log.debug("WebSocket connection closed")

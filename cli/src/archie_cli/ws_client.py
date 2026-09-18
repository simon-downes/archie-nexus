"""Async WebSocket client for the canonical session event stream."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

import websockets
from archie_shared.commands import ClientCommand, InterruptCommand, MessageCommand, encode_command
from archie_shared.events import SessionEvent, decode_event
from websockets import ClientConnection
from websockets.protocol import State

log = logging.getLogger(__name__)


class WSClient:
    """WebSocket client for the archie agent stream endpoint."""

    def __init__(self) -> None:
        self._ws: ClientConnection | None = None

    @property
    def connected(self) -> bool:
        """True if a live connection object is currently held."""
        if self._ws is None:
            return False
        state = getattr(self._ws, "state", None)
        return state is None or state is State.OPEN

    async def connect(self, url: str) -> WSClient:
        """Connect to the agent WebSocket endpoint with keepalive pings."""
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
        await self.send_command(MessageCommand(content=content))

    async def send_interrupt(self) -> None:
        """Send an InterruptCommand to the agent."""
        await self.send_command(InterruptCommand())

    async def send_command(self, command: ClientCommand) -> None:
        """Send any client command."""
        if self._ws is None:
            raise RuntimeError("Not connected")
        await self._ws.send(encode_command(command))

    async def receive(self) -> AsyncGenerator[SessionEvent]:
        """Yield every server frame through the one canonical decoder."""
        if self._ws is None:
            raise RuntimeError("Not connected")
        try:
            async for raw in self._ws:
                try:
                    payload = raw if isinstance(raw, str) else raw.decode()
                    yield decode_event(payload)
                except (ValueError, KeyError, TypeError) as e:
                    log.warning("Malformed event from server: %s", e)
        except websockets.exceptions.ConnectionClosedOK:
            self._ws = None
        except websockets.exceptions.ConnectionClosed:
            self._ws = None
            raise

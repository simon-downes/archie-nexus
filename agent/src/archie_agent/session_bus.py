"""Ordered session event coordination and bounded client delivery."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from archie_shared.events import Event, encode_event
from archie_shared.session.log import SessionLog

log = logging.getLogger(__name__)


class _ClientRegistry:
    """Set-like compatibility view that registers clients with its owning bus."""

    def __init__(self, bus: SessionEventBus) -> None:
        self._bus = bus

    def add(self, websocket: Any) -> None:
        self._bus.add_client(websocket)

    def discard(self, websocket: Any) -> None:
        self._bus.discard_client(websocket)

    def __iter__(self):
        return iter(self._bus._clients)

    def __len__(self) -> int:
        return len(self._bus._clients)

    def __contains__(self, websocket: object) -> bool:
        return websocket in self._bus._clients


class SessionEventBus:
    """The ordered sink for persisted and live session events."""

    def __init__(self, session_log: SessionLog | Path, *, queue_size: int = 1024) -> None:
        self.log = session_log if isinstance(session_log, SessionLog) else SessionLog(session_log)
        self.path = self.log.path
        self.queue_size = queue_size
        self._clients: dict[Any, asyncio.Queue[str]] = {}
        self._senders: dict[Any, asyncio.Task[None]] = {}
        self._ordering = asyncio.Lock()
        self.clients = _ClientRegistry(self)

    async def emit(self, event: Event, target: Any | None = None) -> bool:
        """Append when declared persistent, then enqueue through one path."""
        if not isinstance(event, Event):
            raise TypeError(f"unsupported session event {type(event).__name__}")
        if target is not None and event.persist:
            raise TypeError("persisted events cannot be targeted")

        async with self._ordering:
            if event.persist:
                inserted = self.log.append(event)
                if not inserted:
                    return False
            line = encode_event(event)
            if target is None:
                self._enqueue_all(line)
            else:
                queue = self._clients.get(target)
                if queue is not None:
                    self._enqueue(target, queue, line)
        await asyncio.sleep(0)
        return True

    async def register_client(self, websocket: Any, initial_events: tuple[Event, ...]) -> None:
        """Atomically admit a client with validated live-only initial frames."""
        if any(event.persist for event in initial_events):
            raise TypeError("initial client events must be live-only")
        async with self._ordering:
            self._add_client(websocket)
            queue = self._clients[websocket]
            for event in initial_events:
                self._enqueue(websocket, queue, encode_event(event))
        await asyncio.sleep(0)

    def add_client(self, websocket: Any) -> None:
        """Register a client and start its independent sender task."""
        self._add_client(websocket)

    def _add_client(self, websocket: Any) -> None:
        if websocket in self._clients:
            return
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self.queue_size)
        self._clients[websocket] = queue
        self._senders[websocket] = asyncio.create_task(self._sender(websocket, queue))

    def discard_client(self, websocket: Any) -> None:
        """Remove a client and cancel its sender task."""
        self._clients.pop(websocket, None)
        task = self._senders.pop(websocket, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _enqueue_all(self, line: str) -> None:
        for websocket, queue in list(self._clients.items()):
            self._enqueue(websocket, queue, line)

    def _enqueue(self, websocket: Any, queue: asyncio.Queue[str], line: str) -> None:
        try:
            queue.put_nowait(line)
        except asyncio.QueueFull:
            log.warning("Disconnecting stalled session client after queue overflow")
            self.discard_client(websocket)
            close = getattr(websocket, "close", None)
            if close is not None:
                try:
                    asyncio.create_task(close(code=1013, reason="client send queue full"))
                except RuntimeError:
                    pass

    async def _sender(self, websocket: Any, queue: asyncio.Queue[str]) -> None:
        try:
            while True:
                line = await queue.get()
                await websocket.send_text(line)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.info("Session client disconnected while sending", exc_info=True)
        finally:
            if websocket in self._clients:
                self.discard_client(websocket)


__all__ = ["SessionEventBus"]

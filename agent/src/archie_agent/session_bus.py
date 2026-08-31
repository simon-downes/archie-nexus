"""Ordered session event coordination and bounded client delivery."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from archie_shared.canonical_events import PersistedEvent, PersistedEventTypes, encode_event
from archie_shared.session.log import append_serialized_event, read_event_lines

log = logging.getLogger(__name__)

_PERSISTED_TYPES = PersistedEventTypes


class LogAppendError(RuntimeError):
    """The session log could not be updated."""


class EventIdConflict(RuntimeError):  # noqa: N818
    """An event id was reused with different serialized content."""


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
    """The single ordered sink for persisted and live session events.

    Persistence and queue admission happen under one short ordering lock. Network
    writes happen in independent sender tasks, so a stalled client cannot delay
    appends or delivery to other clients.
    """

    def __init__(self, path: Path, *, queue_size: int = 1024) -> None:
        self.path = path
        self.queue_size = queue_size
        self._index: dict[str, str] = {item["id"]: item["line"] for item in read_event_lines(path)}
        self._clients: dict[Any, asyncio.Queue[str]] = {}
        self._senders: dict[Any, asyncio.Task[None]] = {}
        self._ordering = asyncio.Lock()
        self.clients = _ClientRegistry(self)

    @property
    def index(self) -> dict[str, str]:
        """Expose a read-only-by-convention view for diagnostics and tests."""
        return self._index

    def append(self, event: PersistedEvent) -> str:
        """Persist one event without broadcasting it."""
        self._require_persisted(event)
        line = encode_event(event)
        return self._append_line(event.id, line)[0]

    async def publish(self, event: PersistedEvent) -> str:
        """Persist, then enqueue one newly appended event to every client."""
        self._require_persisted(event)
        async with self._ordering:
            line, inserted = self._append_line(event.id, encode_event(event))
            if inserted:
                self._enqueue_all(line)
            await asyncio.sleep(0)
            return line

    async def broadcast(self, event: Any) -> None:
        """Enqueue a live-only canonical event without appending it."""
        if isinstance(event, _PERSISTED_TYPES):
            raise TypeError(f"persisted event {type(event).__name__} must use publish")
        async with self._ordering:
            self._enqueue_all(encode_event(event))
        await asyncio.sleep(0)

    async def send_to(self, websocket: Any, event: Any) -> None:
        """Enqueue a live-only event to one client in its existing order."""
        if isinstance(event, _PERSISTED_TYPES):
            raise TypeError(f"persisted event {type(event).__name__} must use publish")
        async with self._ordering:
            queue = self._clients.get(websocket)
            if queue is not None:
                self._enqueue(websocket, queue, encode_event(event))

    async def add_client_with_events(self, websocket: Any, events: tuple[Any, ...]) -> None:
        """Register a client and enqueue its initial frames atomically."""
        async with self._ordering:
            self.add_client(websocket)
            queue = self._clients[websocket]
            for event in events:
                if isinstance(event, _PERSISTED_TYPES):
                    raise TypeError(f"persisted event {type(event).__name__} must use publish")
                self._enqueue(websocket, queue, encode_event(event))
        await asyncio.sleep(0)

    def add_client(self, websocket: Any) -> None:
        """Register a client and start its independent sender task."""
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

    def _append_line(self, event_id: str, line: str) -> tuple[str, bool]:
        existing = self._index.get(event_id)
        if existing is not None:
            if existing == line:
                return line, False
            raise EventIdConflict(f"conflicting duplicate event id: {event_id}")
        try:
            append_serialized_event(self.path, line)
        except OSError as exc:
            raise LogAppendError(f"failed to append event {event_id}: {exc}") from exc
        self._index[event_id] = line
        return line, True

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

    @staticmethod
    def _require_persisted(event: Any) -> None:
        if not isinstance(event, _PERSISTED_TYPES):
            raise TypeError(f"event {type(event).__name__} is live-only")

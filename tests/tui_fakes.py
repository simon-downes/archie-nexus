"""Fakes for TUI tests: an orchestrator client, its streams and a recording listener.

``FakeOrchestrator`` implements ``archie_cli.tui.client.OrchestratorClient`` without a
server (084 D32). Each session has an event log that ``/events`` reads from (an
``after`` the log does not hold answers 409, as the server does), and scripted bodies
or errors can be queued ahead of it. ``stream()`` opens a ``FakeStream`` whose frames
come from a queue and whose sends are recorded. ``RecordingListener`` records every
``SessionListener`` call.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from archie_cli.tui.client import CursorNotFound
from archie_shared.events import encode_event
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

_CLOSE = object()


class FakeStream:
    """A connected session stream: frames from a queue, sends recorded.

    ``send_command``, ``send_message``, ``send_interrupt`` and ``disconnect`` are
    ``AsyncMock``s, so tests can assert on them or swap in failures; sends are also
    appended to ``sent`` as ``(kind, value)``.
    """

    def __init__(self, frames=()) -> None:
        self._frames: asyncio.Queue = asyncio.Queue()
        for frame in frames:
            self._frames.put_nowait(frame)
        self.connected = True
        self.sent: list[tuple[str, object]] = []
        self.send_command = AsyncMock(side_effect=lambda c: self.sent.append(("command", c)))
        self.send_message = AsyncMock(side_effect=lambda m: self.sent.append(("message", m)))
        self.send_interrupt = AsyncMock(side_effect=lambda: self.sent.append(("interrupt", None)))
        self.disconnect = AsyncMock(side_effect=self._disconnect)

    def _disconnect(self) -> None:
        self.connected = False
        self._frames.put_nowait(_CLOSE)

    def push(self, *events) -> None:
        """Deliver live frames."""
        for event in events:
            self._frames.put_nowait(event)

    def drop(self, code: int = 1006) -> None:
        """Fail the connection with a close ``code`` (as ``websockets`` does)."""
        self._frames.put_nowait(ConnectionClosedError(Close(code, ""), None))

    def close(self) -> None:
        """End the stream cleanly (the agent closed it)."""
        self._frames.put_nowait(_CLOSE)

    async def receive(self):
        while True:
            item = await self._frames.get()
            if item is _CLOSE:
                self.connected = False
                return
            if isinstance(item, Exception):
                self.connected = False
                raise item
            yield item


class FakeOrchestrator:
    """``OrchestratorClient`` with per-session logs, scripted replies and fake streams.

    Attributes:
        logs: session id → persisted events ``/events`` serves.
        requests: ``(session_id, after)`` for every ``events`` call.
        streams: Every stream opened, oldest first.
        deleted: Session ids passed to ``delete``.
        delete_status: Status ``delete`` returns.
    """

    def __init__(self, logs: dict[str, list] | None = None) -> None:
        self.logs: dict[str, list] = logs or {}
        self.requests: list[tuple[str, str | None]] = []
        self.streams: list[FakeStream] = []
        self.deleted: list[str] = []
        self.delete_status = 200
        self._replies: list[str | Exception] = []
        self._connect_errors: list[Exception] = []
        self._next_frames: list = []

    @property
    def latest(self) -> FakeStream:
        """The most recently opened stream."""
        return self.streams[-1]

    def reply(self, *items: str | Exception) -> None:
        """Queue ``/events`` replies (an NDJSON body or an error to raise) before the log."""
        self._replies.extend(items)

    def reject_cursor_once(self) -> None:
        """Answer the next ``/events`` request with 409."""
        self._replies.append(CursorNotFound("cursor gone", 409))

    def fail_connects(self, *errors: Exception) -> None:
        """Make the next ``stream`` calls raise these errors, in order."""
        self._connect_errors.extend(errors)

    def frames_on_connect(self, *events) -> None:
        """Frames the next opened stream delivers at once."""
        self._next_frames.extend(events)

    async def events(self, session_id: str, after: str | None) -> str:
        self.requests.append((session_id, after))
        await asyncio.sleep(0)  # a real request yields; buffered frames arrive meanwhile
        if self._replies:
            item = self._replies.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        log = self.logs.get(session_id, [])
        if after is not None:
            ids = [event.id for event in log]
            if after not in ids:
                raise CursorNotFound(f"event {after} is not in the log", 409)
            log = log[ids.index(after) + 1 :]
        return "".join(encode_event(event) + "\n" for event in log)

    async def stream(self, session_id: str) -> FakeStream:
        if self._connect_errors:
            raise self._connect_errors.pop(0)
        stream = FakeStream(self._next_frames)
        self._next_frames = []
        self.streams.append(stream)
        return stream

    async def delete(self, session_id: str) -> int:
        self.deleted.append(session_id)
        return self.delete_status


class RecordingListener:
    """A ``SessionListener`` that records each call as ``(name, *args)``."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def names(self) -> list[str]:
        """Call names in order."""
        return [call[0] for call in self.calls]

    def of(self, name: str) -> list[tuple]:
        """Arguments of every call named ``name``."""
        return [call[1:] for call in self.calls if call[0] == name]

    def changes(self) -> list:
        """Every reported change, flattened."""
        return [change for (changes,) in self.of("changes") for change in changes]

    def on_changes(self, changes) -> None:
        self.calls.append(("changes", changes))

    def on_local(self, entry) -> None:
        self.calls.append(("local", entry))

    def on_status(self) -> None:
        self.calls.append(("status",))

    def on_turn(self) -> None:
        self.calls.append(("turn",))

    def on_question(self) -> None:
        self.calls.append(("question",))

    def on_loaded(self) -> None:
        self.calls.append(("loaded",))

    async def on_reset(self) -> None:
        self.calls.append(("reset",))

    def on_replay(self, active: bool) -> None:
        self.calls.append(("replay", active))

    def on_notice(self, message: str, timeout: float | None) -> None:
        self.calls.append(("notice", message))


def make_controller(client: FakeOrchestrator | None = None, session_id: str = "s1"):
    """A ``SessionController`` over a fake client with a recording listener attached."""
    from archie_cli.tui.session import SessionController

    controller = SessionController(client or FakeOrchestrator(), session_id, f"archie-{session_id}")
    listener = RecordingListener()
    controller.attach(listener)
    return controller, listener


def local_texts(controller) -> list[str]:
    """Messages of the controller's client-error entries, in order."""
    return [entry.message for entry in controller.locals if entry.kind == "client_error"]

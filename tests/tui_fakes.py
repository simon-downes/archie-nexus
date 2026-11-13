"""Fakes for TUI tests: an orchestrator client, its streams, a recording listener and app
helpers.

``FakeOrchestrator`` implements ``archie_cli.tui.client.OrchestratorClient`` without a
server (084 D32). Each session has an event log that ``/events`` reads from (an
``after`` the log does not hold answers 409, as the server does), or a raw NDJSON body,
and scripted bodies or errors can be queued ahead of it. ``stream()`` opens a
``FakeStream`` whose frames come from a queue and whose sends are recorded. ``feed()``
sends a snapshot of the live records on every connect, then the records ``publish``
queues, until ``drop_feed`` (``fail_feed`` makes the next connects fail, as a stopped
orchestrator does). PATCH bodies and ``session`` fetches are recorded; ``session``
answers 404 for an ID ``records`` lacks. ``RecordingListener`` records every
``SessionListener`` call.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import msgspec
from archie_cli.tui.client import CursorNotFound, OrchestratorError, ProfileInfo
from archie_shared.events import encode_event
from archie_shared.session import SessionDescriptor, split_id
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

_CLOSE = object()
_DROP = object()

LOCAL_PROFILE = ProfileInfo(label="local", web_url="http://127.0.0.1:7600/", local=True)


def make_record(
    session_id: str = "s1",
    *,
    workspace: str | None = None,
    status: str = "idle",
    started_at: str = "2026-11-12T10:00:00+00:00",
    name: str | None = None,
    **fields,
) -> SessionDescriptor:
    """A session record; the workspace defaults to the ID's prefix (``ws`` for ``s1``)."""
    if workspace is None:
        prefix = split_id(session_id)[0] if "-" in session_id else ""
        workspace = prefix or "ws"
    return SessionDescriptor(
        session_id=session_id,
        workspace=workspace,
        backend=fields.pop("backend", "native"),
        name=name or session_id,
        status=status,
        started_at=started_at,
        **fields,
    )


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

    def __init__(
        self,
        logs: dict[str, list] | None = None,
        *,
        sessions: list[SessionDescriptor] | None = None,
    ) -> None:
        self.logs: dict[str, list] = logs or {}
        self.bodies: dict[str, str] = {}
        self.records: dict[str, SessionDescriptor] = {r.session_id: r for r in sessions or ()}
        self.requests: list[tuple[str, str | None]] = []
        self.streams: list[FakeStream] = []
        self.streams_by_session: dict[str, list[FakeStream]] = {}
        self.deleted: list[str] = []
        self.delete_status = 200
        self.patches: list[tuple[str, dict]] = []
        self.patch_gate: asyncio.Event | None = None  # set: PATCHes wait for it
        self.fetched: list[str] = []
        self.feed_connects = 0
        self._feed: asyncio.Queue | None = None
        self._feed_errors: list[Exception] = []
        self._replies: list[str | Exception] = []
        self._connect_errors: list[Exception] = []
        self._next_frames: list = []

    @property
    def latest(self) -> FakeStream:
        """The most recently opened stream."""
        return self.streams[-1]

    def stream_for(self, session_id: str) -> FakeStream:
        """The most recently opened stream of one session."""
        return self.streams_by_session[session_id][-1]

    def opened(self, session_id: str) -> int:
        """How many streams were opened for a session."""
        return len(self.streams_by_session.get(session_id, ()))

    def full_loads(self, session_id: str) -> int:
        """How many ``/events`` requests asked for a session's whole log."""
        return self.requests.count((session_id, None))

    # --- Feed -----------------------------------------------------------------------

    def _queue(self) -> asyncio.Queue:
        if self._feed is None:
            self._feed = asyncio.Queue()
        return self._feed

    def publish(self, record: SessionDescriptor) -> None:
        """Store a record and send it as a ``session`` feed event."""
        self.records[record.session_id] = record
        self._queue().put_nowait(("session", record))

    def drop_feed(self) -> None:
        """Fail the current feed connection."""
        self._queue().put_nowait(_DROP)

    def fail_feed(self, *errors: Exception) -> None:
        """Make the next ``feed`` connects raise these errors, in order."""
        self._feed_errors.extend(errors)

    async def feed(self):
        self.feed_connects += 1
        if self._feed_errors:
            raise self._feed_errors.pop(0)
        live = [r for r in self.records.values() if not r.ended]
        live.sort(key=lambda r: r.started_at, reverse=True)  # newest first, as the server
        yield "snapshot", live
        queue = self._queue()
        while True:
            item = await queue.get()
            if item is _DROP:
                raise OrchestratorError("feed dropped")
            yield item

    async def patch(self, session_id: str, body: dict) -> SessionDescriptor:
        self.patches.append((session_id, body))
        if self.patch_gate is not None:
            await self.patch_gate.wait()
        record = self.records.get(session_id)
        if record is None:
            raise OrchestratorError("session update failed: 404", 404)
        record = msgspec.structs.replace(record, **body)
        self.records[session_id] = record
        return record

    async def session(self, session_id: str) -> SessionDescriptor:
        self.fetched.append(session_id)
        await asyncio.sleep(0)
        record = self.records.get(session_id)
        if record is None:
            raise OrchestratorError("session request failed: 404", 404)
        return record

    # --- History and streams ------------------------------------------------------------

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
        if after is None and session_id in self.bodies:
            return self.bodies[session_id]
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
        self.streams_by_session.setdefault(session_id, []).append(stream)
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


def make_controller(
    client: FakeOrchestrator | None = None, session_id: str = "s1", *, is_live=None
):
    """A ``SessionController`` over a fake client with a recording listener attached."""
    from archie_cli.tui.session import SessionController

    controller = SessionController(
        client or FakeOrchestrator(), session_id, f"archie-{session_id}", is_live=is_live
    )
    listener = RecordingListener()
    controller.attach(listener)
    return controller, listener


def local_texts(controller) -> list[str]:
    """Messages of the controller's client-error entries, in order."""
    return [entry.message for entry in controller.locals if entry.kind == "client_error"]


# --- App helpers ----------------------------------------------------------------------


def make_app(
    client: FakeOrchestrator | None = None,
    *,
    focus: str | None = "s1",
    profile: ProfileInfo = LOCAL_PROFILE,
):
    """An ``ArchieApp`` over a fake client; by default one idle session ``s1``."""
    from archie_cli.tui.app import ArchieApp

    if client is None:
        client = FakeOrchestrator(sessions=[make_record("s1")])
    return ArchieApp(client, profile=profile, focus=focus)


async def settle(pilot, predicate=None, *, tries: int = 50) -> None:
    """Let the app run until ``predicate()`` holds (or a few frames without one)."""
    for _ in range(tries if predicate is not None else 3):
        if predicate is not None and predicate():
            return
        await pilot.pause()
    if predicate is not None:
        assert predicate(), "condition not reached"


async def attached(app, pilot, session_id: str | None = None):
    """Wait until the pane shows ``session_id`` (or any session) and it has loaded."""

    def ready() -> bool:
        pane = app.pane
        return (
            pane is not None
            and pane.controller is not None
            and (session_id is None or app.current == session_id)
            and pane.controller.loaded
        )

    await settle(pilot, ready)
    return app.pane


async def apply_events(app, pilot, *events) -> None:
    """Apply events to the on-screen controller, then let the widgets update."""
    for event in events:
        app.controller.apply(event)
    await pilot.pause()

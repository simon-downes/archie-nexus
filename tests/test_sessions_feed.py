"""Tests for GET /api/sessions/feed, the sessions SSE feed (082 M4, D11, D20).

``httpx.ASGITransport`` collects the whole body before it returns a response, so an
endless stream can't be read through it. :class:`Feed` drives the ASGI app directly
instead: it sees each body chunk as the app sends it, can hold ``send`` to stall the
client, and ends the request with ``http.disconnect`` as a closing client would.
"""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime

import msgspec
import pytest
from archie_orchestrator import api
from archie_orchestrator.app import app
from archie_orchestrator.tracker import FEED_QUEUE_SIZE, FeedSubscription
from archie_shared.schemas import NexusConfig
from archie_shared.session import SessionDescriptor
from httpx import ASGITransport, AsyncClient

from tests.session_fakes import SID, FakeRuntime, make_tracker, record, serve

ENDED = "proj-01kx77jkz8"
TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def _app_state():
    app.state.config = NexusConfig()
    app.state.start_time = datetime.now(UTC)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _records() -> list[SessionDescriptor]:
    return [
        record(SID, status="working", started_at="2026-07-02T10:00:00+00:00"),
        record(
            ENDED,
            status="stopped",
            started_at="2026-07-01T10:00:00+00:00",
            ended_at="2026-07-01T11:00:00+00:00",
        ),
    ]


async def _until(predicate, timeout: float = TIMEOUT) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def _parse(block: str) -> dict:
    """One SSE message as ``{event, data}`` or ``{comment}``."""
    message: dict = {}
    data = []
    for line in block.split("\n"):
        if line.startswith(":"):
            message["comment"] = line[1:].strip()
        elif line.startswith("event: "):
            message["event"] = line[7:]
        elif line.startswith("data: "):
            data.append(line[6:])
    if data:
        message["data"] = json.loads("\n".join(data))
    return message


class Feed:
    """An open ``GET /api/sessions/feed`` request, driven over raw ASGI.

    Args:
        query: The query string, e.g. ``include=ended``.
    """

    def __init__(self, query: str = "") -> None:
        self.query = query
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.ended = False
        # While set to an unset event, the app's ``send`` waits: a stalled client.
        self.hold: asyncio.Event | None = None
        self._requested = False
        self._disconnect = asyncio.Event()
        self._messages: asyncio.Queue[dict] = asyncio.Queue()
        self._buffer = ""
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "Feed":
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/sessions/feed",
            "raw_path": b"/api/sessions/feed",
            "root_path": "",
            "query_string": self.query.encode(),
            "headers": [(b"host", b"test")],
            "client": ("127.0.0.1", 50000),
            "server": ("test", 80),
        }
        self._task = asyncio.create_task(app(scope, self._receive, self._send))
        start = await asyncio.wait_for(self._messages.get(), TIMEOUT)
        assert start["type"] == "http.response.start"
        self.status = start["status"]
        self.headers = {k.decode(): v.decode() for k, v in start["headers"]}
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self.hold is not None:
            self.hold.set()
        self._disconnect.set()
        await asyncio.wait_for(self._task, TIMEOUT)

    async def _receive(self) -> dict:
        if not self._requested:
            self._requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict) -> None:
        if self.hold is not None:
            await self.hold.wait()
        await self._messages.put(message)

    async def next(self, timeout: float = TIMEOUT) -> dict | None:
        """The next SSE message, or None once the response body has ended."""
        while "\n\n" not in self._buffer:
            if self.ended:
                return None
            message = await asyncio.wait_for(self._messages.get(), timeout)
            assert message["type"] == "http.response.body"
            self._buffer += message.get("body", b"").decode()
            self.ended = not message.get("more_body", False)
        block, self._buffer = self._buffer.split("\n\n", 1)
        return _parse(block)


async def _snapshot(feed: Feed) -> list[dict]:
    message = await feed.next()
    assert message is not None and message["event"] == "snapshot"
    assert list(message["data"]) == ["sessions"]
    return message["data"]["sessions"]


async def _session_event(feed: Feed) -> dict:
    message = await feed.next()
    assert message is not None and message.get("event") == "session", message
    return message["data"]


# ---------------------------------------------------------------------------
# Snapshot and filter
# ---------------------------------------------------------------------------


async def test_snapshot_holds_what_the_list_returns_for_each_filter():
    with serve(app, records=_records()):
        async with _client() as http:
            listed = (await http.get("/api/sessions")).json()
            listed_all = (await http.get("/api/sessions", params={"include": "ended"})).json()
        async with Feed() as feed:
            assert feed.status == 200
            assert feed.headers["content-type"].startswith("text/event-stream")
            assert feed.headers["cache-control"] == "no-cache"
            assert feed.headers["x-accel-buffering"] == "no"
            live = await _snapshot(feed)
        async with Feed("include=ended") as feed:
            every = await _snapshot(feed)
    assert live == listed
    assert [s["session_id"] for s in live] == [SID]
    assert every == listed_all
    assert [(s["session_id"], s["status"]) for s in every] == [
        (SID, "working"),
        (ENDED, "stopped"),
    ]
    assert msgspec.convert(every[0], SessionDescriptor) == _records()[0]


async def test_empty_snapshot():
    with serve(app):
        async with Feed() as feed:
            assert await _snapshot(feed) == []


async def test_bad_include_is_400():
    with serve(app) as tracker:
        async with _client() as http:
            response = await http.get("/api/sessions/feed", params={"include": "all"})
    assert response.status_code == 400
    assert response.json() == {"error": "Invalid 'include': expected 'ended'"}
    assert tracker._listeners == []


async def test_store_failure_is_500_and_unsubscribes(monkeypatch):
    with serve(app) as tracker:
        monkeypatch.setattr(
            tracker.store,
            "list",
            lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
        )
        async with _client() as http:
            response = await http.get("/api/sessions/feed", params={"include": "ended"})
    assert response.status_code == 500
    assert response.json() == {"error": "Session store unavailable"}
    assert tracker._listeners == []


# ---------------------------------------------------------------------------
# Change events
# ---------------------------------------------------------------------------


async def test_patch_is_sent_as_a_session_event():
    with serve(app, records=_records()):
        async with Feed() as feed, _client() as http:
            await _snapshot(feed)
            response = await http.patch(f"/api/sessions/{SID}", json={"name": "Docs pass"})
            event = await _session_event(feed)
    assert response.status_code == 200
    assert event == response.json()
    assert event["name"] == "Docs pass" and event["name_source"] == "user"
    assert msgspec.convert(event, SessionDescriptor).status == "working"


async def test_patch_of_an_ended_session_is_sent_without_include_ended():
    with serve(app, records=_records()):
        async with Feed() as feed, _client() as http:
            assert [s["session_id"] for s in await _snapshot(feed)] == [SID]
            await http.patch(f"/api/sessions/{ENDED}", json={"name": "Old"})
            event = await _session_event(feed)
    assert (event["session_id"], event["name"], event["status"]) == (ENDED, "Old", "stopped")


async def test_a_session_that_ends_sends_its_terminal_record():
    runtime = FakeRuntime()
    with serve(app, records=_records(), runtime=runtime) as tracker:
        async with Feed() as feed, _client() as http:
            assert [s["session_id"] for s in await _snapshot(feed)] == [SID]
            response = await http.delete(f"/api/sessions/{SID}")
            stopping = await _session_event(feed)
            stopped = await _session_event(feed)
        async with Feed() as feed:
            after = await _snapshot(feed)
        await tracker.close()
    assert (stopping["session_id"], stopping["status"]) == (SID, "stopping")
    assert stopped == response.json()
    assert stopped["status"] == "stopped" and stopped["ended_at"] is not None
    assert stopped["container_name"] is None
    assert after == []


async def test_change_while_the_snapshot_is_read_is_not_lost():
    """D20: the queue is registered before the snapshot is built."""
    with serve(app, records=_records()) as tracker:
        changed = msgspec.structs.replace(tracker.live(SID), status="blocked")
        original = tracker.list

        async def list_and_change(include_ended=False):
            records = await original(include_ended=include_ended)
            tracker._publish(changed)  # published after the snapshot was read
            return records

        tracker.list = list_and_change
        async with Feed() as feed:
            [snapshot] = await _snapshot(feed)
            event = await _session_event(feed)
    assert snapshot["status"] == "working"
    assert event["status"] == "blocked"


async def test_ping_after_an_idle_interval(monkeypatch):
    monkeypatch.setattr(api, "FEED_PING_INTERVAL", 0.05)
    with serve(app, records=_records()) as tracker:
        async with Feed() as feed:
            await _snapshot(feed)
            assert await feed.next() == {"comment": "ping"}
            assert await feed.next() == {"comment": "ping"}
            await tracker.patch(SID, unseen=False)
            message = await feed.next()
            while message == {"comment": "ping"}:
                message = await feed.next()
    assert message["event"] == "session"
    assert message["data"]["session_id"] == SID


# ---------------------------------------------------------------------------
# Subscriber lifetime
# ---------------------------------------------------------------------------


async def test_client_disconnect_unsubscribes():
    with serve(app, records=_records()) as tracker:
        async with Feed() as feed:
            await _snapshot(feed)
            assert len(tracker._listeners) == 1
        assert tracker._listeners == []
        # Publishing after the client left reaches nobody and doesn't fail.
        await tracker.patch(SID, unseen=False)


async def test_stalled_client_is_dropped_and_its_stream_ends():
    with serve(app, records=_records()) as tracker:
        async with Feed() as feed:
            await _snapshot(feed)
            [offer] = tracker._listeners
            subscription = offer.__self__
            feed.hold = asyncio.Event()
            first = msgspec.structs.replace(tracker.live(SID), name="first")
            tracker._publish(first)
            # The stream takes the first record and blocks sending it.
            await _until(subscription._queue.empty)
            for i in range(FEED_QUEUE_SIZE + 1):
                tracker._publish(msgspec.structs.replace(first, name=f"n{i}"))
            assert subscription.dropped
            assert tracker._listeners == []
            feed.hold.set()
            event = await _session_event(feed)
            assert await feed.next() is None
            assert feed.ended
    assert event["name"] == "first"


async def test_feed_route_does_not_shadow_session_get():
    with serve(app, records=_records()):
        async with _client() as http:
            one = await http.get(f"/api/sessions/{SID}")
            feed_bad = await http.get("/api/sessions/feed", params={"include": "x"})
        async with Feed() as feed:
            await _snapshot(feed)
    assert one.status_code == 200
    assert one.json()["session_id"] == SID
    # /feed reaches the feed handler, not session_get with the ID "feed".
    assert feed_bad.status_code == 400
    assert "include" in feed_bad.json()["error"]
    assert feed.headers["content-type"].startswith("text/event-stream")


# ---------------------------------------------------------------------------
# FeedSubscription
# ---------------------------------------------------------------------------


async def test_subscription_queues_every_published_record_in_order():
    tracker = make_tracker()
    with tracker.subscribe_feed() as subscription:
        assert isinstance(subscription, FeedSubscription)
        for name in ("a", "b", "c"):
            tracker._publish(record(SID, name=name))
        names = [(await asyncio.wait_for(subscription.get(), TIMEOUT)).name for _ in range(3)]
    assert names == ["a", "b", "c"]
    assert tracker._listeners == []


async def test_subscription_overflow_drops_only_that_subscriber():
    tracker = make_tracker()
    slow = tracker.subscribe_feed(maxsize=2)
    with tracker.subscribe_feed() as fast:
        for name in ("a", "b", "c", "d"):
            tracker._publish(record(SID, name=name))
        assert slow.dropped and not fast.dropped
        assert await asyncio.wait_for(slow.get(), TIMEOUT) is None
        assert [(await fast.get()).name for _ in range(4)] == ["a", "b", "c", "d"]
        assert tracker._listeners == [fast._offer]
    slow.close()  # closing again is harmless
    assert tracker._listeners == []

"""Tests for ordered session persistence and client delivery."""

import asyncio
import json

import pytest
from archie_agent.session_bus import SessionEventBus
from archie_shared.events import Event, SessionStarted, TextDelta, UserMessage
from archie_shared.session.log import EventIdConflict, LogAppendError, SessionLog


class UnencodableLiveEvent(Event):
    payload: object


class FakeWebSocket:
    def __init__(self, *, block: bool = False):
        self.messages: list[str] = []
        self.block = block
        self.closed = False
        self.release = asyncio.Event()

    async def send_text(self, line: str) -> None:
        if self.block:
            await self.release.wait()
        self.messages.append(line)

    async def close(self, **kwargs) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_publish_order_matches_log_and_each_client(tmp_path):
    bus = SessionEventBus(tmp_path / "session.jsonl")
    first = FakeWebSocket()
    second = FakeWebSocket()
    await bus.register_client(first, ())
    await bus.register_client(second, ())

    events = [
        SessionStarted(
            id="01J00000000000000000000001", schema_version=2, sent_at="now", model_key="m"
        ),
        UserMessage(id="01J00000000000000000000002", turn=1, scope=None, content="hello"),
        UserMessage(id="01J00000000000000000000003", turn=1, scope="child", content="child"),
    ]
    for event in events:
        await bus.emit(event)
    await asyncio.sleep(0)

    expected = [json.loads(line) for line in (tmp_path / "session.jsonl").read_text().splitlines()]
    assert [json.loads(line) for line in first.messages] == expected
    assert [json.loads(line) for line in second.messages] == expected

    bus.discard_client(first)
    bus.discard_client(second)


def test_duplicate_and_conflicting_ids_use_append_index(tmp_path):
    path = tmp_path / "session.jsonl"
    session_log = SessionLog(path)
    event = SessionStarted(
        id="01J00000000000000000000001", schema_version=2, sent_at="now", model_key="m"
    )

    first = session_log.append(event)
    assert first is True
    assert session_log.append(event) is False
    assert len(path.read_text().splitlines()) == 1

    conflicting = SessionStarted(
        id=event.id, schema_version=2, sent_at="later", model_key="different"
    )
    with pytest.raises(EventIdConflict):
        session_log.append(conflicting)


def test_empty_event_id_is_rejected(tmp_path):
    session_log = SessionLog(tmp_path / "session.jsonl")
    event = UserMessage(id="", turn=1, scope=None, content="hello")

    with pytest.raises(ValueError, match="non-empty id"):
        session_log.append(event)
    assert not (tmp_path / "session.jsonl").exists()


def test_append_failure_is_typed(tmp_path, monkeypatch):
    session_log = SessionLog(tmp_path / "session.jsonl")
    event = SessionStarted(
        id="01J00000000000000000000001", schema_version=2, sent_at="now", model_key="m"
    )

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("archie_shared.session.log._append_serialized_event", fail)
    with pytest.raises(LogAppendError, match="disk full"):
        session_log.append(event)


@pytest.mark.asyncio
async def test_stalled_client_isolated_by_bounded_queue(tmp_path):
    bus = SessionEventBus(tmp_path / "session.jsonl", queue_size=2)
    stalled = FakeWebSocket(block=True)
    healthy = FakeWebSocket()
    await bus.register_client(stalled, ())
    await bus.register_client(healthy, ())

    for index in range(4):
        await bus.emit(
            UserMessage(
                id=f"01J0000000000000000000000{index + 1}",
                turn=1,
                scope=None,
                content=str(index),
            )
        )
        await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert stalled not in bus.clients
    assert len(healthy.messages) == 4
    assert stalled.closed is True
    bus.discard_client(healthy)


@pytest.mark.asyncio
async def test_concurrent_producers_share_one_append_and_delivery_order(tmp_path):
    bus = SessionEventBus(tmp_path / "session.jsonl")
    client = FakeWebSocket()
    await bus.register_client(client, ())

    events = [
        UserMessage(
            id=f"01J000000000000000000001{index:02d}",
            turn=1,
            scope=scope,
            content=content,
        )
        for index, (scope, content) in enumerate(
            [(None, "root"), ("child-a", "a"), ("child-b", "b")], 1
        )
    ]
    await asyncio.gather(*(bus.emit(event) for event in events))
    await asyncio.sleep(0)

    log_lines = (tmp_path / "session.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in client.messages] == [
        json.loads(line) for line in log_lines
    ]
    assert [json.loads(line)["id"] for line in log_lines] == [event.id for event in bus.log.read()]
    bus.discard_client(client)


@pytest.mark.asyncio
async def test_live_event_is_queued_without_persistence(tmp_path):
    path = tmp_path / "session.jsonl"
    bus = SessionEventBus(path)
    client = FakeWebSocket()
    await bus.register_client(client, ())

    await bus.emit(
        TextDelta(
            id="01J00000000000000000000001",
            turn=1,
            iteration=0,
            scope=None,
            request_id="request",
            text="hello",
        )
    )
    await asyncio.sleep(0)

    assert not path.exists()
    assert json.loads(client.messages[0])["type"] == "text_delta"
    bus.discard_client(client)


@pytest.mark.asyncio
async def test_identical_publish_retry_is_not_rebroadcast(tmp_path):
    bus = SessionEventBus(tmp_path / "session.jsonl")
    client = FakeWebSocket()
    await bus.register_client(client, ())
    event = UserMessage(id="01J00000000000000000000001", turn=1, scope=None, content="hello")

    await bus.emit(event)
    await bus.emit(event)
    await asyncio.sleep(0)

    assert len((tmp_path / "session.jsonl").read_text().splitlines()) == 1
    assert len(client.messages) == 1
    bus.discard_client(client)


@pytest.mark.asyncio
async def test_register_client_encodes_before_mutating_client_state(tmp_path):
    bus = SessionEventBus(tmp_path / "session.jsonl")
    websocket = FakeWebSocket()

    with pytest.raises(TypeError):
        await bus.register_client(websocket, (UnencodableLiveEvent(payload=object()),))

    assert websocket not in bus.clients
    assert websocket not in bus._senders


@pytest.mark.asyncio
async def test_register_client_rejects_persisted_initial_events_atomically(tmp_path):
    bus = SessionEventBus(tmp_path / "session.jsonl")
    websocket = FakeWebSocket()
    persisted = SessionStarted(
        id="01J00000000000000000000001", schema_version=2, sent_at="now", model_key="m"
    )

    with pytest.raises(TypeError, match="live-only"):
        await bus.register_client(websocket, (persisted,))

    assert websocket not in bus.clients
    await bus.emit(
        TextDelta(
            id="01J00000000000000000000002",
            turn=1,
            iteration=0,
            scope=None,
            request_id="r",
            text="live",
        )
    )
    assert websocket.messages == []

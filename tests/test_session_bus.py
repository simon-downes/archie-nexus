"""Tests for ordered session persistence and client delivery."""

import asyncio
import json

import pytest
from archie_agent.session_bus import EventIdConflict, LogAppendError, SessionEventBus
from archie_shared.canonical_events import SessionStarted, TextDelta, UserMessage


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
    bus.add_client(first)
    bus.add_client(second)

    events = [
        SessionStarted(
            id="01J00000000000000000000001", schema_version=1, sent_at="now", model_key="m"
        ),
        UserMessage(id="01J00000000000000000000002", turn=1, scope=None, content="hello"),
        UserMessage(id="01J00000000000000000000003", turn=1, scope="child", content="child"),
    ]
    for event in events:
        await bus.publish(event)
    await asyncio.sleep(0)

    expected = [json.loads(line) for line in (tmp_path / "session.jsonl").read_text().splitlines()]
    assert [json.loads(line) for line in first.messages] == expected
    assert [json.loads(line) for line in second.messages] == expected

    bus.discard_client(first)
    bus.discard_client(second)


def test_duplicate_and_conflicting_ids_use_append_index(tmp_path):
    path = tmp_path / "session.jsonl"
    bus = SessionEventBus(path)
    event = SessionStarted(
        id="01J00000000000000000000001", schema_version=1, sent_at="now", model_key="m"
    )

    first = bus.append(event)
    assert bus.append(event) == first
    assert len(path.read_text().splitlines()) == 1

    conflicting = SessionStarted(
        id=event.id, schema_version=2, sent_at="later", model_key="different"
    )
    with pytest.raises(EventIdConflict):
        bus.append(conflicting)


def test_append_failure_is_typed(tmp_path, monkeypatch):
    bus = SessionEventBus(tmp_path / "session.jsonl")
    event = SessionStarted(
        id="01J00000000000000000000001", schema_version=1, sent_at="now", model_key="m"
    )

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("archie_agent.session_bus.append_serialized_event", fail)
    with pytest.raises(LogAppendError, match="disk full"):
        bus.append(event)


@pytest.mark.asyncio
async def test_stalled_client_isolated_by_bounded_queue(tmp_path):
    bus = SessionEventBus(tmp_path / "session.jsonl", queue_size=2)
    stalled = FakeWebSocket(block=True)
    healthy = FakeWebSocket()
    bus.add_client(stalled)
    bus.add_client(healthy)

    for index in range(4):
        await bus.publish(
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
    bus.add_client(client)

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
    await asyncio.gather(*(bus.publish(event) for event in events))
    await asyncio.sleep(0)

    log_lines = (tmp_path / "session.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in client.messages] == [
        json.loads(line) for line in log_lines
    ]
    assert [json.loads(line)["id"] for line in log_lines] == list(bus.index)
    bus.discard_client(client)


@pytest.mark.asyncio
async def test_live_event_is_queued_without_persistence(tmp_path):
    path = tmp_path / "session.jsonl"
    bus = SessionEventBus(path)
    client = FakeWebSocket()
    bus.add_client(client)

    await bus.broadcast(
        TextDelta(
            id="01J00000000000000000000001",
            turn_iteration="1.0",
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
    bus.add_client(client)
    event = UserMessage(id="01J00000000000000000000001", turn=1, scope=None, content="hello")

    await bus.publish(event)
    await bus.publish(event)
    await asyncio.sleep(0)

    assert len((tmp_path / "session.jsonl").read_text().splitlines()) == 1
    assert len(client.messages) == 1
    bus.discard_client(client)

"""Tests for the TUI WebSocket client (archie_cli.ws_client.WSClient).

Focus: the receive() generator must NOT silently swallow an unexpected
connection close. It must clear the connection reference and re-raise
ConnectionClosed so the TUI can surface a client error and reconnect. A clean
close (ConnectionClosedOK) terminates quietly.
"""

from __future__ import annotations

import pytest
import websockets.exceptions
from archie_cli.ws_client import WSClient
from websockets.frames import Close


class _FakeConnection:
    """Minimal stand-in for websockets.ClientConnection.

    Async-iterates over the queued raw frames, then raises the configured
    close exception (mirroring how the real client ends its iterator).
    """

    def __init__(self, frames: list[str], close_exc: BaseException | None) -> None:
        self._frames = list(frames)
        self._close_exc = close_exc
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        if self._close_exc is not None:
            raise self._close_exc
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True


def _closed(ok: bool) -> websockets.exceptions.ConnectionClosed:
    frame = Close(1000 if ok else 1006, "")
    cls = (
        websockets.exceptions.ConnectionClosedOK
        if ok
        else websockets.exceptions.ConnectionClosedError
    )
    # rcvd only (sent=None) => rcvd_then_sent must stay None.
    return cls(frame, None)


def _serialize_text_event() -> str:
    """A canonical live-only TextDelta frame."""
    from archie_shared.canonical_events import TextDelta, encode_event

    return encode_event(
        TextDelta(
            id="01J00000000000000000000001",
            turn_iteration="1.0",
            scope=None,
            request_id="request",
            text="hi",
        )
    )


async def test_receive_reraises_on_unexpected_close():
    """An abnormal close must propagate and clear the connection."""
    client = WSClient()
    client._ws = _FakeConnection([], _closed(ok=False))

    with pytest.raises(websockets.exceptions.ConnectionClosed):
        async for _ in client.receive():
            pass

    assert client.connected is False


async def test_receive_quiet_on_clean_close():
    """A clean close must terminate the generator without raising."""
    client = WSClient()
    client._ws = _FakeConnection([], _closed(ok=True))

    events = [e async for e in client.receive()]

    assert events == []
    assert client.connected is False


async def test_receive_yields_then_reraises():
    """Events are yielded before an unexpected close is surfaced."""
    client = WSClient()
    client._ws = _FakeConnection([_serialize_text_event()], _closed(ok=False))

    received = []
    with pytest.raises(websockets.exceptions.ConnectionClosed):
        async for event in client.receive():
            received.append(event)

    assert len(received) == 1
    assert client.connected is False


async def test_receive_without_connection_raises_runtime_error():
    client = WSClient()
    with pytest.raises(RuntimeError, match="Not connected"):
        async for _ in client.receive():
            pass


async def test_send_message_without_connection_raises():
    client = WSClient()
    with pytest.raises(RuntimeError, match="Not connected"):
        await client.send_message("hello")


async def test_connected_property_reflects_state():
    client = WSClient()
    assert client.connected is False
    client._ws = _FakeConnection([], None)
    assert client.connected is True
    await client.disconnect()
    assert client.connected is False


async def test_receive_decodes_tool_input_with_data_key():
    from archie_shared.canonical_events import ToolCall, encode_event

    client = WSClient()
    client._ws = _FakeConnection(
        [
            encode_event(
                ToolCall(
                    id="01J00000000000000000000001",
                    turn_iteration="1.0",
                    scope="child",
                    request_id="request",
                    tool_use_id="tool",
                    name="read",
                    input={"data": {"path": "/tmp/file"}},
                )
            )
        ],
        None,
    )

    received = [event async for event in client.receive()]
    assert received[0].input == {"data": {"path": "/tmp/file"}}

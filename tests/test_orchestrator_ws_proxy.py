"""Tests for orchestrator WebSocket proxy (WS /sessions/{id}/stream)."""

from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from archie_orchestrator.app import app
from archie_shared.schemas import NexusConfig
from archie_shared.session import SessionDescriptor
from starlette.testclient import TestClient
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidMessage, InvalidStatus
from websockets.http11 import Response as HandshakeResponse

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def prime_app_state():
    """Set app.state.config before each test."""
    app.state.config = NexusConfig()
    yield


@pytest.fixture
def test_client():
    return TestClient(app, raise_server_exceptions=False)


def _running_session(session_id: str = "proj-01abc12345", port: int = 32771) -> SessionDescriptor:
    return SessionDescriptor(
        session_id=session_id,
        container_name=f"archie-{session_id}",
        port=port,
        raw_docker_status="Up",
    )


# ---------------------------------------------------------------------------
# WS /sessions/{id}/stream
# ---------------------------------------------------------------------------


def test_ws_stream_session_not_found(test_client):
    """WS connect for unknown session → server closes with code 4004."""
    with patch("archie_orchestrator.proxy.list_sessions", return_value=[]):
        # TestClient raises WebSocketDisconnect when the server closes before accept
        from starlette.websockets import WebSocketDisconnect as StarletteDisconnect

        with pytest.raises(StarletteDisconnect) as exc_info:
            with test_client.websocket_connect("/api/sessions/unknown-01abc12345/stream"):
                pass
        assert exc_info.value.code == 4004


def test_ws_stream_backend_unreachable(test_client):
    """WS connect when backend refuses → server accepts then closes with 4002."""
    session = _running_session()

    @asynccontextmanager
    async def refusing_connect(url, **kwargs):
        raise ConnectionRefusedError("refused")
        yield  # pragma: no cover

    from starlette.websockets import WebSocketDisconnect as StarletteDisconnect

    with (
        patch("archie_orchestrator.proxy.list_sessions", return_value=[session]),
        patch("archie_orchestrator.proxy.websockets.connect", refusing_connect),
    ):
        # Server accepts the connection then closes with 4002 after failing to
        # connect to backend. TestClient raises WebSocketDisconnect on receive.
        with test_client.websocket_connect("/api/sessions/proj-01abc12345/stream") as ws:
            with pytest.raises(StarletteDisconnect) as exc_info:
                ws.receive_text()
        assert exc_info.value.code == 4002


@pytest.mark.parametrize(
    "handshake_error",
    [
        InvalidMessage("did not receive a valid HTTP response"),
        InvalidStatus(HandshakeResponse(503, "Service Unavailable", Headers(), b"")),
    ],
    ids=["invalid-message", "invalid-status"],
)
def test_ws_stream_backend_protocol_loss_is_expected(test_client, caplog, handshake_error):
    """A failed backend handshake closes with 4002 without an internal error log."""
    session = _running_session()
    from starlette.websockets import WebSocketDisconnect as StarletteDisconnect

    @asynccontextmanager
    async def invalid_connect(url, **kwargs):
        raise handshake_error
        yield  # pragma: no cover

    with (
        patch("archie_orchestrator.proxy.list_sessions", return_value=[session]),
        patch("archie_orchestrator.proxy.websockets.connect", invalid_connect),
        caplog.at_level("ERROR", logger="archie_orchestrator.proxy"),
    ):
        with test_client.websocket_connect(f"/api/sessions/{session.session_id}/stream") as ws:
            with pytest.raises(StarletteDisconnect) as exc_info:
                ws.receive_text()

    assert exc_info.value.code == 4002
    assert not any("WS proxy error" in record.message for record in caplog.records)


def test_ws_stream_backend_close_during_relay_is_expected(test_client, caplog):
    """An established backend close is relayed as session-unreachable."""
    session = _running_session()
    from starlette.websockets import WebSocketDisconnect as StarletteDisconnect

    class ClosingBackend:
        async def send(self, msg: str) -> None:
            pass

        def __aiter__(self):
            return self._frames()

        async def _frames(self):
            # websockets ends iteration on a clean close; only an abnormal
            # close (ConnectionClosedError) escapes the relay.
            raise ConnectionClosedError(None, None)
            yield  # pragma: no cover

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    @asynccontextmanager
    async def closing_connect(url, **kwargs):
        yield ClosingBackend()

    with (
        patch("archie_orchestrator.proxy.list_sessions", return_value=[session]),
        patch("archie_orchestrator.proxy.websockets.connect", closing_connect),
        caplog.at_level("ERROR", logger="archie_orchestrator.proxy"),
    ):
        with test_client.websocket_connect(f"/api/sessions/{session.session_id}/stream") as ws:
            with pytest.raises(StarletteDisconnect) as exc_info:
                ws.receive_text()

    assert exc_info.value.code == 4002
    assert not any("WS proxy error" in record.message for record in caplog.records)


def test_ws_stream_text_relay(test_client):
    """WS relay forwards text frames from backend to client."""
    session = _running_session()

    class FakeBackend:
        """Fake websockets backend that sends one message then closes."""

        async def send(self, msg: str) -> None:
            pass  # client→backend direction not exercised in this test

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            yield "hello from backend"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    @asynccontextmanager
    async def fake_connect(url, **kwargs):
        yield FakeBackend()

    with (
        patch("archie_orchestrator.proxy.list_sessions", return_value=[session]),
        patch("archie_orchestrator.proxy.websockets.connect", fake_connect),
    ):
        with test_client.websocket_connect("/api/sessions/proj-01abc12345/stream") as ws:
            msg = ws.receive_text()
            assert msg == "hello from backend"

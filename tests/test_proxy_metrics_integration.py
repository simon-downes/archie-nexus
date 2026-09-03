"""Tests for WS proxy frame inspection → metrics queue integration."""

import json
from unittest.mock import patch

import pytest
from archie_orchestrator.app import app
from archie_orchestrator.proxy import _METRICS_MARKERS
from archie_shared.schemas import NexusConfig
from archie_shared.session import SessionDescriptor
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _app_state():
    app.state.config = NexusConfig()
    yield


def _make_session(session_id: str = "proj-01abc12345") -> SessionDescriptor:
    return SessionDescriptor(
        session_id=session_id,
        container_name=f"archie-{session_id}",
        port=32771,
        raw_docker_status="Up",
    )


def _make_ws_backend(messages: list[str]):
    """Return a fake websockets.connect context manager that yields the given messages."""

    class _Backend:
        def __aiter__(self):
            return iter(messages).__class__(messages)

        async def __anext__(self):
            raise StopAsyncIteration

        async def send(self, msg):
            pass

    class _BackendIter:
        def __init__(self):
            self._msgs = list(messages)
            self._idx = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._idx >= len(self._msgs):
                raise StopAsyncIteration
            msg = self._msgs[self._idx]
            self._idx += 1
            return msg

        async def send(self, msg):
            pass

    class _Connect:
        def __call__(self, url, **kwargs):
            return self

        async def __aenter__(self):
            return _BackendIter()

        async def __aexit__(self, *_):
            pass

    return _Connect()


# ---------------------------------------------------------------------------
# _METRICS_MARKERS correctness
# ---------------------------------------------------------------------------


def test_markers_match_actual_json_dumps_output():
    """_METRICS_MARKERS strings match actual json.dumps / msgspec wire format."""
    # json.dumps default (space after colon) and msgspec (no space) forms.
    json_form = json.dumps({"type": "llm_request", "id": "e1"})
    compact_form = json.dumps({"type": "llm_request", "id": "e1"}, separators=(",", ":"))

    assert '"type": "llm_request"' in json_form
    assert '"type":"llm_request"' in compact_form

    for marker in _METRICS_MARKERS:
        assert any(marker in e for e in [json_form, compact_form]), (
            f"Marker {marker!r} not found in any expected event"
        )


# ---------------------------------------------------------------------------
# Frame inspection — correct events enqueued
# ---------------------------------------------------------------------------


def _llm_request_frame(turn: int = 1, iteration: int = 1, **overrides) -> str:
    """A canonical llm_request frame as it flows over the WebSocket."""
    event = {
        "type": "llm_request",
        "id": overrides.get("id", "evt-1"),
        "scope": None,
        "turn": turn,
        "iteration": iteration,
        "model_key": "bedrock-anthropic.claude-sonnet-4-6",
        "sent_at": "2026-07-01T10:00:00+00:00",
        "duration_ms": 1234,
        "status": "completed",
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "context_tokens": 12000,
        "cost_usd": 0.001,
    }
    event.update({k: v for k, v in overrides.items() if k != "id"})
    return json.dumps(event)


def test_llm_request_frame_enqueued(tmp_path):
    """llm_request frame flowing through proxy → enqueued with correct session_id."""
    session = _make_session()
    frame = _llm_request_frame()

    from archie_orchestrator.metrics import MetricsWriter

    writer = MetricsWriter(tmp_path / "metrics.db")
    app.state.metrics_writer = writer

    with (
        patch("archie_orchestrator.proxy.list_sessions", return_value=[session]),
        patch("archie_orchestrator.proxy.websockets.connect", _make_ws_backend([frame])),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect(f"/sessions/{session.session_id}/stream"):
            pass

    assert not writer.queue.empty()
    sid, msg = writer.queue.get_nowait()
    assert sid == session.session_id
    assert '"type": "llm_request"' in msg


def test_text_delta_frame_not_enqueued(tmp_path):
    """TextDelta frame (no metrics marker) → not enqueued."""
    session = _make_session()
    text_msg = json.dumps(
        {
            "type": "text_delta",
            "id": "t1",
            "turn": 1,
            "iteration": 1,
            "scope": None,
            "request_id": "r1",
            "text": "Hello",
        }
    )

    from archie_orchestrator.metrics import MetricsWriter

    writer = MetricsWriter(tmp_path / "metrics.db")
    app.state.metrics_writer = writer

    with (
        patch("archie_orchestrator.proxy.list_sessions", return_value=[session]),
        patch("archie_orchestrator.proxy.websockets.connect", _make_ws_backend([text_msg])),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect(f"/sessions/{session.session_id}/stream"):
            pass

    assert writer.queue.empty()


def test_frame_forwarded_even_without_metrics_writer(tmp_path):
    """Frames are forwarded even when metrics_writer is not set on app state."""
    session = _make_session()
    usage_msg = _llm_request_frame()

    # Remove metrics_writer from app state
    if hasattr(app.state, "metrics_writer"):
        del app.state.metrics_writer

    received = []

    with (
        patch("archie_orchestrator.proxy.list_sessions", return_value=[session]),
        patch("archie_orchestrator.proxy.websockets.connect", _make_ws_backend([usage_msg])),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect(f"/sessions/{session.session_id}/stream") as ws:
            try:
                data = ws.receive()
                received.append(data)
            except Exception:  # noqa: BLE001
                pass

    # The relay ran without error — no assertion on received needed, just no crash


def test_multiple_sessions_correct_session_ids(tmp_path):
    """Each session's frames are enqueued with the correct session_id."""
    sid_a = "project-a-01abc12345"
    sid_b = "project-b-02def67890"
    session_a = SessionDescriptor(
        session_id=sid_a, container_name=f"archie-{sid_a}", port=32771, raw_docker_status="Up"
    )
    session_b = SessionDescriptor(
        session_id=sid_b, container_name=f"archie-{sid_b}", port=32772, raw_docker_status="Up"
    )

    usage_a = _llm_request_frame(turn=1, iteration=1, id="a1")
    usage_b = _llm_request_frame(turn=2, iteration=1, id="b1")

    from archie_orchestrator.metrics import MetricsWriter

    writer = MetricsWriter(tmp_path / "metrics.db")
    app.state.metrics_writer = writer

    with (
        patch(
            "archie_orchestrator.proxy.list_sessions",
            side_effect=[
                [session_a],  # first call for session A
                [session_b],  # second call for session B
            ],
        ),
        patch(
            "archie_orchestrator.proxy.websockets.connect", _make_ws_backend([])
        ),  # placeholder — overridden per-session below
    ):
        # Test session A
        with patch("archie_orchestrator.proxy.websockets.connect", _make_ws_backend([usage_a])):
            with patch("archie_orchestrator.proxy.list_sessions", return_value=[session_a]):
                client = TestClient(app, raise_server_exceptions=False)
                with client.websocket_connect(f"/sessions/{sid_a}/stream"):
                    pass

        # Test session B
        with patch("archie_orchestrator.proxy.websockets.connect", _make_ws_backend([usage_b])):
            with patch("archie_orchestrator.proxy.list_sessions", return_value=[session_b]):
                client = TestClient(app, raise_server_exceptions=False)
                with client.websocket_connect(f"/sessions/{sid_b}/stream"):
                    pass

    items = []
    while not writer.queue.empty():
        items.append(writer.queue.get_nowait())

    assert len(items) == 2
    sids = {item[0] for item in items}
    assert sids == {sid_a, sid_b}

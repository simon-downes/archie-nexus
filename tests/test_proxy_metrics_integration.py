"""The WS proxy relays agent frames unchanged and no longer feeds metrics.

Metrics come from tailing the session logs (``MetricsIndex``), so frames are
forwarded without inspection.
"""

import json
from unittest.mock import patch

import pytest
from archie_orchestrator import proxy
from archie_orchestrator.app import app
from archie_shared.schemas import NexusConfig
from archie_shared.session import SessionDescriptor
from starlette.testclient import TestClient


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
    class _BackendIter:
        def __init__(self):
            self._msgs = list(messages)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._msgs:
                raise StopAsyncIteration
            return self._msgs.pop(0)

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


def _llm_request_frame() -> str:
    return json.dumps(
        {
            "type": "llm_request",
            "id": "evt-1",
            "scope": None,
            "turn": 1,
            "iteration": 1,
            "model_key": "m1",
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
    )


def test_llm_request_frame_forwarded_unchanged():
    session = _make_session()
    frame = _llm_request_frame()
    with (
        patch("archie_orchestrator.proxy.list_sessions", return_value=[session]),
        patch("archie_orchestrator.proxy.websockets.connect", _make_ws_backend([frame])),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect(f"/sessions/{session.session_id}/stream") as ws:
            assert ws.receive_text() == frame


def test_proxy_has_no_metrics_feed():
    assert not hasattr(proxy, "_METRICS_MARKERS")
    assert "metrics" not in open(proxy.__file__).read().lower()

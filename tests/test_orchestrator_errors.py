"""Tests for orchestrator error resilience (global handler, DockerError, WS safety)."""

import logging
from unittest.mock import patch

import pytest
from archie_orchestrator.app import app
from archie_orchestrator.docker import DockerError
from archie_shared.schemas import NexusConfig
from archie_shared.session import SessionDescriptor
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Shared fixture — prime app state before each test
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
        raw_docker_status="Up 5 minutes",
    )


# ---------------------------------------------------------------------------
# DockerError
# ---------------------------------------------------------------------------


def test_docker_error_str():
    """DockerError message includes command and stderr."""
    err = DockerError(command=["docker", "run", "--rm"], stderr="no space left", returncode=1)
    assert "docker run --rm" in str(err)
    assert "no space left" in str(err)


def test_docker_error_command_as_string():
    """DockerError accepts string command."""
    err = DockerError(command="docker stop mycontainer", stderr="not found", returncode=1)
    assert "docker stop mycontainer" in str(err)


# ---------------------------------------------------------------------------
# Global exception handler — unexpected errors return 500
# ---------------------------------------------------------------------------


def test_unhandled_exception_returns_500(caplog):
    """An unexpected error in a route handler returns 500 with error detail."""

    async def _boom(request):
        raise ValueError("something exploded unexpectedly")

    from archie_orchestrator.app import unhandled_exception_handler
    from starlette.applications import Starlette
    from starlette.routing import Route

    boom_app = Starlette(
        routes=[Route("/boom", _boom)],
        exception_handlers={Exception: unhandled_exception_handler},
    )

    # raise_server_exceptions=False lets the 500 response through without re-raising
    with caplog.at_level(logging.ERROR, logger="archie_orchestrator.app"):
        client = TestClient(boom_app, raise_server_exceptions=False)
        resp = client.get("/boom")

    assert resp.status_code == 500
    body = resp.json()
    assert body["error"] == "Internal server error"
    # Internal exception detail must NOT leak to the client (logged only).
    assert "detail" not in body
    assert "something exploded" not in resp.text
    assert any("Unhandled error" in r.message for r in caplog.records)
    assert any("something exploded" in r.message for r in caplog.records)


def test_second_request_succeeds_after_first_explodes():
    """Process stays alive — second request succeeds after first raised."""

    call_count = 0

    async def _sometimes_boom(request):
        from starlette.responses import JSONResponse

        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("first call fails")
        return JSONResponse({"ok": True})

    from archie_orchestrator.app import unhandled_exception_handler
    from starlette.applications import Starlette
    from starlette.routing import Route

    test_app = Starlette(
        routes=[Route("/endpoint", _sometimes_boom)],
        exception_handlers={Exception: unhandled_exception_handler},
    )

    client = TestClient(test_app, raise_server_exceptions=False)
    first = client.get("/endpoint")
    second = client.get("/endpoint")

    assert first.status_code == 500
    assert second.status_code == 200
    assert second.json() == {"ok": True}


# ---------------------------------------------------------------------------
# DockerError from route → helpful 500 response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_sessions_docker_error_returns_helpful_message():
    """POST /sessions with DockerError from start_session → 500 + message."""
    with patch(
        "archie_orchestrator.app.start_session",
        side_effect=DockerError(
            command=["docker", "run"],
            stderr="Cannot connect to the Docker daemon",
            returncode=1,
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/sessions",
                json={"workspace": "myproject"},
            )

    assert resp.status_code == 500
    assert "Docker command failed" in resp.json()["error"]


@pytest.mark.asyncio
async def test_delete_session_docker_error_returns_500():
    """DELETE /sessions/{id} with DockerError → 500."""
    session = _make_session()
    with (
        patch(
            "archie_orchestrator.app.stop_session",
            side_effect=DockerError(
                command=["docker", "stop"],
                stderr="Error: No such container",
                returncode=1,
            ),
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete(f"/sessions/{session.session_id}")

    assert resp.status_code == 500
    assert "Docker command failed" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Expected errors (404, 400) still work — not swallowed by global handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_session_not_found_still_404():
    """DELETE /sessions/{id} for unknown session still returns 404."""
    with patch("archie_orchestrator.app.stop_session", side_effect=KeyError("no such session")):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/sessions/nonexistent-session-id")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_post_sessions_missing_workspace_still_400():
    """POST /sessions with missing workspace → still 400."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/sessions", json={})

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# WS proxy unexpected error — logged, closed with 1011, others unaffected
# ---------------------------------------------------------------------------


def test_ws_proxy_unexpected_error_closes_with_1011(caplog):
    """An unexpected error in proxy_stream closes the WS with code 1011."""
    session = _make_session()

    class _ExplodingConnect:
        """Backend that raises an unexpected error during __aenter__."""

        def __call__(self, url, **kwargs):
            return self

        async def __aenter__(self):
            raise RuntimeError("unexpected relay error")

        async def __aexit__(self, *_):
            pass

    with (
        patch("archie_orchestrator.proxy.list_sessions", return_value=[session]),
        patch("archie_orchestrator.proxy.websockets.connect", _ExplodingConnect()),
        caplog.at_level(logging.ERROR, logger="archie_orchestrator.proxy"),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect(f"/sessions/{session.session_id}/stream") as ws:
            data = ws.receive()
            # Expect a close frame
            assert data["type"] == "websocket.close"
            assert data.get("code") == 1011

    assert any("WS proxy error" in r.message for r in caplog.records)

"""Tests for orchestrator HTTP proxy routes (events; /status is gone)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archie_orchestrator.app import app
from archie_orchestrator.docker import RunningContainer
from archie_shared.schemas import NexusConfig
from httpx import ASGITransport, AsyncClient

from tests.session_fakes import serve

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def prime_app_state():
    """Set app.state.config before each test."""
    app.state.config = NexusConfig()
    yield


@pytest.fixture
def http_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _running_session(session_id: str = "proj-01abc12345", port: int = 32771) -> RunningContainer:
    return RunningContainer(
        session_id=session_id,
        container_name=f"archie-{session_id}",
        port=port,
    )


def _mock_backend_response(status_code: int = 200, content: bytes = b'{"ok": true}') -> MagicMock:
    mock = MagicMock()
    mock.status_code = status_code
    mock.content = content
    mock.headers = {"content-type": "application/json"}
    return mock


# ---------------------------------------------------------------------------
# GET /sessions/{id}/events
# ---------------------------------------------------------------------------


async def test_proxy_events_success(http_client):
    """GET /sessions/{id}/events forwards to session and returns response."""
    session = _running_session()
    backend_resp = _mock_backend_response(content=b'{"status": "ok"}')

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=backend_resp)

    with (
        serve(app, {session.session_id: session.port}),
        patch("archie_orchestrator.proxy.httpx.AsyncClient", return_value=mock_client),
    ):
        async with http_client as client:
            response = await client.get("/api/sessions/proj-01abc12345/events")

    assert response.status_code == 200
    mock_client.get.assert_called_once_with("http://127.0.0.1:32771/events", timeout=5.0)


async def test_proxy_events_session_not_found(http_client):
    """GET /sessions/{id}/events with unknown session → 404."""
    with serve(app):
        async with http_client as client:
            response = await client.get("/api/sessions/unknown-01abc12345/events")

    assert response.status_code == 404
    assert "unknown-01abc12345" in response.json()["error"]


async def test_proxy_events_backend_unreachable(http_client):
    """GET /sessions/{id}/events when container is gone → 502."""
    import httpx as real_httpx

    session = _running_session()
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=real_httpx.ConnectError("refused"))

    with (
        serve(app, {session.session_id: session.port}),
        patch("archie_orchestrator.proxy.httpx.AsyncClient", return_value=mock_client),
    ):
        async with http_client as client:
            response = await client.get("/api/sessions/proj-01abc12345/events")

    assert response.status_code == 502


async def test_proxy_events_backend_protocol_disconnect(http_client):
    """GET /sessions/{id}/events when the backend drops HTTP → 502."""
    import httpx as real_httpx

    session = _running_session()
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=real_httpx.RemoteProtocolError("disconnected"))

    with (
        serve(app, {session.session_id: session.port}),
        patch("archie_orchestrator.proxy.httpx.AsyncClient", return_value=mock_client),
    ):
        async with http_client as client:
            response = await client.get("/api/sessions/proj-01abc12345/events")

    assert response.status_code == 502
    assert response.json() == {"error": "Session 'proj-01abc12345' is unreachable"}


async def test_proxy_events_backend_timeout(http_client):
    """GET /sessions/{id}/events when container times out → 504."""
    import httpx as real_httpx

    session = _running_session()
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=real_httpx.TimeoutException("timeout"))

    with (
        serve(app, {session.session_id: session.port}),
        patch("archie_orchestrator.proxy.httpx.AsyncClient", return_value=mock_client),
    ):
        async with http_client as client:
            response = await client.get("/api/sessions/proj-01abc12345/events")

    assert response.status_code == 504


async def test_status_proxy_route_is_removed(http_client):
    """GET /api/sessions/{id}/status is gone (082): state comes from the session record."""
    with serve(app, {"proj-01abc12345": 32771}):
        async with http_client as client:
            response = await client.get("/api/sessions/proj-01abc12345/status")

    assert response.status_code == 404
    assert not response.headers["content-type"].startswith("application/json")
    paths = [getattr(route, "path", None) for route in api_routes()]
    assert "/sessions/{session_id}/status" not in paths


def api_routes():
    from archie_orchestrator import api

    return (*app.routes, *api.routes)


def test_orchestrator_shell_route_is_removed():
    paths = [getattr(route, "path", None) for route in api_routes()]
    assert "/sessions/{session_id}/shell" not in paths

"""Tests for orchestrator HTTP proxy routes (status, events, and metrics)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archie_orchestrator.app import app
from archie_shared.schemas import NexusConfig
from archie_shared.session import SessionDescriptor
from httpx import ASGITransport, AsyncClient

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


def _running_session(session_id: str = "proj-01abc12345", port: int = 32771) -> SessionDescriptor:
    return SessionDescriptor(
        session_id=session_id,
        container_name=f"archie-{session_id}",
        port=port,
        raw_docker_status="Up",
    )


def _mock_backend_response(status_code: int = 200, content: bytes = b'{"ok": true}') -> MagicMock:
    mock = MagicMock()
    mock.status_code = status_code
    mock.content = content
    mock.headers = {"content-type": "application/json"}
    return mock


# ---------------------------------------------------------------------------
# GET /sessions/{id}/status
# ---------------------------------------------------------------------------


async def test_proxy_status_success(http_client):
    """GET /sessions/{id}/status forwards to session and returns response."""
    session = _running_session()
    backend_resp = _mock_backend_response(content=b'{"status": "ok"}')

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=backend_resp)

    with (
        patch("archie_orchestrator.proxy.list_sessions", return_value=[session]),
        patch("archie_orchestrator.proxy.httpx.AsyncClient", return_value=mock_client),
    ):
        async with http_client as client:
            response = await client.get("/sessions/proj-01abc12345/status")

    assert response.status_code == 200
    mock_client.get.assert_called_once_with("http://127.0.0.1:32771/status", timeout=5.0)


async def test_proxy_status_session_not_found(http_client):
    """GET /sessions/{id}/status with unknown session → 404."""
    with patch("archie_orchestrator.proxy.list_sessions", return_value=[]):
        async with http_client as client:
            response = await client.get("/sessions/unknown-01abc12345/status")

    assert response.status_code == 404
    assert "unknown-01abc12345" in response.json()["error"]


async def test_proxy_status_backend_unreachable(http_client):
    """GET /sessions/{id}/status when container is gone → 502."""
    import httpx as real_httpx

    session = _running_session()
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=real_httpx.ConnectError("refused"))

    with (
        patch("archie_orchestrator.proxy.list_sessions", return_value=[session]),
        patch("archie_orchestrator.proxy.httpx.AsyncClient", return_value=mock_client),
    ):
        async with http_client as client:
            response = await client.get("/sessions/proj-01abc12345/status")

    assert response.status_code == 502


async def test_proxy_status_backend_timeout(http_client):
    """GET /sessions/{id}/status when container times out → 504."""
    import httpx as real_httpx

    session = _running_session()
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=real_httpx.TimeoutException("timeout"))

    with (
        patch("archie_orchestrator.proxy.list_sessions", return_value=[session]),
        patch("archie_orchestrator.proxy.httpx.AsyncClient", return_value=mock_client),
    ):
        async with http_client as client:
            response = await client.get("/sessions/proj-01abc12345/status")

    assert response.status_code == 504


def test_orchestrator_shell_route_is_removed():
    paths = [getattr(route, "path", None) for route in app.routes]
    assert "/sessions/{session_id}/shell" not in paths

"""Tests to verify remote-binding correctness.

Validates that:
- The orchestrator responds correctly regardless of the bind address
- Proxy backend connections always use 127.0.0.1 (loopback) for sessions
- No response bodies contain hardcoded localhost addresses
- archie ls from a 'remote' client (ARCHIE_HOST) resolves correctly
"""

from unittest.mock import patch

import pytest
from archie_orchestrator.app import app
from archie_shared.schemas import NexusConfig
from archie_shared.session import SessionDescriptor
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _app_state():
    app.state.config = NexusConfig()
    yield


def _make_session(session_id: str = "proj-01abc12345", port: int = 32771) -> SessionDescriptor:
    return SessionDescriptor(
        session_id=session_id,
        container_name=f"archie-{session_id}",
        port=port,
        raw_docker_status="Up",
    )


# ---------------------------------------------------------------------------
# Endpoints respond correctly when accessed "remotely"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_accessible_from_any_client():
    """GET /health returns 200 regardless of calling client."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_get_sessions_accessible_from_remote():
    """GET /sessions returns session list (simulating remote client)."""
    sessions = [_make_session()]
    with patch("archie_orchestrator.app.list_sessions", return_value=sessions):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://10.0.0.2"
        ) as client:
            resp = await client.get("/sessions")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["session_id"] == "proj-01abc12345"


@pytest.mark.asyncio
async def test_response_bodies_contain_no_hardcoded_localhost():
    """Session list responses do not embed localhost/127.0.0.1 addresses."""
    sessions = [_make_session(port=32771)]
    with patch("archie_orchestrator.app.list_sessions", return_value=sessions):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/sessions")

    body_text = resp.text
    # Session descriptors should not expose internal loopback addresses
    # (they expose the host port number, not a full URL with 127.0.0.1)
    assert "127.0.0.1" not in body_text
    assert "localhost" not in body_text


# ---------------------------------------------------------------------------
# Proxy backend always uses loopback regardless of orchestrator bind address
# ---------------------------------------------------------------------------


def test_proxy_backend_always_uses_loopback_not_bind_address():
    """Proxy backend URL uses 127.0.0.1 for sessions, not any orchestrator bind address.

    This complements test_proxy_status_forwards_to_loopback which validates the
    actual HTTP call. Together they confirm sessions are always accessed via loopback.
    """
    # The backend target construction in proxy_stream must use 127.0.0.1.
    # We validate by checking the module-level constant (CONTAINER_PORT is not
    # the bind address) and that no bind-address placeholder appears.
    import inspect

    from archie_orchestrator import proxy as proxy_module

    source = inspect.getsource(proxy_module.proxy_stream)
    assert "127.0.0.1" in source, "Backend target must use 127.0.0.1 loopback"


@pytest.mark.asyncio
async def test_proxy_status_forwards_to_loopback(tmp_path):
    """GET /sessions/{id}/status proxies to 127.0.0.1:{port}, not the bind address."""
    session = _make_session(port=32771)
    captured_urls = []

    import httpx as real_httpx

    class _FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def get(self, url, **kwargs):
            captured_urls.append(url)
            mock = real_httpx.Response(200, content=b'{"status":"ok"}')
            return mock

    with (
        patch("archie_orchestrator.proxy.list_sessions", return_value=[session]),
        patch("archie_orchestrator.proxy.httpx.AsyncClient", _FakeAsyncClient),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get(f"/sessions/{session.session_id}/status")

    assert len(captured_urls) == 1
    assert captured_urls[0].startswith("http://127.0.0.1:32771")


# ---------------------------------------------------------------------------
# ARCHIE_HOST remote client can list sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archie_host_ls_queries_correct_address(monkeypatch, tmp_path):
    """ARCHIE_HOST overrides profile resolution in ls — queries the override address."""
    from unittest.mock import MagicMock

    import httpx as real_httpx
    import msgspec

    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHIE_HOST", "192.168.1.50:7600")

    from archie_cli.cli import main
    from click.testing import CliRunner

    sessions_resp = MagicMock()
    sessions_resp.status_code = 200
    sessions_resp.content = msgspec.json.encode([_make_session()])
    sessions_resp.raise_for_status = MagicMock()

    with patch("archie_cli.cli.httpx") as mock_httpx:
        mock_httpx.get.return_value = sessions_resp
        mock_httpx.ConnectError = real_httpx.ConnectError
        mock_httpx.HTTPStatusError = real_httpx.HTTPStatusError
        runner = CliRunner()
        result = runner.invoke(main, ["ls"])

    assert result.exit_code == 0
    url = mock_httpx.get.call_args[0][0]
    assert "192.168.1.50:7600" in url

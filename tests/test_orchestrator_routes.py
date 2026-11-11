"""Route layout: JSON under API_PREFIX, pages at the root, no old paths."""

from urllib.parse import parse_qs, urlparse

import pytest
from archie_orchestrator import api, pages
from archie_orchestrator.app import app
from archie_orchestrator.auth import AuthService
from archie_shared.plugins.config import PluginAuthConfig, PluginConfig
from archie_shared.protocol import API_PREFIX
from archie_shared.schemas import NexusConfig
from httpx import ASGITransport, AsyncClient
from starlette.routing import Mount

from tests.session_fakes import serve
from tests.test_orchestrator_auth_oauth import FakeTransport

GOOGLE = NexusConfig(plugins={"google": PluginConfig(auth=PluginAuthConfig(client_id="client"))})


@pytest.fixture(autouse=True)
def _auth_state(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    monkeypatch.setattr(app.state, "config", GOOGLE, raising=False)
    service = AuthService(GOOGLE, transport=FakeTransport())
    monkeypatch.setattr(app.state, "auth_service", service, raising=False)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_api_prefix_is_api():
    assert API_PREFIX == "/api"


def test_json_routes_are_mounted_at_api_prefix():
    mounts = [r for r in app.routes if isinstance(r, Mount) and r.path == API_PREFIX]
    assert len(mounts) == 1
    assert mounts[0].routes == api.routes


def test_root_routes_are_pages_health_api_and_static():
    n = len(pages.routes)
    assert app.routes[:n] == pages.routes
    assert [r.path for r in app.routes[n:]] == ["/health", API_PREFIX, "/static"]
    assert not [r.path for r in app.routes if r.path.startswith("/ui")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/metrics"),
        ("GET", "/auth/status"),
        ("GET", "/auth/providers"),
        ("GET", "/auth/flow/x"),
        ("POST", "/auth/login/google"),
        ("POST", "/auth/refresh/google"),
        ("PUT", "/auth/credential/github"),
        ("POST", "/sessions"),
        ("DELETE", "/sessions/x"),
        ("GET", "/sessions/x/status"),
        ("GET", "/sessions/x/events"),
        ("GET", "/sessions/x/metrics"),
    ],
)
async def test_former_root_json_routes_are_gone(method, path):
    """Starlette's no-route answer (plain text), not a handler's JSON 404."""
    async with _client() as http:
        response = await http.request(method, path)
    assert response.status_code in (404, 405)
    assert not response.headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_api_refresh_reaches_handler():
    async with _client() as http:
        response = await http.post(f"{API_PREFIX}/auth/refresh/google")
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert "error" in response.json()


@pytest.mark.asyncio
async def test_api_flow_reaches_handler():
    async with _client() as http:
        response = await http.get(f"{API_PREFIX}/auth/flow/unknown")
    assert response.status_code == 400
    assert response.json() == {"error": "Unknown or expired OAuth flow"}


@pytest.mark.asyncio
async def test_api_login_redirects_to_root_callback():
    async with _client() as http:
        response = await http.post(f"{API_PREFIX}/auth/login/google")
    assert response.status_code == 200
    assert response.json()["redirect_uri"] == "http://test/auth/callback/google"


@pytest.mark.asyncio
async def test_login_then_root_callback_completes_flow():
    async with _client() as http:
        login = (await http.post(f"{API_PREFIX}/auth/login/google")).json()
        state = parse_qs(urlparse(login["authorization_url"]).query)["state"][0]
        callback = await http.get("/auth/callback/google", params={"state": state, "code": "code"})
        flow = await http.get(f"{API_PREFIX}/auth/flow/{login['flow_id']}")
    assert callback.status_code == 200
    assert callback.headers["content-type"].startswith("text/html")
    assert "Authentication successful" in callback.text
    assert flow.json()["status"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/ui/auth",
        "/ui/sessions",
        "/ui/sessions/proj-01k0000000",
        "/ui/sessions/proj-01k0000000/live",
        "/ui/insights",
        "/ui/insights/sessions",
        "/ui/insights/sessions/proj-01k0000000",
        "/ui/insights/models",
        "/ui/insights/tools",
        "/ui/insights/subagents",
        "/ui/insights/errors",
    ],
)
async def test_former_ui_pages_are_gone(path):
    async with _client() as http:
        response = await http.get(path)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_sessions_root_path_is_the_log_viewer_page():
    with serve(app):
        async with _client() as http:
            response = await http.get("/sessions")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.asyncio
async def test_root_callback_rejects_unknown_state_with_html():
    async with _client() as http:
        response = await http.get("/auth/callback/google", params={"state": "bad", "code": "x"})
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert "Invalid or expired flow" in response.text

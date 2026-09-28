from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from archie_orchestrator.app import app
from archie_shared.credentials.api import CredentialStatus
from archie_shared.schemas import NexusConfig
from httpx import ASGITransport, AsyncClient


@pytest.fixture(autouse=True)
def _app_state():
    app.state.config = NexusConfig()
    app.state.start_time = datetime.now(UTC)
    app.state.auth_service = SimpleNamespace(
        provider_names=lambda: ["google", "github"],
        status=lambda name: CredentialStatus(
            provider=name,
            auth_type="oauth" if name == "google" else "static",
            configured=name == "google",
            state="expired" if name == "google" else "configured",
            expires_at="2026-01-01T00:00:00+00:00" if name == "google" else None,
            error=None,
        ),
    )
    yield


@pytest.mark.asyncio
async def test_auth_status_browser_renders_html_and_actions():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ui/auth")

    assert response.status_code == 200
    assert "Authentication status" in response.text
    assert "OAuth credentials" in response.text
    assert "Static credentials" in response.text
    assert "data-action=\"refresh\"" in response.text
    assert "data-action=\"login\"" in response.text
    assert "2026-01-01 00:00:00 UTC" in response.text
    assert "access_token" not in response.text


@pytest.mark.asyncio
async def test_auth_status_endpoint_remains_json():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/auth/status")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()[0]["provider"] == "google"

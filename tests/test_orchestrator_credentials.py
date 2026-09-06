"""Tests for orchestrator auth provider and credential endpoints."""

import pytest
from archie_orchestrator.app import app
from archie_orchestrator.auth import AuthService
from archie_shared.schemas import NexusConfig
from httpx import ASGITransport, AsyncClient


@pytest.fixture(autouse=True)
def _app_state(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    app.state.config = NexusConfig()
    app.state.auth_service = AuthService(app.state.config)


async def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_post_credentials_route_removed():
    async with await client() as http:
        response = await http.post("/credentials", content=b"bedrock: {}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_provider_discovery_is_complete_and_secret_free():
    async with await client() as http:
        response = await http.get("/auth/providers")
    assert response.status_code == 200
    providers = {item["name"]: item for item in response.json()}
    assert set(providers) == {"notion", "linear", "slack", "github", "aws", "scalr", "jira", "google", "bedrock"}
    assert providers["bedrock"]["fields"] == [
        "aws_access_key_id", "aws_secret_access_key", "aws_session_token"
    ]
    assert "client_secret" not in response.text


@pytest.mark.asyncio
async def test_static_credential_replacement_and_deletion_preserve_other_entries(tmp_path):
    async with await client() as http:
        response = await http.put("/auth/credential/github", json={"token": "secret"})
        assert response.status_code == 200
        assert response.json()["state"] == "configured"
        response = await http.put("/auth/credential/linear", json={"token": "other"})
        assert response.status_code == 200
        response = await http.put("/auth/credential/github", json={"token": "new"})
        assert response.status_code == 200
        response = await http.delete("/auth/credential/github")
        assert response.status_code == 200
        assert response.json()["state"] == "missing"

    assert (tmp_path / "credentials.yaml").read_text() == "linear:\n  token: other\n"


@pytest.mark.asyncio
async def test_static_credential_validation_does_not_modify_existing():
    async with await client() as http:
        assert (await http.put("/auth/credential/github", json={"token": "keep"})).status_code == 200
        response = await http.put("/auth/credential/github", json={"token": "new", "extra": "bad"})
    assert response.status_code == 400
    assert "keep" in (await _read_store()).read_text()


@pytest.mark.asyncio
async def test_unknown_and_oauth_static_updates_are_actionable():
    async with await client() as http:
        assert (await http.put("/auth/credential/nope", json={})).status_code == 404
        response = await http.put("/auth/credential/google", json={"access_token": "x"})
    assert response.status_code == 400
    assert "OAuth" in response.json()["error"]


async def _read_store():
    import os
    from pathlib import Path

    return Path(os.environ["ARCHIE_HOME_DIR"]) / "credentials.yaml"

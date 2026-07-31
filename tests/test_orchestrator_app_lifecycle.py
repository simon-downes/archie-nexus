"""Tests for orchestrator app.py — POST /sessions and DELETE /sessions/{id}."""

from unittest.mock import patch

import msgspec
import pytest
from archie_orchestrator.app import app
from archie_shared.schemas import NexusConfig
from archie_shared.session import SessionDescriptor
from httpx import ASGITransport, AsyncClient


@pytest.fixture(autouse=True)
def prime_app_state():
    """Set app.state.config before each test (lifespan not triggered by ASGITransport)."""
    app.state.config = NexusConfig()
    yield


@pytest.fixture
def http_client():
    """Return an async HTTPX test client for the orchestrator app."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# POST /sessions
# ---------------------------------------------------------------------------


async def test_post_sessions_success(http_client):
    """POST /sessions with valid workspace → 200 + SessionDescriptor."""
    fake_descriptor = SessionDescriptor(
        session_id="myproject-01abc12345",
        container_name="archie-myproject-01abc12345",
        port=32771,
        raw_docker_status="Up",
    )

    with patch("archie_orchestrator.app.start_session", return_value=fake_descriptor):
        async with http_client as client:
            response = await client.post("/sessions", json={"workspace": "myproject"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"

    decoded = msgspec.json.decode(response.content, type=SessionDescriptor)
    assert decoded.session_id == "myproject-01abc12345"
    assert decoded.port == 32771


async def test_post_sessions_missing_workspace_key(http_client):
    """POST /sessions with no workspace key → 400."""
    async with http_client as client:
        response = await client.post("/sessions", json={"other": "value"})

    assert response.status_code == 400
    assert "workspace" in response.json()["error"].lower()


async def test_post_sessions_workspace_not_found(http_client):
    """POST /sessions when workspace dir missing → 400 from ValueError."""
    with patch(
        "archie_orchestrator.app.start_session",
        side_effect=ValueError("Workspace 'bad' not found"),
    ):
        async with http_client as client:
            response = await client.post("/sessions", json={"workspace": "bad"})

    assert response.status_code == 400
    assert "not found" in response.json()["error"].lower()


async def test_post_sessions_docker_failure(http_client):
    """POST /sessions when Docker fails → 500 from RuntimeError."""
    with patch(
        "archie_orchestrator.app.start_session",
        side_effect=RuntimeError("docker run failed"),
    ):
        async with http_client as client:
            response = await client.post("/sessions", json={"workspace": "myproject"})

    assert response.status_code == 500
    assert "docker run failed" in response.json()["error"]


async def test_post_sessions_invalid_json(http_client):
    """POST /sessions with non-JSON body → 400."""
    async with http_client as client:
        response = await client.post(
            "/sessions",
            content=b"not json",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /sessions/{session_id}
# ---------------------------------------------------------------------------


async def test_delete_session_success(http_client):
    """DELETE /sessions/{id} for existing session → 200 + {"stopped": id}."""
    with patch("archie_orchestrator.app.stop_session"):
        async with http_client as client:
            response = await client.delete("/sessions/myproject-01abc12345")

    assert response.status_code == 200
    assert response.json() == {"stopped": "myproject-01abc12345"}


async def test_delete_session_not_found(http_client):
    """DELETE /sessions/{id} for non-existent session → 404."""
    with patch(
        "archie_orchestrator.app.stop_session",
        side_effect=KeyError("myproject-01abc12345"),
    ):
        async with http_client as client:
            response = await client.delete("/sessions/myproject-01abc12345")

    assert response.status_code == 404
    assert "myproject-01abc12345" in response.json()["error"]

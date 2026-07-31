"""Tests for archie_orchestrator.app — Starlette HTTP endpoints."""

from unittest.mock import patch

import msgspec
import pytest
from archie_orchestrator.app import app
from archie_shared.session import SessionDescriptor
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def http_client():
    """Return an async HTTPX test client for the orchestrator app."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- GET /health ---


async def test_health_returns_200(http_client):
    """GET /health → 200 with {"status": "ok"}."""
    async with http_client as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- GET /sessions ---


async def test_sessions_with_running_containers(http_client):
    """GET /sessions with patched docker → serialized SessionDescriptors."""
    fake_sessions = [
        SessionDescriptor(
            session_id="myproject-01abc12345",
            container_name="archie-myproject-01abc12345",
            port=32771,
            raw_docker_status="Up 3 hours",
        ),
        SessionDescriptor(
            session_id="otherapp-01def67890",
            container_name="archie-otherapp-01def67890",
            port=None,
            raw_docker_status="Up 10 minutes",
        ),
    ]

    with patch("archie_orchestrator.app.list_sessions", return_value=fake_sessions):
        async with http_client as client:
            response = await client.get("/sessions")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"

    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 2

    first = data[0]
    assert first["session_id"] == "myproject-01abc12345"
    assert first["container_name"] == "archie-myproject-01abc12345"
    assert first["port"] == 32771
    assert first["raw_docker_status"] == "Up 3 hours"

    second = data[1]
    assert second["session_id"] == "otherapp-01def67890"
    assert second["port"] is None


async def test_sessions_empty(http_client):
    """GET /sessions with no containers → empty JSON array."""
    with patch("archie_orchestrator.app.list_sessions", return_value=[]):
        async with http_client as client:
            response = await client.get("/sessions")

    assert response.status_code == 200
    assert response.json() == []


async def test_sessions_response_is_msgspec_encoded(http_client):
    """Response body is decodable via msgspec as list[SessionDescriptor]."""
    fake_sessions = [
        SessionDescriptor(
            session_id="test-01zzz00001",
            container_name="archie-test-01zzz00001",
            port=9999,
            raw_docker_status="Up 1 second",
        )
    ]

    with patch("archie_orchestrator.app.list_sessions", return_value=fake_sessions):
        async with http_client as client:
            response = await client.get("/sessions")

    decoded = msgspec.json.decode(response.content, type=list[SessionDescriptor])
    assert len(decoded) == 1
    assert decoded[0].session_id == "test-01zzz00001"
    assert decoded[0].port == 9999

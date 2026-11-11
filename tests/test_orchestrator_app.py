"""Tests for archie_orchestrator.app — Starlette HTTP endpoints."""

import msgspec
import pytest
from archie_orchestrator.app import app
from archie_shared.session import SessionDescriptor
from httpx import ASGITransport, AsyncClient

from tests.session_fakes import record, serve


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
    """GET /sessions → the tracker's live records as serialized SessionDescriptors."""
    records = [
        record("myproject-01abc12345", status="working", started_at="2026-07-02T00:00:00+00:00"),
        record("otherapp-01def67890", status="starting", started_at="2026-07-01T00:00:00+00:00"),
    ]

    with serve(app, {"myproject-01abc12345": 32771, "otherapp-01def67890": None}, records=records):
        async with http_client as client:
            response = await client.get("/api/sessions")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"

    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 2

    first = data[0]
    assert first["session_id"] == "myproject-01abc12345"
    assert first["container_name"] == "archie-myproject-01abc12345"
    assert first["status"] == "working"
    assert "port" not in first and "raw_docker_status" not in first

    second = data[1]
    assert second["session_id"] == "otherapp-01def67890"
    assert second["status"] == "starting"


async def test_sessions_empty(http_client):
    """GET /sessions with no sessions → empty JSON array."""
    with serve(app):
        async with http_client as client:
            response = await client.get("/api/sessions")

    assert response.status_code == 200
    assert response.json() == []


async def test_sessions_response_is_msgspec_encoded(http_client):
    """Response body is decodable via msgspec as list[SessionDescriptor]."""
    with serve(app, {"test-01zzz00001": 9999}):
        async with http_client as client:
            response = await client.get("/api/sessions")

    decoded = msgspec.json.decode(response.content, type=list[SessionDescriptor])
    assert len(decoded) == 1
    assert decoded[0].session_id == "test-01zzz00001"
    assert decoded[0].status == "idle"

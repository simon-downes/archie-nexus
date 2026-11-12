"""Tests for orchestrator app.py — POST /sessions and DELETE /sessions/{id}."""

import msgspec
import pytest
from archie_orchestrator.app import app
from archie_shared.schemas import NexusConfig
from archie_shared.session import SessionDescriptor
from httpx import ASGITransport, AsyncClient

from tests.session_fakes import FakeRuntime, serve


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
    """POST /sessions with valid workspace → 202 + the starting SessionDescriptor."""
    runtime = FakeRuntime(run_error=RuntimeError("not started in this test"))
    with serve(app, runtime=runtime) as tracker:
        async with http_client as client:
            response = await client.post("/api/sessions", json={"workspace": "myproject"})
        await tracker.close()

    assert response.status_code == 202
    assert response.headers["content-type"] == "application/json"

    decoded = msgspec.json.decode(response.content, type=SessionDescriptor)
    assert decoded.session_id.startswith("myproject-")
    assert decoded.status == "starting"
    assert decoded.container_name == f"archie-{decoded.session_id}"


async def test_post_sessions_missing_workspace_key(http_client):
    """POST /sessions with no workspace key → 400."""
    async with http_client as client:
        response = await client.post("/api/sessions", json={"other": "value"})

    assert response.status_code == 400
    assert "workspace" in response.json()["error"].lower()


async def test_post_sessions_workspace_not_found(http_client):
    """POST /sessions when workspace dir missing → 400 from ValueError."""
    with serve(app, runtime=FakeRuntime(prepare_error=ValueError("Workspace 'bad' not found"))):
        async with http_client as client:
            response = await client.post("/api/sessions", json={"workspace": "bad"})

    assert response.status_code == 400
    assert "not found" in response.json()["error"].lower()


async def test_post_sessions_docker_failure(http_client):
    """POST /sessions when the image check fails → 500 from RuntimeError."""
    with serve(app, runtime=FakeRuntime(prepare_error=RuntimeError("docker run failed"))):
        async with http_client as client:
            response = await client.post("/api/sessions", json={"workspace": "myproject"})

    assert response.status_code == 500
    assert "docker run failed" in response.json()["error"]


async def test_post_sessions_invalid_json(http_client):
    """POST /sessions with non-JSON body → 400."""
    async with http_client as client:
        response = await client.post(
            "/api/sessions",
            content=b"not json",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /sessions/{session_id}
# ---------------------------------------------------------------------------


async def test_delete_session_success(http_client):
    """DELETE /sessions/{id} for a running session → 200 + the stopped SessionDescriptor."""
    runtime = FakeRuntime()
    with serve(app, {"myproject-01abc12345": 32771}, runtime=runtime):
        async with http_client as client:
            response = await client.delete("/api/sessions/myproject-01abc12345")

    assert response.status_code == 200
    assert response.json()["session_id"] == "myproject-01abc12345"
    assert response.json()["status"] == "stopped"
    assert runtime.stops == ["myproject-01abc12345"]


async def test_delete_session_not_found(http_client):
    """DELETE /sessions/{id} for an unknown session → 404."""
    with serve(app):
        async with http_client as client:
            response = await client.delete("/api/sessions/myproject-01abc12345")

    assert response.status_code == 404
    assert "myproject-01abc12345" in response.json()["error"]


async def test_post_sessions_kiro_backend(http_client):
    """POST /sessions {"backend": "kiro"} passes the backend to the start and the record."""
    runtime = FakeRuntime(run_error=RuntimeError("x"))
    with serve(app, runtime=runtime) as tracker:
        async with http_client as client:
            response = await client.post(
                "/api/sessions", json={"workspace": "p", "backend": "kiro"}
            )
        await tracker.close()
    assert response.status_code == 202
    assert response.json()["backend"] == "kiro"
    assert runtime.prepared == [("p", "kiro")]


async def test_post_sessions_default_backend_is_native(http_client):
    runtime = FakeRuntime(run_error=RuntimeError("x"))
    with serve(app, runtime=runtime) as tracker:
        async with http_client as client:
            response = await client.post("/api/sessions", json={"workspace": "p"})
        await tracker.close()
    assert response.json()["backend"] == "native"
    assert runtime.prepared == [("p", "native")]


async def test_post_sessions_invalid_backend(http_client):
    async with http_client as client:
        response = await client.post("/api/sessions", json={"workspace": "p", "backend": "x"})
    assert response.status_code == 400
    assert response.json() == {"error": "Invalid backend 'x': expected 'native' or 'kiro'"}

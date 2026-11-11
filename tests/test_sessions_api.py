"""Tests for the session record API: GET/POST /api/sessions and GET /api/sessions/{id} (082)."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import msgspec
import pytest
from archie_orchestrator.app import app
from archie_orchestrator.store import db_path
from archie_orchestrator.tracker import DockerRuntime
from archie_shared.config import home_dir
from archie_shared.schemas import NexusConfig
from archie_shared.session import SessionDescriptor
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from tests.session_fakes import (
    PORT,
    SID,
    FakeAgent,
    FakeRuntime,
    make_tracker,
    record,
    serve,
    status_frame,
    use_tracker,
)

ENDED = "proj-01kx77jkz8"


@pytest.fixture(autouse=True)
def _app_state():
    app.state.config = NexusConfig()
    app.state.start_time = datetime.now(UTC)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _records():
    return [
        record(SID, status="working", started_at="2026-07-02T10:00:00+00:00"),
        record(
            ENDED,
            status="stopped",
            started_at="2026-07-01T10:00:00+00:00",
            ended_at="2026-07-01T11:00:00+00:00",
        ),
    ]


async def until(predicate, timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# GET /api/sessions and /api/sessions/{id}
# ---------------------------------------------------------------------------


async def test_list_returns_sessions_that_have_not_ended():
    with serve(app, records=_records()):
        async with _client() as http:
            response = await http.get("/api/sessions")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    [item] = msgspec.json.decode(response.content, type=list[SessionDescriptor])
    assert item == _records()[0]
    assert response.json()[0]["container_name"] == f"archie-{SID}"


async def test_list_include_ended_returns_every_record_newest_first():
    with serve(app, records=_records()):
        async with _client() as http:
            response = await http.get("/api/sessions", params={"include": "ended"})
    assert response.status_code == 200
    body = response.json()
    assert [(s["session_id"], s["status"]) for s in body] == [
        (SID, "working"),
        (ENDED, "stopped"),
    ]
    assert body[1]["container_name"] is None
    assert body[1]["ended_at"] == "2026-07-01T11:00:00+00:00"


async def test_list_empty_and_bad_include():
    with serve(app):
        async with _client() as http:
            empty = await http.get("/api/sessions")
            bad = await http.get("/api/sessions", params={"include": "all"})
    assert empty.json() == []
    assert bad.status_code == 400
    assert "include" in bad.json()["error"]


async def test_list_include_ended_store_failure_is_500(monkeypatch):
    with serve(app) as tracker:
        monkeypatch.setattr(
            tracker.store,
            "list",
            lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
        )
        async with _client() as http:
            response = await http.get("/api/sessions", params={"include": "ended"})
    assert response.status_code == 500


async def test_get_one_live_ended_and_unknown():
    with serve(app, records=_records()):
        async with _client() as http:
            live = await http.get(f"/api/sessions/{SID}")
            ended = await http.get(f"/api/sessions/{ENDED}")
            unknown = await http.get("/api/sessions/nope-01kx77jkz8")
    assert live.status_code == 200
    assert live.json()["status"] == "working"
    assert ended.status_code == 200
    assert ended.json()["status"] == "stopped"
    assert unknown.status_code == 404
    assert unknown.json() == {"error": "No session with ID 'nope-01kx77jkz8'"}


# ---------------------------------------------------------------------------
# POST /api/sessions
# ---------------------------------------------------------------------------


async def test_post_returns_202_starting_and_the_session_reaches_idle():
    runtime = FakeRuntime(port=40002)
    agent = FakeAgent([status_frame("idle", model_key="m1"), 30.0])
    with (
        serve(app, runtime=runtime) as tracker,
        patch("archie_orchestrator.tracker.websockets.connect", agent.connect()),
    ):
        async with _client() as http:
            response = await http.post("/api/sessions", json={"workspace": "proj"})
            assert response.status_code == 202
            created = msgspec.json.decode(response.content, type=SessionDescriptor)
            assert created.status == "starting"
            assert (created.workspace, created.backend, created.name_fixed) == (
                "proj",
                "native",
                False,
            )
            await until(lambda: tracker.live(created.session_id).status == "idle")
            listed = (await http.get("/api/sessions")).json()
            one = (await http.get(f"/api/sessions/{created.session_id}")).json()
        await tracker.close()

    assert [s["session_id"] for s in listed] == [created.session_id]
    assert one["status"] == "idle"
    assert one["model_key"] == "m1"
    conn = sqlite3.connect(db_path(home_dir()))
    try:
        statuses = conn.execute("SELECT status FROM sessions").fetchall()
    finally:
        conn.close()
    assert statuses == [("idle",)]


async def test_post_with_name_and_backend():
    runtime = FakeRuntime(run_error=RuntimeError("not in this test"))
    with serve(app, runtime=runtime) as tracker:
        async with _client() as http:
            response = await http.post(
                "/api/sessions",
                json={"workspace": "proj", "backend": "kiro", "name": "  Review PR  "},
            )
        await tracker.close()
    assert response.status_code == 202
    body = response.json()
    assert (body["name"], body["name_fixed"], body["backend"]) == ("Review PR", True, "kiro")
    assert runtime.prepared == [("proj", "kiro")]


async def test_post_null_name_is_the_default():
    with serve(app, runtime=FakeRuntime(run_error=RuntimeError("x"))) as tracker:
        async with _client() as http:
            response = await http.post("/api/sessions", json={"workspace": "proj", "name": None})
        await tracker.close()
    body = response.json()
    assert response.status_code == 202
    assert (body["name"], body["name_fixed"]) == (body["session_id"], False)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"not json", "Invalid JSON body"),
        (b"[1, 2]", "Invalid JSON body"),
        ({"other": "value"}, "workspace"),
        ({"workspace": 3}, "workspace"),
        ({"workspace": ""}, "workspace"),
        ({"workspace": "p", "backend": "x"}, "Invalid backend 'x'"),
        ({"workspace": "p", "name": 5}, "Invalid 'name'"),
        ({"workspace": "p", "name": "   "}, "Invalid 'name'"),
        ({"workspace": "p", "name": "x" * 81}, "Invalid 'name'"),
    ],
    ids=[
        "not-json",
        "not-object",
        "no-workspace",
        "workspace-not-string",
        "workspace-empty",
        "bad-backend",
        "name-not-string",
        "name-blank",
        "name-too-long",
    ],
)
async def test_post_bad_body_is_400_and_starts_nothing(payload, message):
    runtime = FakeRuntime()
    with serve(app, runtime=runtime):
        async with _client() as http:
            if isinstance(payload, bytes):
                response = await http.post(
                    "/api/sessions", content=payload, headers={"content-type": "application/json"}
                )
            else:
                response = await http.post("/api/sessions", json=payload)
    assert response.status_code == 400
    assert message in response.json()["error"]
    assert runtime.prepared == []


async def test_post_name_of_80_characters_is_accepted():
    with serve(app, runtime=FakeRuntime(run_error=RuntimeError("x"))) as tracker:
        async with _client() as http:
            response = await http.post("/api/sessions", json={"workspace": "p", "name": "x" * 80})
        await tracker.close()
    assert response.status_code == 202


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ValueError("Workspace 'bad' not found"), 400),
        (RuntimeError("Image 'archie:latest' not found locally."), 500),
    ],
)
async def test_post_prepare_errors_keep_their_status(error, status):
    with serve(app, runtime=FakeRuntime(prepare_error=error)) as tracker:
        async with _client() as http:
            response = await http.post("/api/sessions", json={"workspace": "bad"})
    assert response.status_code == status
    assert response.json() == {"error": str(error)}
    assert await tracker.list(include_ended=True) == []


async def test_post_insert_failure_is_500_and_starts_nothing(monkeypatch):
    runtime = FakeRuntime()
    with serve(app, runtime=runtime) as tracker:
        monkeypatch.setattr(
            tracker.store,
            "insert",
            lambda r: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")),
        )
        async with _client() as http:
            response = await http.post("/api/sessions", json={"workspace": "proj"})
    assert response.status_code == 500
    assert runtime.runs == []


async def test_post_start_failure_is_visible_as_failed():
    runtime = FakeRuntime(run_error=RuntimeError("Container did not become ready within 30s"))
    with serve(app, runtime=runtime) as tracker:
        async with _client() as http:
            created = (await http.post("/api/sessions", json={"workspace": "proj"})).json()
            sid = created["session_id"]
            await until(lambda: tracker.live(sid) is None)
            one = (await http.get(f"/api/sessions/{sid}")).json()
            listed = (await http.get("/api/sessions")).json()
    assert one["status"] == "failed"
    assert "did not become ready" in one["error"]
    assert listed == []


# ---------------------------------------------------------------------------
# Read and proxy paths never call Docker
# ---------------------------------------------------------------------------


@pytest.fixture
def no_docker(session_logs):
    """A tracker on the real DockerRuntime with every Docker subprocess failing."""
    tracker = make_tracker({SID: PORT}, runtime=DockerRuntime())
    boom = AssertionError("Docker must not be called")
    with (
        use_tracker(app, tracker),
        patch("archie_orchestrator.docker.subprocess.run", side_effect=boom) as run,
    ):
        yield tracker
    run.assert_not_called()


async def test_read_routes_and_pages_do_not_call_docker(no_docker, session_logs):
    w = session_logs
    w.write(SID, [w.user("hello")])
    async with _client() as http:
        responses = [
            await http.get("/api/sessions"),
            await http.get(f"/api/sessions/{SID}"),
            await http.get("/"),
            await http.get("/sessions"),
            await http.get(f"/sessions/{SID}"),
        ]
    assert [r.status_code for r in responses] == [200] * 5
    assert SID in responses[2].text


async def test_events_proxy_does_not_call_docker(no_docker):
    backend = MagicMock(status_code=200, content=b"", headers={"content-type": "text/plain"})
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=backend)
    with patch("archie_orchestrator.proxy.httpx.AsyncClient", return_value=client):
        async with _client() as http:
            response = await http.get(f"/api/sessions/{SID}/events", params={"after": "e1"})
    assert response.status_code == 200
    client.get.assert_called_once_with(f"http://127.0.0.1:{PORT}/events?after=e1", timeout=5.0)


def test_stream_proxy_does_not_call_docker(no_docker):
    class Backend:
        async def send(self, msg):
            pass

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            yield "frame"

    urls = []

    @asynccontextmanager
    async def connect(url, **kwargs):
        urls.append(url)
        yield Backend()

    with patch("archie_orchestrator.proxy.websockets.connect", connect):
        client = TestClient(app, raise_server_exceptions=False)
        with client.websocket_connect(f"/api/sessions/{SID}/stream") as ws:
            assert ws.receive_text() == "frame"
    assert urls == [f"ws://127.0.0.1:{PORT}/stream"]


async def test_live_viewer_does_not_call_docker(no_docker, session_logs):
    w = session_logs
    w.write(SID, [w.user("hello")])
    agent = FakeAgent([status_frame("idle")])
    with patch("archie_orchestrator.web.websockets.connect", agent.connect()):
        async with _client() as http:
            response = await http.get(f"/sessions/{SID}/live")
    assert response.status_code == 200
    assert agent.urls == [f"ws://127.0.0.1:{PORT}/stream"]


def test_starting_session_without_port_is_not_ready():
    with serve(app, {SID: None}):
        client = TestClient(app, raise_server_exceptions=False)
        events = client.get(f"/api/sessions/{SID}/events")
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(f"/api/sessions/{SID}/stream"):
                pass
    assert events.status_code == 503
    assert exc_info.value.code == 4003

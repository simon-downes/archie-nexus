"""Tests for the session record API: list, get, create, stop, patch and events (082)."""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import msgspec
import pytest
from archie_orchestrator.app import app
from archie_orchestrator.docker import DockerError
from archie_orchestrator.store import db_path
from archie_orchestrator.tracker import DockerRuntime
from archie_shared.config import home_dir
from archie_shared.events import SessionStarted, encode_event
from archie_shared.schemas import NexusConfig
from archie_shared.session import SessionDescriptor
from archie_shared.session.log import SessionLog
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
EMOJI_NAME = "\U0001f469\u200d\U0001f4bb Review \u00e9"  # woman technologist (a ZWJ sequence)


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
            assert (created.workspace, created.backend, created.name_source) == (
                "proj",
                "native",
                "id",
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
    assert (body["name"], body["name_source"], body["backend"]) == ("Review PR", "user", "kiro")
    assert runtime.prepared == [("proj", "kiro")]


async def test_post_null_name_is_the_default():
    with serve(app, runtime=FakeRuntime(run_error=RuntimeError("x"))) as tracker:
        async with _client() as http:
            response = await http.post("/api/sessions", json={"workspace": "proj", "name": None})
        await tracker.close()
    body = response.json()
    assert response.status_code == 202
    assert (body["name"], body["name_source"]) == (body["session_id"], "id")


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
        ({"workspace": "p", "name": "Review\nPR"}, "Invalid 'name'"),
        ({"workspace": "p", "name": "Review\tPR"}, "Invalid 'name'"),
        ({"workspace": "p", "name": "Review \x1b[31mPR"}, "Invalid 'name'"),
        ({"workspace": "p", "name": "Review PR\n"}, "Invalid 'name'"),
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
        "name-newline",
        "name-tab",
        "name-escape",
        "name-trailing-newline",
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


async def test_post_name_with_an_emoji_zwj_sequence_is_accepted():
    with serve(app, runtime=FakeRuntime(run_error=RuntimeError("x"))) as tracker:
        async with _client() as http:
            response = await http.post("/api/sessions", json={"workspace": "p", "name": EMOJI_NAME})
        await tracker.close()
    assert response.status_code == 202
    assert response.json()["name"] == EMOJI_NAME


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


# ---------------------------------------------------------------------------
# GET /api/sessions/{id}/events for ended sessions (D21)
# ---------------------------------------------------------------------------


def _ndjson(path, after=None) -> str:
    return "".join(encode_event(e) + "\n" for e in SessionLog(path).read(after))


async def test_ended_session_events_are_served_from_the_log(session_logs):
    w = session_logs
    with serve(app, records=_records()):
        path = w.write(ENDED, [w.user("hello"), w.assistant("hi"), w.user("again", turn=2)])
        first, second, third = (e.id for e in SessionLog(path).read())
        async with _client() as http:
            full = await http.get(f"/api/sessions/{ENDED}/events")
            tail = await http.get(f"/api/sessions/{ENDED}/events", params={"after": first})
            last = await http.get(f"/api/sessions/{ENDED}/events", params={"after": third})
    assert full.status_code == 200
    assert full.headers["content-type"] == "application/x-ndjson"
    assert full.text == _ndjson(path)
    assert len(full.text.splitlines()) == 3
    assert tail.status_code == 200
    assert [json.loads(line)["id"] for line in tail.text.splitlines()] == [second, third]
    assert last.status_code == 200
    assert last.text == ""


async def test_ended_session_unknown_cursor_is_409(session_logs):
    w = session_logs
    with serve(app, records=_records()):
        w.write(ENDED, [w.user("hello")])
        async with _client() as http:
            response = await http.get(f"/api/sessions/{ENDED}/events", params={"after": "nope"})
    assert response.status_code == 409
    assert response.json() == {"error": "cursor_not_found", "cursor": "nope"}


async def test_failed_session_without_a_log_has_no_events():
    failed = record(
        ENDED, status="failed", error="image missing", ended_at="2026-07-01T11:00:00+00:00"
    )
    with serve(app, records=[failed]) as tracker:
        assert not tracker.log_path(ENDED).exists()
        async with _client() as http:
            response = await http.get(f"/api/sessions/{ENDED}/events")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-ndjson"
    assert response.content == b""


async def test_ended_record_awaiting_its_terminal_write_is_served_from_the_log(session_logs):
    w = session_logs
    with serve(app) as tracker:
        tracker._records[SID] = record(SID, status="exited", ended_at="2026-07-01T11:00:00+00:00")
        path = w.write(SID, [w.user("hello")])
        async with _client() as http:
            response = await http.get(f"/api/sessions/{SID}/events")
    assert response.status_code == 200
    assert response.text == _ndjson(path)


async def test_running_session_events_are_still_proxied(session_logs):
    w = session_logs
    backend = MagicMock(
        status_code=200, content=b"from-agent\n", headers={"content-type": "application/x-ndjson"}
    )
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=backend)
    with (
        serve(app, {SID: PORT}),
        patch("archie_orchestrator.proxy.httpx.AsyncClient", return_value=client),
    ):
        w.write(SID, [w.user("hello")])
        async with _client() as http:
            response = await http.get(f"/api/sessions/{SID}/events", params={"after": "x"})
    assert response.status_code == 200
    assert response.content == b"from-agent\n"
    client.get.assert_called_once_with(f"http://127.0.0.1:{PORT}/events?after=x", timeout=5.0)


async def test_unknown_session_events_is_404():
    with serve(app, records=_records()):
        async with _client() as http:
            response = await http.get("/api/sessions/nope-01kx77jkz8/events")
    assert response.status_code == 404
    assert response.json() == {"error": "No session with ID 'nope-01kx77jkz8'"}


async def test_ended_session_events_store_failure_is_500(monkeypatch):
    with serve(app) as tracker:
        monkeypatch.setattr(
            tracker.store,
            "get",
            lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
        )
        async with _client() as http:
            response = await http.get(f"/api/sessions/{ENDED}/events")
    assert response.status_code == 500
    assert response.json() == {"error": "Session store unavailable"}


# ---------------------------------------------------------------------------
# DELETE /api/sessions/{id} (D19, D19a)
# ---------------------------------------------------------------------------


async def test_delete_returns_the_stopped_record():
    runtime = FakeRuntime()
    with serve(app, records=_records(), runtime=runtime) as tracker:
        async with _client() as http:
            response = await http.delete(f"/api/sessions/{SID}")
            after = await http.get(f"/api/sessions/{SID}")
            listed = await http.get("/api/sessions")
        await tracker.close()
    assert response.status_code == 200
    stopped = msgspec.json.decode(response.content, type=SessionDescriptor)
    assert stopped.status == "stopped"
    assert stopped.ended_at is not None
    assert response.json()["container_name"] is None
    assert after.json() == response.json()
    assert listed.json() == []
    assert runtime.stops == [SID]


@pytest.mark.parametrize(
    ("stored", "message"),
    [
        (record(SID, status="starting"), f"Session '{SID}' is still starting"),
        (record(SID, status="stopping"), f"Session '{SID}' is already stopping"),
        (
            record(SID, status="stopped", ended_at="2026-07-01T11:00:00+00:00"),
            f"Session '{SID}' has already ended (stopped)",
        ),
    ],
    ids=["starting", "stopping", "ended"],
)
async def test_delete_conflicts_are_409(stored, message):
    runtime = FakeRuntime()
    with serve(app, records=[stored], runtime=runtime):
        async with _client() as http:
            response = await http.delete(f"/api/sessions/{SID}")
            after = await http.get(f"/api/sessions/{SID}")
    assert response.status_code == 409
    assert response.json() == {"error": message}
    assert after.json()["status"] == stored.status
    assert runtime.stops == []


async def test_delete_unknown_session_is_404():
    with serve(app):
        async with _client() as http:
            response = await http.delete("/api/sessions/nope-01kx77jkz8")
    assert response.status_code == 404
    assert response.json() == {"error": "No session with ID 'nope-01kx77jkz8'"}


@pytest.mark.parametrize(
    "alive_error",
    [None, DockerError(command=["docker", "inspect"], stderr="daemon down", returncode=1)],
    ids=["running", "cannot-tell"],
)
async def test_delete_docker_failure_with_container_running_is_500_and_restores_status(
    alive_error,
):
    error = DockerError(command=["docker", "stop"], stderr="daemon busy", returncode=1)
    runtime = FakeRuntime(stop_error=error, alive=True, alive_error=alive_error)
    with serve(app, records=_records(), runtime=runtime) as tracker:
        async with _client() as http:
            response = await http.delete(f"/api/sessions/{SID}")
            after = await http.get(f"/api/sessions/{SID}")
        await tracker.close()
    assert response.status_code == 500
    assert "daemon busy" in response.json()["error"]
    assert after.json()["status"] == "working"
    assert tracker.store.get(SID).status == "working"


async def test_delete_docker_failure_with_container_gone_is_stopped():
    error = DockerError(command=["docker", "stop"], stderr="No such container", returncode=1)
    runtime = FakeRuntime(stop_error=error, alive=False)
    with serve(app, records=_records(), runtime=runtime) as tracker:
        async with _client() as http:
            response = await http.delete(f"/api/sessions/{SID}")
        await tracker.close()
    assert response.status_code == 200
    assert response.json()["status"] == "stopped"
    assert tracker.store.get(SID).status == "stopped"


async def test_delete_when_docker_cannot_inspect_is_500_and_restores_status():
    """A daemon failure on ``docker inspect`` is not "container gone" (real DockerRuntime)."""
    stop_error = DockerError(command=["docker", "stop"], stderr="daemon down", returncode=1)
    inspect = MagicMock(returncode=1, stdout="", stderr="Cannot connect to the Docker daemon")
    with (
        serve(app, records=_records(), runtime=DockerRuntime()) as tracker,
        patch("archie_orchestrator.lifecycle.stop_container", side_effect=stop_error),
        patch("archie_orchestrator.docker.subprocess.run", return_value=inspect) as run,
    ):
        async with _client() as http:
            response = await http.delete(f"/api/sessions/{SID}")
            after = await http.get(f"/api/sessions/{SID}")
        await tracker.close()
    assert run.call_args.args[0][:2] == ["docker", "inspect"]
    assert response.status_code == 500
    assert "daemon down" in response.json()["error"]
    assert after.json()["status"] == "working"
    assert tracker.store.get(SID).status == "working"


async def test_delete_never_prompted_session_leaves_no_row():
    """The real DockerRuntime removes the prompt-less log, so the row goes too (D19a)."""
    log_path = home_dir() / "sessions" / f"{SID}.jsonl"
    SessionLog(log_path).append(
        SessionStarted(id="s", schema_version=2, sent_at="2026-07-02T10:00:00Z", model_key="m")
    )
    with (
        serve(app, {SID: PORT}, runtime=DockerRuntime()) as tracker,
        patch("archie_orchestrator.lifecycle.stop_container") as stop_container,
    ):
        async with _client() as http:
            response = await http.delete(f"/api/sessions/{SID}")
            after = await http.get(f"/api/sessions/{SID}")
            ended = await http.get("/api/sessions", params={"include": "ended"})
        await tracker.close()
    stop_container.assert_called_once_with(f"archie-{SID}")
    assert response.status_code == 200
    assert response.json()["status"] == "stopped"
    assert not log_path.exists()
    assert after.status_code == 404
    assert ended.json() == []
    conn = sqlite3.connect(db_path(home_dir()))
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone() == (0,)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PATCH /api/sessions/{id} (D18)
# ---------------------------------------------------------------------------


async def _patch(payload, *, records=None, session_id: str = SID):
    with serve(app, records=_records() if records is None else records) as tracker:
        async with _client() as http:
            if isinstance(payload, bytes):
                response = await http.patch(
                    f"/api/sessions/{session_id}",
                    content=payload,
                    headers={"content-type": "application/json"},
                )
            else:
                response = await http.patch(f"/api/sessions/{session_id}", json=payload)
            after = await http.get(f"/api/sessions/{session_id}")
    return response, after, tracker


async def test_patch_name_is_trimmed_and_fixed():
    response, after, tracker = await _patch({"name": "  Review PR  "})
    assert response.status_code == 200
    body = response.json()
    assert (body["name"], body["name_source"], body["status"]) == ("Review PR", "user", "working")
    assert after.json() == body
    assert tracker.store.get(SID).name == "Review PR"


async def test_patch_null_name_resets_to_the_id():
    response, _, _ = await _patch(
        {"name": None}, records=[record(SID, name="Review", name_source="user")]
    )
    assert response.status_code == 200
    assert (response.json()["name"], response.json()["name_source"]) == (SID, "id")


async def test_patch_unseen_false_clears_it():
    response, _, tracker = await _patch({"unseen": False}, records=[record(SID, unseen=True)])
    assert response.status_code == 200
    assert response.json()["unseen"] is False
    assert tracker.store.get(SID).unseen is False


async def test_patch_name_and_unseen_together():
    response, _, _ = await _patch(
        {"name": "Both", "unseen": False}, records=[record(SID, unseen=True)]
    )
    body = response.json()
    assert (body["name"], body["name_source"], body["unseen"]) == ("Both", "user", False)


async def test_patch_ended_session_is_allowed():
    response, after, tracker = await _patch({"name": "History"}, session_id=ENDED)
    assert response.status_code == 200
    body = response.json()
    assert (body["name"], body["name_source"], body["status"]) == ("History", "user", "stopped")
    assert after.json() == body
    assert tracker.store.get(ENDED).name == "History"


async def test_patch_name_of_80_characters_is_accepted():
    response, _, _ = await _patch({"name": "x" * 80})
    assert response.status_code == 200


async def test_patch_name_with_an_emoji_zwj_sequence_is_accepted():
    response, _, tracker = await _patch({"name": EMOJI_NAME})
    assert response.status_code == 200
    assert response.json()["name"] == EMOJI_NAME
    assert tracker.store.get(SID).name == EMOJI_NAME


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"not json", "Invalid JSON body"),
        (b"[1, 2]", "Invalid JSON body"),
        ({}, "Expected 'name' or 'unseen'"),
        ({"status": "idle"}, "Unknown fields: status"),
        ({"name": "ok", "model_key": "m"}, "Unknown fields: model_key"),
        ({"unseen": True}, "Invalid 'unseen'"),
        ({"unseen": 0}, "Invalid 'unseen'"),
        ({"unseen": None}, "Invalid 'unseen'"),
        ({"name": 5}, "Invalid 'name'"),
        ({"name": "   "}, "Invalid 'name'"),
        ({"name": "x" * 81}, "Invalid 'name'"),
        ({"name": "Review\nPR"}, "Invalid 'name'"),
        ({"name": "Review\tPR"}, "Invalid 'name'"),
        ({"name": "Review \x1b[31mPR"}, "Invalid 'name'"),
        ({"name": "Review\x7f"}, "Invalid 'name'"),
        ({"name": "Review\x9b"}, "Invalid 'name'"),
    ],
    ids=[
        "not-json",
        "not-object",
        "empty",
        "unknown-key",
        "unknown-key-with-name",
        "unseen-true",
        "unseen-zero",
        "unseen-null",
        "name-not-string",
        "name-blank",
        "name-too-long",
        "name-newline",
        "name-tab",
        "name-escape",
        "name-del",
        "name-c1-control",
    ],
)
async def test_patch_bad_body_is_400_and_changes_nothing(payload, message):
    response, after, _ = await _patch(payload, records=[record(SID, unseen=True)])
    assert response.status_code == 400
    assert message in response.json()["error"]
    assert after.json() == msgspec.to_builtins(record(SID, unseen=True))


async def test_patch_unknown_session_is_404():
    response, _, _ = await _patch({"name": "x"}, session_id="nope-01kx77jkz8")
    assert response.status_code == 404
    assert response.json() == {"error": "No session with ID 'nope-01kx77jkz8'"}


@pytest.mark.parametrize("session_id", [SID, ENDED], ids=["live", "ended"])
async def test_patch_store_write_failure_is_500(monkeypatch, session_id):
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    with serve(app, records=_records()) as tracker:
        monkeypatch.setattr(tracker.store, "save", broken)
        monkeypatch.setattr(tracker.store, "update", broken)
        async with _client() as http:
            response = await http.patch(f"/api/sessions/{session_id}", json={"name": "x"})
    assert response.status_code == 500
    assert response.json() == {"error": "Session store unavailable"}

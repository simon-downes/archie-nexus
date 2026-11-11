"""Tests for archie_orchestrator.tracker — records, background start and reconcile (082 M1)."""

import asyncio
import sqlite3
import threading
from unittest.mock import patch

import pytest
from archie_orchestrator import tracker as tracker_module
from archie_orchestrator.docker import DockerError
from archie_orchestrator.store import SessionStore, db_path, id_start_time
from archie_orchestrator.tracker import DockerRuntime, SessionTracker
from archie_shared.config import home_dir
from archie_shared.events import PendingQuestion, QuestionOption
from archie_shared.schemas import NexusConfig

from tests.session_fakes import (
    PORT,
    SID,
    FakeAgent,
    FakeRuntime,
    handshake_frame,
    make_tracker,
    record,
    running,
    status_frame,
)

QUESTION = {"id": "tu1", "question": "Which?", "options": [{"label": "a"}], "recommended": 0}


async def until(predicate, timeout: float = 3.0):
    """Wait for ``predicate()`` to become true while background tasks run."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def _patch_agent(agent: FakeAgent):
    return patch("archie_orchestrator.tracker.websockets.connect", agent.connect())


def _tracker(runtime: FakeRuntime | None = None) -> SessionTracker:
    return make_tracker(runtime=runtime or FakeRuntime())


@pytest.fixture
def fast_retry(monkeypatch):
    monkeypatch.setattr(tracker_module, "RESUBSCRIBE_DELAY", 0.01)
    monkeypatch.setattr(tracker_module, "TERMINAL_RETRY_INTERVAL", 0.01)


# ---------------------------------------------------------------------------
# create() and the background start
# ---------------------------------------------------------------------------


async def test_create_records_starting_then_idle_when_ready():
    runtime = FakeRuntime(port=40001)
    runtime.run_gate = threading.Event()
    tracker = _tracker(runtime)
    published = []
    tracker.add_listener(published.append)
    agent = FakeAgent([handshake_frame(), status_frame("idle", model_key="m1"), 5.0])

    with _patch_agent(agent):
        created = await tracker.create("proj", backend="native", name=None, config=NexusConfig())
        sid = created.session_id
        assert created.status == "starting"
        assert (created.name, created.name_fixed, created.workspace) == (sid, False, "proj")
        assert created.container_name == f"archie-{sid}"
        assert tracker.store.get(sid) == created  # inserted before the start runs
        assert tracker.live_ids() == {sid}
        assert tracker.port(sid) is None
        assert published == [created]

        runtime.run_gate.set()
        await until(lambda: tracker.live(sid).status == "idle")
        live = tracker.live(sid)
        assert live.model_key == "m1"
        assert tracker.port(sid) == 40001
        assert agent.urls == ["ws://127.0.0.1:40001/stream"]
        await until(lambda: tracker.store.get(sid).status == "idle")
        await tracker.close()

    assert [r.status for r in published] == ["starting", "idle"]
    assert runtime.prepared == [("proj", "native")]


async def test_create_with_name_sets_name_fixed():
    tracker = _tracker(FakeRuntime(run_error=DockerError("docker run", "x", 1)))
    created = await tracker.create("proj", backend="kiro", name="Review", config=NexusConfig())
    assert (created.name, created.name_fixed, created.backend) == ("Review", True, "kiro")
    await tracker.close()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            DockerError(
                command=["docker", "inspect", "c"],
                stderr="Container 'c' exited during startup.\nKiro startup failed: kiro-cli "
                "is not logged in",
                returncode=1,
            ),
            "Kiro startup failed: kiro-cli is not logged in",
        ),
        (RuntimeError("did not become ready"), "did not become ready"),
    ],
    ids=["kiro-startup-error", "runtime-error"],
)
async def test_start_failure_records_failed_with_error(error, expected):
    tracker = _tracker(FakeRuntime(run_error=error))
    created = await tracker.create("proj", backend="kiro", name=None, config=NexusConfig())
    sid = created.session_id

    await until(lambda: tracker.live(sid) is None and sid not in tracker._records)

    stored = tracker.store.get(sid)
    assert stored.status == "failed"
    assert expected in stored.error
    assert stored.ended_at is not None
    assert stored.container_name is None
    assert tracker.live_ids() == set()
    assert await tracker.list() == []
    assert await tracker.list(include_ended=True) == [stored]
    assert await tracker.get(sid) == stored
    await tracker.close()


async def test_create_rejected_by_prepare_records_nothing():
    runtime = FakeRuntime(prepare_error=ValueError("Workspace 'bad' not found"))
    tracker = _tracker(runtime)
    with pytest.raises(ValueError, match="not found"):
        await tracker.create("bad", backend="native", name=None, config=NexusConfig())
    assert await tracker.list(include_ended=True) == []
    assert runtime.runs == []


async def test_create_insert_failure_starts_nothing(monkeypatch):
    runtime = FakeRuntime()
    tracker = _tracker(runtime)

    def broken(_record):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(tracker.store, "insert", broken)
    with pytest.raises(sqlite3.OperationalError):
        await tracker.create("proj", backend="native", name=None, config=NexusConfig())
    await asyncio.sleep(0.05)
    assert runtime.runs == []
    assert tracker.live_ids() == set()


# ---------------------------------------------------------------------------
# Following the agent stream (session_status only in M1)
# ---------------------------------------------------------------------------


async def _follow(frames, *, alive: bool = True):
    runtime = FakeRuntime(alive=alive)
    tracker = make_tracker({SID: PORT}, runtime=runtime)
    snapshots = []
    tracker.add_listener(snapshots.append)
    agent = FakeAgent([handshake_frame(), *frames])
    with _patch_agent(agent):
        await tracker._follow(SID)
    return tracker, snapshots, runtime


async def test_session_status_sets_state_model_and_question():
    tracker, snapshots, _ = await _follow(
        [
            status_frame("idle", model_key="m1"),
            status_frame("working", model_key="m1"),
            status_frame("blocked", model_key="m2", question=QUESTION),
            status_frame("done", model_key="m2"),
        ],
        alive=False,
    )
    question = PendingQuestion(
        id="tu1", question="Which?", options=[QuestionOption("a")], recommended=0
    )
    assert [(s.status, s.model_key, s.question) for s in snapshots[:4]] == [
        ("idle", "m1", None),
        ("working", "m1", None),
        ("blocked", "m2", question),
        ("idle", "m2", None),  # done is never stored
    ]
    await tracker.close()


async def test_unchanged_status_is_not_republished():
    tracker, snapshots, _ = await _follow(
        [status_frame("idle"), status_frame("idle"), "not json", '{"type": "mystery"}'],
        alive=False,
    )
    # The idle model update, then the exit when the stream closed (container gone).
    assert [s.status for s in snapshots] == ["idle", "exited"]
    await tracker.close()


async def test_stream_close_with_container_gone_marks_exited():
    tracker, snapshots, runtime = await _follow([status_frame("idle")], alive=False)
    assert runtime.checked == [SID]
    stored = tracker.store.get(SID)
    assert stored.status == "exited"
    assert stored.ended_at is not None
    assert tracker.live_ids() == set()
    assert tracker.port(SID) is None
    assert snapshots[-1] == stored
    await tracker.close()


async def test_stream_close_with_container_running_resubscribes(fast_retry):
    runtime = FakeRuntime(alive=True)
    tracker = make_tracker({SID: PORT}, runtime=runtime)
    agent = FakeAgent([status_frame("working")], [status_frame("idle")])
    with _patch_agent(agent):
        task = asyncio.create_task(tracker._follow(SID))
        await until(lambda: len(agent.urls) >= 3)  # two scripts, then a refused connect
        runtime.alive = False
        await asyncio.wait_for(task, 2)
    assert agent.urls[:2] == [f"ws://127.0.0.1:{PORT}/stream"] * 2
    assert tracker.store.get(SID).status == "exited"
    await tracker.close()


async def test_close_cancels_following_without_status_change():
    tracker = make_tracker({SID: PORT})
    agent = FakeAgent([status_frame("working"), 30.0])
    with _patch_agent(agent):
        tracker._spawn(SID, tracker._follow(SID))
        await until(lambda: tracker.live(SID).status == "working")
        await tracker.close()
    assert tracker.live(SID).status == "working"
    assert tracker.store.get(SID).status == "working"
    assert tracker._tasks == {}


# ---------------------------------------------------------------------------
# Store write failures (D12a)
# ---------------------------------------------------------------------------


async def test_failed_write_keeps_memory_authoritative(monkeypatch):
    tracker = make_tracker({SID: PORT})
    monkeypatch.setattr(
        tracker.store, "save", lambda r: (_ for _ in ()).throw(sqlite3.OperationalError("x"))
    )
    await tracker._update(SID, status="working")
    assert tracker.live(SID).status == "working"
    assert (await tracker.get(SID)).status == "working"


async def test_failed_terminal_write_is_retried(monkeypatch, fast_retry):
    tracker = make_tracker({SID: PORT})
    real_save = tracker.store.save
    failures = [sqlite3.OperationalError("locked")] * 2

    def flaky(item):
        if failures:
            raise failures.pop()
        real_save(item)

    monkeypatch.setattr(tracker.store, "save", flaky)
    await tracker._update(SID, status="exited", ended_at="2026-07-01T10:00:00+00:00")

    # Until the write commits the ended record stays in memory, served to readers.
    assert SID in tracker._records
    assert tracker.live_ids() == set()
    assert [r.status for r in await tracker.list(include_ended=True)] == ["exited"]

    await until(lambda: SID not in tracker._records)
    assert tracker.store.get(SID).status == "exited"
    await tracker.close()


# ---------------------------------------------------------------------------
# start(): migrate and minimal reconcile (D13)
# ---------------------------------------------------------------------------


async def test_start_migrates_imports_and_revives_running_imported_row(session_logs):
    w = session_logs
    w.write(SID, [w.session_started(model_key="m0"), w.user("hi")])
    w.write("old-01kx77jkz8", [w.user("bye")])
    runtime = FakeRuntime([running(SID, PORT)])
    tracker = SessionTracker(SessionStore(db_path(home_dir())), runtime)
    agent = FakeAgent([status_frame("idle", model_key="m1"), 30.0])

    with _patch_agent(agent):
        await tracker.start()
        live = tracker.live(SID)
        assert (live.status, live.ended_at, live.model_key) == ("idle", None, "m0")
        assert live.started_at == "2026-07-01T09:59:00+00:00"
        assert tracker.port(SID) == PORT
        assert tracker.live_ids() == {SID}
        await until(lambda: tracker.live(SID).model_key == "m1")
        await tracker.close()

    assert agent.urls == [f"ws://127.0.0.1:{PORT}/stream"]
    assert tracker.store.get(SID).ended_at is None
    assert tracker.store.get("old-01kx77jkz8").status == "stopped"
    assert runtime.list_calls == 1


async def test_start_adopts_a_container_with_a_log_but_no_row(session_logs):
    tracker = make_tracker()  # migrated before the log exists, so it has no row
    w = session_logs
    w.write(SID, [w.session_started(model_key="kiro/auto"), w.user("hi")])
    tracker.runtime = FakeRuntime([running(SID, None)])

    await tracker.start()

    adopted = tracker.live(SID)
    assert (adopted.status, adopted.backend, adopted.model_key) == ("idle", "kiro", "kiro/auto")
    assert adopted.started_at == "2026-07-01T09:59:00+00:00"
    assert adopted.ended_at is None
    assert tracker.port(SID) is None  # no port published yet, so nothing to follow
    assert tracker._tasks == {}
    assert tracker.store.get(SID) == adopted
    await tracker.close()


async def test_start_adopts_a_container_without_a_log():
    sid = "my-app-01kx77jkz8"
    tracker = SessionTracker(SessionStore(db_path(home_dir())), FakeRuntime([running(sid, None)]))
    await tracker.start()
    adopted = tracker.live(sid)
    assert (adopted.workspace, adopted.backend, adopted.name) == ("my-app", "native", sid)
    assert adopted.started_at == id_start_time(sid)
    assert adopted.status == "idle"
    await tracker.close()


async def test_start_keeps_live_rows_from_the_store():
    tracker = make_tracker(records=[record(SID, status="working")])
    restarted = SessionTracker(tracker.store, FakeRuntime([running(SID, None)]))
    await restarted.start()
    assert restarted.live(SID).status == "idle"  # a running container is live and idle
    await restarted.close()


async def test_start_when_docker_fails_leaves_records(caplog):
    tracker = make_tracker(records=[record(SID, status="working")])
    restarted = SessionTracker(
        tracker.store, FakeRuntime(list_error=DockerError("docker ps", "daemon down", 1))
    )
    await restarted.start()
    assert restarted.live(SID).status == "working"
    assert any("Cannot list running sessions" in r.message for r in caplog.records)
    await restarted.close()


# ---------------------------------------------------------------------------
# DockerRuntime
# ---------------------------------------------------------------------------


def test_docker_runtime_delegates_to_docker_and_lifecycle():
    runtime = DockerRuntime()
    with (
        patch("archie_orchestrator.docker.list_sessions", return_value=[running()]) as ls,
        patch("archie_orchestrator.docker.container_running", return_value=True) as alive,
        patch("archie_orchestrator.lifecycle.prepare_start", return_value="prepared") as prep,
        patch("archie_orchestrator.lifecycle.run_start", return_value=PORT) as run,
    ):
        assert runtime.list_running() == [running()]
        assert runtime.is_running(SID) is True
        assert runtime.prepare("proj", "config", "kiro") == "prepared"
        assert runtime.run("prepared") == PORT
    ls.assert_called_once_with()
    alive.assert_called_once_with(f"archie-{SID}")
    prep.assert_called_once_with("proj", "config", backend="kiro")
    run.assert_called_once_with("prepared")

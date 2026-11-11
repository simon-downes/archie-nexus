"""Tests for archie_orchestrator.tracker — records, start, turn status, reconcile, ending (082)."""

import asyncio
import itertools
import os
import re
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from archie_orchestrator import tracker as tracker_module
from archie_orchestrator.docker import DockerError
from archie_orchestrator.store import SessionStore, db_path, id_start_time
from archie_orchestrator.tracker import (
    RESTARTED_DURING_STARTUP,
    DockerRuntime,
    SessionConflictError,
    SessionTracker,
    resubscribe_delay,
)
from archie_shared.config import home_dir
from archie_shared.events import PendingQuestion, QuestionOption
from archie_shared.schemas import NexusConfig

from tests.session_fakes import (
    PORT,
    SID,
    FakeAgent,
    FakeRuntime,
    delta_frame,
    handshake_frame,
    make_tracker,
    record,
    running,
    status_frame,
    turn_frame,
    user_frame,
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
    monkeypatch.setattr(tracker_module, "RESUBSCRIBE_DELAYS", (0.01,))
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
# Following the agent stream: session_status
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
    runtime = FakeRuntime(alive=False)
    tracker = make_tracker({SID: PORT}, runtime=runtime)
    agent = FakeAgent([status_frame("working"), 30.0])
    with _patch_agent(agent):
        tracker._spawn(SID, tracker._follow(SID))
        await until(lambda: tracker.live(SID).status == "working")
        await until(lambda: tracker.store.get(SID).status == "working")
        await tracker.close()
    # Shutdown skips the end check (D14): the container is never asked about.
    assert runtime.checked == []
    assert tracker.live(SID).status == "working"
    assert tracker.store.get(SID).status == "working"
    assert tracker._tasks == {}


# ---------------------------------------------------------------------------
# Turn status from the stream (D10)
# ---------------------------------------------------------------------------


def _turns(snapshots):
    """(status, unseen) of each published record before the final ``exited``."""
    assert snapshots[-1].status == "exited"
    return [(s.status, s.unseen) for s in snapshots[:-1]]


async def test_native_turn_working_then_unseen_and_idle():
    tracker, snapshots, _ = await _follow(
        [
            status_frame("idle"),
            user_frame(),
            status_frame("working"),
            delta_frame(),
            # Subagent events are activity only.
            user_frame(scope="sub-1"),
            turn_frame("error", scope="sub-1"),
            turn_frame("complete", scope="sub-1"),
            turn_frame("complete"),
            status_frame("idle"),
        ],
        alive=False,
    )
    assert _turns(snapshots) == [
        ("idle", False),
        ("working", False),
        ("working", True),
        ("idle", True),
    ]
    assert snapshots[2].last_activity_at is not None
    assert snapshots[0].backend == "native"
    assert tracker.store.get(SID).unseen is True
    await tracker.close()


async def test_kiro_turn_without_working_status():
    kiro = "kiro/auto"
    tracker, snapshots, _ = await _follow(
        [
            status_frame("idle", model_key=kiro),
            user_frame(),  # Kiro sends no working status at turn start
            delta_frame(),
            turn_frame("complete"),
            status_frame("idle", model_key=kiro),
        ],
        alive=False,
    )
    assert _turns(snapshots) == [
        ("idle", False),
        ("working", False),
        ("working", True),
        ("idle", True),
    ]
    # The model marks an adopted session's backend (it started as native).
    assert (snapshots[0].backend, snapshots[0].model_key) == ("kiro", kiro)
    assert tracker.store.get(SID).backend == "kiro"
    await tracker.close()


async def test_failed_turn_then_idle_is_error_until_the_next_message():
    tracker, snapshots, _ = await _follow(
        [
            status_frame("idle"),
            user_frame(),
            status_frame("working"),
            turn_frame("error"),
            status_frame("idle"),  # sent straight after the failed turn
            status_frame("idle"),  # e.g. on reconnect: still error
            user_frame(),
            turn_frame("complete"),
            status_frame("idle"),
        ],
        alive=False,
    )
    assert _turns(snapshots) == [
        ("idle", False),
        ("working", False),
        ("working", True),
        ("error", True),
        ("working", True),
        ("idle", True),
    ]
    assert all(s.error is None for s in snapshots)  # ``error`` is for start failures
    await tracker.close()


@pytest.mark.parametrize(
    ("frames", "expected"),
    [
        (
            [user_frame(), turn_frame("interrupted"), status_frame("idle")],
            [("idle", False), ("working", False), ("idle", False)],
        ),
        (
            [user_frame(), turn_frame("error"), turn_frame("interrupted"), status_frame("idle")],
            [("idle", False), ("working", False), ("working", True), ("idle", True)],
        ),
    ],
    ids=["interrupted", "interrupt-clears-pending-error"],
)
async def test_interrupted_turn_sets_no_unseen_and_clears_a_pending_error(frames, expected):
    tracker, snapshots, _ = await _follow([status_frame("idle"), *frames], alive=False)
    assert _turns(snapshots) == expected
    await tracker.close()


async def test_pending_question_blocks_then_clears():
    tracker, snapshots, _ = await _follow(
        [
            status_frame("idle"),
            user_frame(),
            status_frame("working"),
            status_frame("blocked", question=QUESTION),
            status_frame("working"),  # answered
            turn_frame("complete"),
            status_frame("idle"),
        ],
        alive=False,
    )
    assert [(s.status, s.question is not None, s.unseen) for s in snapshots[:-1]] == [
        ("idle", False, False),
        ("working", False, False),
        ("blocked", True, False),
        ("working", False, False),
        ("working", False, True),
        ("idle", False, True),
    ]
    assert snapshots[2].question.id == "tu1"
    await tracker.close()


# ---------------------------------------------------------------------------
# Activity throttling (D11)
# ---------------------------------------------------------------------------


def _tick(n: int) -> str:
    return f"2026-07-01T10:00:{n:02d}+00:00"


@pytest.fixture
def ticks(monkeypatch):
    """The tracker's ``now_iso`` returns ``_tick(1)``, ``_tick(2)``, ... in call order."""
    counter = itertools.count(1)
    monkeypatch.setattr(tracker_module, "now_iso", lambda: _tick(next(counter)))


class FakeClock:
    """A monotonic clock that only moves when a test moves it."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@asynccontextmanager
async def _tracking(frames, *, interval: float):
    """Follow ``frames`` (then an open stream) with a frozen clock and an activity window."""
    tracker = make_tracker({SID: PORT})
    clock = FakeClock()
    tracker.activity_interval = interval
    tracker.clock = clock
    snapshots = []
    tracker.add_listener(snapshots.append)
    with _patch_agent(FakeAgent([*frames, 30.0])):
        tracker._spawn(SID, tracker._follow(SID))
        try:
            yield tracker, snapshots, clock
        finally:
            await tracker.close()


async def test_activity_only_changes_are_throttled_with_a_trailing_flush(ticks):
    frames = [user_frame(), delta_frame(), delta_frame(), delta_frame()]
    async with _tracking(frames, interval=0.05) as (tracker, snapshots, _):
        await until(lambda: len(snapshots) == 2)
        await until(lambda: tracker.store.get(SID).last_activity_at == _tick(4))
        await asyncio.sleep(0.15)
        # The message is written at once; the three deltas once, at the end of the window.
        assert [(s.status, s.last_activity_at) for s in snapshots] == [
            ("working", _tick(1)),
            ("working", _tick(4)),
        ]
        assert tracker._flush_tasks == {}


async def test_activity_after_the_window_is_written_at_once(ticks):
    async with _tracking([user_frame(), 0.5, delta_frame()], interval=10.0) as (
        tracker,
        snapshots,
        clock,
    ):
        await until(lambda: len(snapshots) == 1)
        clock.now += 10.0
        await until(lambda: len(snapshots) == 2)  # well before a 10 s trailing flush
        assert snapshots[1].last_activity_at == _tick(2)
        assert tracker._flush_tasks == {}


async def test_status_change_carries_the_latest_activity(ticks):
    frames = [user_frame(), delta_frame(), status_frame("blocked", question=QUESTION)]
    async with _tracking(frames, interval=0.05) as (tracker, snapshots, _):
        await until(lambda: len(snapshots) == 2)
        await asyncio.sleep(0.15)
        # The blocked write took the delta's activity, so no trailing write follows.
        assert [(s.status, s.last_activity_at) for s in snapshots] == [
            ("working", _tick(1)),
            ("blocked", _tick(2)),
        ]
        assert tracker._flush_tasks == {}


async def test_close_writes_pending_activity(ticks):
    async with _tracking([user_frame(), delta_frame()], interval=10.0) as (tracker, snapshots, _):
        await until(lambda: SID in tracker._flush_tasks)
        assert len(snapshots) == 1
        await tracker.close()
        assert tracker.store.get(SID).last_activity_at == _tick(2)
        assert [s.last_activity_at for s in snapshots] == [_tick(1), _tick(2)]
        assert tracker.live(SID).status == "working"


async def test_session_end_takes_pending_activity(ticks):
    tracker, snapshots, _ = await _follow([user_frame(), delta_frame()], alive=False)
    assert [(s.status, s.last_activity_at) for s in snapshots] == [
        ("working", _tick(1)),
        ("exited", _tick(2)),
    ]
    assert tracker.store.get(SID).ended_at == _tick(3)
    assert tracker._flush_tasks == {}
    await tracker.close()


# ---------------------------------------------------------------------------
# Ending (D14)
# ---------------------------------------------------------------------------


def test_resubscribe_delay_backs_off_1_2_5_and_stays_at_5():
    assert [resubscribe_delay(n) for n in range(6)] == [1.0, 2.0, 5.0, 5.0, 5.0, 5.0]


async def test_resubscribe_backs_off_while_running_and_resets_after_a_frame(monkeypatch):
    delays = []
    real = tracker_module.resubscribe_delay

    def spy(attempt):
        delays.append(real(attempt))
        return 0.001

    monkeypatch.setattr(tracker_module, "resubscribe_delay", spy)
    runtime = FakeRuntime(alive=True)
    tracker = make_tracker({SID: PORT}, runtime=runtime)
    snapshots = []
    tracker.add_listener(snapshots.append)
    # Four streams that close without a frame, one that delivers a frame, then refusals.
    agent = FakeAgent([], [], [], [], [status_frame("working")])
    with _patch_agent(agent):
        task = asyncio.create_task(tracker._follow(SID))
        await until(lambda: len(delays) >= 8)
        runtime.alive = False
        await asyncio.wait_for(task, 2)
    assert delays[:8] == [1.0, 2.0, 5.0, 5.0, 1.0, 2.0, 5.0, 5.0]
    # Backing off never changes the status; the container going away ends the session.
    assert [s.status for s in snapshots] == ["working", "exited"]
    assert len(runtime.checked) == len(delays) + 1
    await tracker.close()


async def test_stream_close_while_stopping_does_nothing():
    runtime = FakeRuntime(alive=False)
    tracker = make_tracker(records=[record(SID, status="stopping")], runtime=runtime)
    tracker._ports[SID] = PORT
    with _patch_agent(FakeAgent([handshake_frame()])):
        await asyncio.wait_for(tracker._follow(SID), 2)
    assert runtime.checked == []
    assert tracker.live(SID).status == "stopping"
    await tracker.close()


async def test_close_during_back_off_changes_nothing():
    runtime = FakeRuntime(alive=True)
    tracker = make_tracker({SID: PORT}, runtime=runtime)
    with _patch_agent(FakeAgent([status_frame("working")])):
        tracker._spawn(SID, tracker._follow(SID))
        await until(lambda: runtime.checked == [SID])  # now waiting 1 s to resubscribe
        await tracker.close()
    assert runtime.checked == [SID]
    assert tracker.live(SID).status == "working"


async def test_close_retries_a_pending_terminal_write(monkeypatch):
    tracker = make_tracker({SID: PORT})
    real_save = tracker.store.save
    failures = [sqlite3.OperationalError("locked")]

    def flaky(item):
        if failures:
            raise failures.pop()
        real_save(item)

    monkeypatch.setattr(tracker.store, "save", flaky)
    await tracker._update(SID, status="exited", ended_at="2026-07-01T10:00:00+00:00")
    assert SID in tracker._records  # the retry waits TERMINAL_RETRY_INTERVAL (5 s)
    await tracker.close()
    assert SID not in tracker._records
    assert tracker.store.get(SID).status == "exited"


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
# start(): migrate and reconcile (D13)
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


@pytest.mark.parametrize(
    ("stored", "expected", "error"),
    [
        ("idle", "exited", None),
        ("working", "exited", None),
        ("blocked", "exited", None),
        ("error", "exited", None),
        ("stopping", "stopped", None),
        ("starting", "failed", RESTARTED_DURING_STARTUP),
    ],
)
async def test_start_ends_a_live_row_without_a_container(session_logs, stored, expected, error):
    tracker = make_tracker(records=[record(SID, status=stored)])
    w = session_logs
    w.write(SID, [w.user("hi")])
    mtime = datetime(2026, 7, 1, 11, 30, tzinfo=UTC).timestamp()
    os.utime(w.dir / f"{SID}.jsonl", (mtime, mtime))
    restarted = SessionTracker(tracker.store, FakeRuntime([]))
    published = []
    restarted.add_listener(published.append)

    await restarted.start()

    row = restarted.store.get(SID)
    assert (row.status, row.error) == (expected, error)
    assert row.ended_at == "2026-07-01T11:30:00+00:00"  # the log's mtime
    assert restarted.live_ids() == set()
    assert published == [row]
    await restarted.close()


async def test_start_ends_a_live_row_without_container_or_log_now():
    tracker = make_tracker(records=[record(SID, status="working")])
    restarted = SessionTracker(tracker.store, FakeRuntime([]))
    before = datetime.now(UTC)
    await restarted.start()
    row = restarted.store.get(SID)
    assert row.status == "exited"
    assert datetime.fromisoformat(row.ended_at) >= before
    await restarted.close()


async def test_start_leaves_ended_rows_alone():
    ended = record(SID, status="stopped", ended_at="2026-07-01T10:00:00+00:00")
    tracker = make_tracker(records=[ended])
    restarted = SessionTracker(tracker.store, FakeRuntime([]))
    await restarted.start()
    assert restarted.store.get(SID) == ended
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


async def test_reconcile_retries_while_docker_fails(monkeypatch):
    monkeypatch.setattr(tracker_module, "RECONCILE_RETRY_INTERVAL", 0.01)
    gone, back = "gone-01abc12345", SID
    base = make_tracker(records=[record(gone, status="working"), record(back, status="blocked")])
    runtime = FakeRuntime(list_error=DockerError("docker ps", "daemon down", 1))
    runtime.run_gate = threading.Event()
    restarted = SessionTracker(base.store, runtime)
    agent = FakeAgent([status_frame("idle"), 30.0])

    with _patch_agent(agent):
        try:
            await restarted.start()
            await until(lambda: runtime.list_calls >= 3)
            # While Docker fails nothing ends: "cannot list" is not "nothing runs".
            assert restarted.live(gone).status == "working"
            assert restarted.live(back).status == "blocked"
            # A session started during the outage isn't a stale row; reconcile leaves it be.
            created = await restarted.create(
                "proj", backend="native", name=None, config=NexusConfig()
            )

            runtime.containers = [running(back, PORT)]
            runtime.list_error = None
            await until(lambda: gone not in restarted._records)  # ended and written
            assert restarted.store.get(gone).status == "exited"
            assert restarted.live(back).status == "idle"
            assert restarted.live(created.session_id).status == "starting"
            await until(lambda: agent.urls == [f"ws://127.0.0.1:{PORT}/stream"])
            calls = runtime.list_calls
            await asyncio.sleep(0.05)
            assert runtime.list_calls == calls  # retrying stopped once reconcile succeeded
        finally:
            runtime.run_gate.set()
            await restarted.close()


# ---------------------------------------------------------------------------
# stop() (D19, D19a) and patch() (D18)
# ---------------------------------------------------------------------------


async def test_stop_marks_stopping_then_stopped():
    runtime = FakeRuntime()
    runtime.stop_gate = threading.Event()
    tracker = make_tracker({SID: PORT}, runtime=runtime)
    published = []
    tracker.add_listener(published.append)

    stopping = asyncio.create_task(tracker.stop(SID))
    await until(lambda: tracker.live(SID).status == "stopping")
    assert tracker.store.get(SID).status == "stopping"  # written before Docker is asked
    runtime.stop_gate.set()
    stopped = await stopping

    assert (stopped.status, stopped.container_name) == ("stopped", None)
    assert stopped.ended_at is not None
    assert tracker.store.get(SID) == stopped
    assert tracker.live_ids() == set()
    assert tracker.port(SID) is None
    assert [r.status for r in published] == ["stopping", "stopped"]
    assert runtime.stops == [SID]
    await tracker.close()


async def test_stop_that_removed_the_log_deletes_the_row():
    tracker = make_tracker({SID: PORT}, runtime=FakeRuntime(log_removed=True))
    published = []
    tracker.add_listener(published.append)

    stopped = await tracker.stop(SID)

    assert stopped.status == "stopped"
    assert published[-1] == stopped  # the terminal record is still published
    assert tracker.store.get(SID) is None
    assert await tracker.get(SID) is None
    assert await tracker.list(include_ended=True) == []
    await tracker.close()


async def test_stop_row_delete_failure_is_retried(monkeypatch, fast_retry):
    tracker = make_tracker({SID: PORT}, runtime=FakeRuntime(log_removed=True))
    real_delete = tracker.store.delete
    failures = [sqlite3.OperationalError("locked")]

    def flaky(session_id):
        if failures:
            raise failures.pop()
        real_delete(session_id)

    monkeypatch.setattr(tracker.store, "delete", flaky)
    stopped = await tracker.stop(SID)
    assert (await tracker.get(SID)) == stopped  # held in memory until the delete commits
    await until(lambda: SID not in tracker._records)
    assert tracker.store.get(SID) is None
    await tracker.close()


@pytest.mark.parametrize(
    ("stored", "message"),
    [
        (record(SID, status="starting"), "is still starting"),
        (record(SID, status="stopping"), "is already stopping"),
        (
            record(SID, status="exited", ended_at="2026-07-01T10:00:00+00:00"),
            "has already ended (exited)",
        ),
    ],
    ids=["starting", "stopping", "ended"],
)
async def test_stop_conflicts(stored, message):
    runtime = FakeRuntime()
    tracker = make_tracker(records=[stored], runtime=runtime)
    with pytest.raises(SessionConflictError, match=re.escape(message)):
        await tracker.stop(SID)
    assert runtime.stops == []
    assert (await tracker.get(SID)).status == stored.status


async def test_stop_unknown_session_is_key_error():
    runtime = FakeRuntime()
    with pytest.raises(KeyError):
        await make_tracker(runtime=runtime).stop(SID)
    assert runtime.stops == []


async def test_stop_failure_with_container_running_restores_status_and_follows_again():
    error = DockerError(command=["docker", "stop"], stderr="daemon busy", returncode=1)
    runtime = FakeRuntime(stop_error=error, alive=True)
    runtime.stop_gate = threading.Event()
    tracker = make_tracker({SID: PORT}, records=[record(SID, status="working")], runtime=runtime)
    published = []
    tracker.add_listener(published.append)
    agent = FakeAgent([handshake_frame()], [status_frame("idle"), 30.0])
    with _patch_agent(agent):
        stopping = asyncio.create_task(tracker.stop(SID))
        await until(lambda: tracker.live(SID).status == "stopping")
        # The stream closes while stopping: the end check is skipped and following ends.
        tracker._spawn(SID, tracker._follow(SID))
        await until(lambda: SID not in tracker._tasks)
        assert runtime.checked == []
        runtime.stop_gate.set()
        with pytest.raises(DockerError):
            await stopping

        assert runtime.checked == [SID]  # asked once, after the failed stop
        await until(lambda: tracker.live(SID).status == "idle")  # followed again
        assert len(agent.urls) == 2
        # The previous status was restored (and written) before the stream moved it on.
        assert [r.status for r in published] == ["stopping", "working", "idle"]
        await until(lambda: tracker.store.get(SID).status == "idle")
        await tracker.close()


async def test_stop_failure_with_container_gone_is_stopped():
    error = DockerError(command=["docker", "stop"], stderr="No such container", returncode=1)
    runtime = FakeRuntime(stop_error=error, alive=False)
    tracker = make_tracker({SID: PORT}, runtime=runtime)
    stopped = await tracker.stop(SID)
    assert stopped.status == "stopped"
    assert tracker.store.get(SID).status == "stopped"
    assert runtime.checked == [SID]
    await tracker.close()


async def test_frames_while_stopping_keep_the_status():
    runtime = FakeRuntime()
    runtime.stop_gate = threading.Event()
    tracker = make_tracker({SID: PORT}, runtime=runtime)
    stopping = asyncio.create_task(tracker.stop(SID))
    await until(lambda: tracker.live(SID).status == "stopping")
    await tracker._on_frame(SID, status_frame("idle", model_key="m9"))
    await tracker._on_frame(SID, user_frame())
    assert tracker.live(SID).status == "stopping"
    assert tracker.live(SID).model_key == "m9"
    runtime.stop_gate.set()
    assert (await stopping).status == "stopped"
    await tracker.close()


async def test_container_gone_check_racing_a_stop_does_not_mark_exited():
    runtime = FakeRuntime()
    tracker = make_tracker({SID: PORT}, runtime=runtime)

    def stopped_meanwhile(session_id):
        tracker._records[SID] = record(SID, status="stopping")
        return False

    runtime.is_running = stopped_meanwhile
    assert await tracker._still_running(SID) is False
    assert tracker.live(SID).status == "stopping"


async def test_patch_live_session_name_and_unseen():
    tracker = make_tracker(records=[record(SID, unseen=True)])
    published = []
    tracker.add_listener(published.append)

    renamed = await tracker.patch(SID, name="Review")
    assert (renamed.name, renamed.name_fixed, renamed.unseen) == ("Review", True, True)
    seen = await tracker.patch(SID, unseen=False)
    assert (seen.name, seen.unseen) == ("Review", False)
    reset = await tracker.patch(SID, name=None)
    assert (reset.name, reset.name_fixed) == (SID, False)

    assert published == [renamed, seen, reset]
    assert tracker.live(SID) == reset
    assert tracker.store.get(SID) == reset


async def test_patch_ended_session_updates_the_store_and_publishes():
    ended = record(SID, status="stopped", ended_at="2026-07-01T10:00:00+00:00", unseen=True)
    tracker = make_tracker(records=[ended])
    published = []
    tracker.add_listener(published.append)

    updated = await tracker.patch(SID, name="Old work", unseen=False)

    assert (updated.name, updated.name_fixed, updated.unseen) == ("Old work", True, False)
    assert (updated.status, updated.ended_at) == ("stopped", ended.ended_at)
    assert tracker.store.get(SID) == updated
    assert published == [updated]
    assert SID not in tracker._records


async def test_patch_unknown_session_is_key_error():
    with pytest.raises(KeyError):
        await make_tracker().patch(SID, name="x")


async def test_patch_write_failure_raises_and_memory_keeps_the_change(monkeypatch):
    tracker = make_tracker({SID: PORT})
    monkeypatch.setattr(
        tracker.store, "save", lambda r: (_ for _ in ()).throw(sqlite3.OperationalError("x"))
    )
    with pytest.raises(sqlite3.OperationalError):
        await tracker.patch(SID, name="Review")
    assert tracker.live(SID).name == "Review"  # D12a: memory stays authoritative


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


def test_docker_runtime_stop_stops_by_name_and_reports_log_removal():
    runtime = DockerRuntime()
    with (
        patch("archie_orchestrator.docker.stop_container") as stop,
        patch("archie_orchestrator.docker.list_sessions") as ls,
        patch(
            "archie_orchestrator.lifecycle._remove_empty_session_log", return_value=True
        ) as remove,
    ):
        assert runtime.stop(SID) is True
    stop.assert_called_once_with(f"archie-{SID}")
    ls.assert_not_called()  # no docker ps (D19)
    remove.assert_called_once_with(SID)

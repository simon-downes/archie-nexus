"""Tests for session titles in the tracker: triggers, schedule, precedence, limits (083)."""

import asyncio
import threading
from unittest.mock import patch

import pytest
from archie_orchestrator.tracker import TITLE_CONCURRENCY, title_due

from tests.session_fakes import (
    PORT,
    SID,
    FakeAgent,
    FakeRuntime,
    FakeTitler,
    handshake_frame,
    make_tracker,
    record,
    turn_frame,
    user_frame,
)


async def until(predicate, timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def idle(tracker):
    """Wait until no title job is running."""
    await until(lambda: not tracker._title_tasks)


@pytest.fixture
def log(session_logs):
    """The session's log, with a first prompt and reply (ARCHIE_HOME_DIR is tmp_path)."""
    w = session_logs
    return w.write(SID, [w.user("add a workspace api", turn=1), w.assistant("Added it.", turn=1)])


def _tracker(titler, *records):
    return make_tracker(records=list(records) or [record(SID)], titler=titler)


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def test_title_due_turns():
    due = [turn for turn in range(1, 60) if title_due(turn)]
    assert due == [1, 3, 8, 15, 25, 35, 45, 55]


async def test_the_first_prompt_titles_the_session(log):
    titler = FakeTitler("Workspaces API")
    tracker = _tracker(titler)
    published = []
    tracker.add_listener(published.append)

    await tracker._on_frame(SID, user_frame(turn=1))
    await idle(tracker)

    current = tracker.live(SID)
    assert (current.name, current.name_source) == ("Workspaces API", "model")
    assert published[-1] == current
    assert tracker.store.get(SID) == current
    [(session_id, title_input)] = titler.calls
    assert session_id == SID
    assert (title_input.workspace, title_input.current_title) == ("proj", None)
    assert title_input.first_request == "add a workspace api"
    await tracker.close()


async def test_turn_ends_refresh_a_title_on_schedule(log):
    titler = FakeTitler()
    tracker = _tracker(titler, record(SID, name="Old", name_source="model"))
    called = []

    for turn in range(1, 27):
        before = len(titler.calls)
        await tracker._on_frame(SID, turn_frame("complete", turn=turn))
        await idle(tracker)
        if len(titler.calls) > before:
            called.append(turn)

    assert called == [1, 3, 8, 15, 25]
    assert titler.calls[0][1].current_title == "Old"
    await tracker.close()


async def test_later_prompts_and_failed_turns_do_not_trigger(log):
    titler = FakeTitler()
    tracker = _tracker(titler, record(SID, name="Old", name_source="model"))

    for frame in (
        user_frame(turn=3),
        turn_frame("error", turn=3),
        turn_frame("interrupted", turn=3),
        turn_frame("complete", scope="tc-1", turn=3),
    ):
        await tracker._on_frame(SID, frame)
    await idle(tracker)

    assert titler.calls == []
    await tracker.close()


async def test_an_untitled_session_retries_at_every_turn_end(log):
    titler = FakeTitler(None, None, "Workspaces API")
    tracker = _tracker(titler)

    for turn in (2, 4, 5, 6):
        await tracker._on_frame(SID, turn_frame("complete", turn=turn))
        await idle(tracker)

    # Turns 2 and 4 failed, 5 succeeded; 6 isn't due once titled.
    assert len(titler.calls) == 3
    assert tracker.live(SID).name == "Workspaces API"
    await tracker.close()


async def test_titles_follow_the_agent_stream(log):
    titler = FakeTitler("Workspaces API")
    tracker = make_tracker({SID: PORT}, runtime=FakeRuntime(alive=False), titler=titler)
    agent = FakeAgent([handshake_frame(), user_frame(turn=1), 0.3])

    with patch("archie_orchestrator.tracker.websockets.connect", agent.connect()):
        await tracker._follow(SID)
    await idle(tracker)

    stored = tracker.store.get(SID)
    assert (stored.name, stored.name_source, stored.status) == (
        "Workspaces API",
        "model",
        "exited",
    )
    await tracker.close()


# ---------------------------------------------------------------------------
# Precedence and failures
# ---------------------------------------------------------------------------


async def test_a_user_named_session_is_never_titled(log):
    titler = FakeTitler()
    tracker = _tracker(titler, record(SID, name="Mine", name_source="user"))

    await tracker._on_frame(SID, user_frame(turn=1))
    await tracker._on_frame(SID, turn_frame("complete", turn=1))
    await idle(tracker)

    assert titler.calls == []
    assert tracker.live(SID).name == "Mine"
    await tracker.close()


async def test_titles_off_makes_no_calls(log):
    titler = FakeTitler(enabled=False)
    tracker = _tracker(titler)

    await tracker._on_frame(SID, user_frame(turn=1))
    await idle(tracker)

    assert titler.calls == []
    assert tracker.live(SID).name_source == "id"
    await tracker.close()


async def test_a_rename_during_the_call_wins(log):
    gate = threading.Event()
    titler = FakeTitler("Model title", gate=gate)
    tracker = _tracker(titler)

    await tracker._on_frame(SID, user_frame(turn=1))
    await until(lambda: titler.active == 1)
    await tracker.patch(SID, name="Mine")
    gate.set()
    await idle(tracker)

    current = tracker.live(SID)
    assert (current.name, current.name_source) == ("Mine", "user")
    assert tracker.store.get(SID) == current
    await tracker.close()


@pytest.mark.parametrize("result", [None, "KEEP"])
async def test_a_failure_or_keep_keeps_the_previous_title(log, result):
    titler = FakeTitler(result)
    tracker = _tracker(titler, record(SID, name="Old", name_source="model"))
    published = []
    tracker.add_listener(published.append)

    await tracker._on_frame(SID, turn_frame("complete", turn=3))
    await idle(tracker)

    assert (tracker.live(SID).name, tracker.live(SID).name_source) == ("Old", "model")
    assert [(p.name, p.name_source) for p in published] == [("Old", "model")]  # unseen only
    assert len(titler.calls) == 1
    await tracker.close()


async def test_an_unreadable_log_makes_no_call():
    titler = FakeTitler()
    tracker = _tracker(titler)  # no log written

    await tracker._on_frame(SID, user_frame(turn=1))
    await idle(tracker)

    assert titler.calls == []
    await tracker.close()


async def test_an_ended_session_is_titled_in_the_store(log):
    ended = record(SID, status="stopped", ended_at="2026-07-01T10:00:00+00:00")
    titler = FakeTitler("Workspaces API")
    tracker = _tracker(titler, ended)
    published = []
    tracker.add_listener(published.append)

    tracker.request_title(SID)
    await idle(tracker)

    stored = tracker.store.get(SID)
    assert (stored.name, stored.name_source, stored.status) == (
        "Workspaces API",
        "model",
        "stopped",
    )
    assert published == [stored]
    assert SID not in tracker._records
    await tracker.close()


def test_set_model_name_never_overwrites_a_user_name():
    tracker = make_tracker(records=[record(SID, name="Mine", name_source="user")])
    assert tracker.store.set_model_name(SID, "Model title") is None
    assert tracker.store.get(SID).name == "Mine"
    assert tracker.store.set_model_name("proj-01kx77jkz9", "Nobody") is None


# ---------------------------------------------------------------------------
# Coalescing, concurrency and shutdown
# ---------------------------------------------------------------------------


async def test_requests_during_a_call_coalesce_into_one_rerun(log):
    gate = threading.Event()
    titler = FakeTitler("First", "Second", gate=gate)
    tracker = _tracker(titler)

    tracker.request_title(SID)
    await until(lambda: titler.active == 1)
    for _ in range(3):
        tracker.request_title(SID)
    gate.set()
    await idle(tracker)

    assert len(titler.calls) == 2
    assert titler.calls[1][1].current_title == "First"
    assert tracker.live(SID).name == "Second"
    await tracker.close()


async def test_at_most_two_calls_run_at_once(session_logs):
    w = session_logs
    ids = [f"proj-01kx77jkz{n}" for n in range(4)]
    for session_id in ids:
        w.write(session_id, [w.user("hi", turn=1)])
    gate = threading.Event()
    titler = FakeTitler(gate=gate)
    tracker = make_tracker(records=[record(s) for s in ids], titler=titler)

    for session_id in ids:
        tracker.request_title(session_id)
    await until(lambda: titler.active == TITLE_CONCURRENCY)
    await asyncio.sleep(0.1)
    assert titler.active == TITLE_CONCURRENCY
    gate.set()
    await idle(tracker)

    assert titler.max_active == TITLE_CONCURRENCY == 2
    assert len(titler.calls) == 4
    await tracker.close()


async def test_close_cancels_title_jobs(log):
    gate = threading.Event()
    titler = FakeTitler("Late", gate=gate)
    tracker = _tracker(titler)

    tracker.request_title(SID)
    await until(lambda: titler.active == 1)
    await asyncio.wait_for(tracker.close(), 2)
    gate.set()

    assert tracker._title_tasks == {}
    assert tracker.store.get(SID).name_source == "id"

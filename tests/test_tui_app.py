"""The multi-session TUI: sidebar, switching, the pane, notices, reconnect, dialogs, palette (084).

Everything runs against ``FakeOrchestrator`` (``tests/tui_fakes.py``) with ``run_test``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import archie_cli.tui.animation as animation
import archie_cli.tui.app as tui_app
import msgspec
import pytest
from archie_cli.tui.app import EMPTY_TEXT, EmptyPane
from archie_cli.tui.client import OrchestratorError
from archie_cli.tui.conversation import ClientErrorMessage, ShellOutput, TurnStatus
from archie_cli.tui.input import MessageInput
from archie_cli.tui.pane import SessionHeader, SessionPane
from archie_cli.tui.session import (
    NOT_FOUND_TEXT,
    RECONNECTING_TEXT,
    ClientErrorEntry,
    ShellOutputEntry,
    TurnStatusEntry,
)
from archie_cli.tui.sidebar import (
    SessionRow,
    WorkspaceBand,
    age,
    branch_label,
    state_badge,
    state_glyph,
    where,
)
from archie_cli.tui.status import StatusBar
from archie_cli.tui.transcript_view import WINDOW_ROWS, TranscriptView
from archie_shared.events import (
    AssistantMessage,
    SessionStatus,
    ToolCall,
    ToolResult,
    TurnComplete,
    UserMessage,
)
from archie_shared.transcript import TranscriptBuilder
from archie_shared.workspaces import SessionLocation
from textual.app import App

from tests.tui_fakes import (
    FakeOrchestrator,
    apply_events,
    attached,
    make_app,
    make_record,
    settle,
)

SIZE = (170, 45)


def _at(minute: int) -> str:
    return f"2026-11-12T10:{minute:02d}:00+00:00"


def _client(*records, logs=None) -> FakeOrchestrator:
    return FakeOrchestrator(logs=logs, sessions=list(records))


def _user(event_id: str, content: str, turn: int = 1) -> UserMessage:
    return UserMessage(id=event_id, turn=turn, scope=None, content=content)


def _assistant(event_id: str, content: str, turn: int = 1) -> AssistantMessage:
    return AssistantMessage(
        id=event_id,
        turn=turn,
        iteration=1,
        scope=None,
        request_id=f"r-{event_id}",
        content=content,
        interrupted=False,
    )


def _sidebar_items(app) -> list[str]:
    """Bands and rows of the session list in display order."""
    items = []
    for child in app.sidebar.sessions.children:
        if isinstance(child, WorkspaceBand) and child.parent is not None:
            items.append(f"[{child.workspace} {child.count}]")
        elif isinstance(child, SessionRow):
            items.append(child.record.session_id)
    return items


def _row_text(app, session_id: str) -> str:
    return app.sidebar.sessions.rows[session_id].render().plain


# --- Sidebar --------------------------------------------------------------------------


async def test_sidebar_groups_by_workspace_and_orders_oldest_first():
    client = _client(
        make_record("beta-02", started_at=_at(2)),
        make_record("alpha-03", started_at=_at(3)),
        make_record("beta-01", started_at=_at(1)),
        make_record("alpha-01", started_at=_at(4)),
    )
    app = make_app(client, focus=None)
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot)
        assert _sidebar_items(app) == [
            "[alpha 2]",
            "alpha-03",
            "alpha-01",
            "[beta 2]",
            "beta-01",
            "beta-02",
        ]
        assert app.sidebar.sessions.order == ["alpha-03", "alpha-01", "beta-01", "beta-02"]
        # A new workspace gets its band in name order.
        client.publish(make_record("aardvark-01", started_at=_at(5)))
        await settle(pilot, lambda: "aardvark-01" in app.sidebar.sessions.rows)
        assert _sidebar_items(app)[:2] == ["[aardvark 1]", "aardvark-01"]


async def test_session_event_updates_its_row_in_place():
    client = _client(make_record("ws-a", started_at=_at(1)), make_record("ws-b", started_at=_at(2)))
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-a")
        rows = app.sidebar.sessions.rows
        before_a, before_b = rows["ws-a"], rows["ws-b"]
        client.publish(make_record("ws-b", status="blocked", started_at=_at(2), name="Fix it"))
        await settle(pilot, lambda: rows["ws-b"].record.status == "blocked")
        assert app.sidebar.sessions.rows["ws-b"] is before_b
        assert app.sidebar.sessions.rows["ws-a"] is before_a
        text = _row_text(app, "ws-b")
        assert "? Fix it" in text
        assert _sidebar_items(app) == ["[ws 2]", "ws-a", "ws-b"]


async def test_ended_session_disappears_unless_it_is_on_screen():
    client = _client(make_record("ws-a", started_at=_at(1)), make_record("ws-b", started_at=_at(2)))
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-a")
        client.publish(make_record("ws-b", status="stopped", started_at=_at(2)))
        await settle(pilot, lambda: "ws-b" not in app.sidebar.sessions.rows)
        assert _sidebar_items(app) == ["[ws 1]", "ws-a"]

        client.publish(make_record("ws-a", status="exited", started_at=_at(1)))
        await settle(pilot, lambda: app.sidebar.sessions.rows["ws-a"].record.ended)
        assert _sidebar_items(app) == ["[ws 1]", "ws-a"]  # still shown: it is on screen
        assert "· ws-a" in _row_text(app, "ws-a")
        assert "0 running" in str(app.sidebar.info.render())


def test_one_glyph_and_badge_per_status():
    expected = {
        "starting": ("◌", "STARTING"),
        "working": ("⠋", "WORKING"),
        "blocked": ("?", "NEEDS AN ANSWER"),
        "error": ("✕", "TURN FAILED"),
        "idle": ("●", "READY"),
        "stopping": ("◌", "STOPPING"),
        "stopped": ("·", "ENDED"),
        "exited": ("·", "ENDED"),
        "failed": ("✕", "FAILED"),
    }
    for status, (glyph, badge) in expected.items():
        record = make_record(status=status)
        assert state_glyph(record).plain == glyph, status
        assert state_badge(record)[0] == badge, status
    unseen = make_record(status="idle", unseen=True)
    assert state_badge(unseen)[0] == "NEW REPLY"
    # Working spins; failed is muted, a failed turn is red.
    assert state_glyph(make_record(status="working"), 1).plain == "⠙"
    assert str(state_glyph(make_record(status="failed")).style) != str(
        state_glyph(make_record(status="error")).style
    )


def test_age_is_compact():
    now = datetime(2026, 11, 12, 12, 0, 0, tzinfo=UTC)
    assert age("2026-11-12T11:59:55+00:00", now) == "now"
    assert age("2026-11-12T11:59:15+00:00", now) == "45s"
    assert age("2026-11-12T11:48:00+00:00", now) == "12m"
    assert age("2026-11-12T09:00:00+00:00", now) == "3h"
    assert age("2026-11-07T12:00:00+00:00", now) == "5d"
    assert age(None, now) == "—"


async def test_row_content_marks_the_session_on_screen():
    client = _client(
        make_record("ws-a", started_at=_at(1), last_activity_at=_at(3), name="Add a sidebar"),
        make_record("ws-b", started_at=_at(2)),
    )
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-a")
        first, second = _row_text(app, "ws-a").split("\n")
        assert first.startswith("▌ ● Add a sidebar")
        assert first.rstrip().endswith(age(_at(3)))
        assert second == "▌   no repo"
        assert _row_text(app, "ws-b").split("\n")[1] == "    no repo"
        width = app.sidebar.sessions.rows["ws-a"].size.width
        assert len(first) == width


def test_row_line_two_shows_the_repo_and_branch():
    sha = "0123456789abcdef0123456789abcdef01234567"
    cases = {
        "repo": (SessionLocation(repo="archie-nexus", branch="main"), "⎇ archie-nexus · main"),
        "nested": (SessionLocation(repo="group/lib", branch="feat/x"), "⎇ lib · feat/x"),
        "workspace": (SessionLocation(repo="", branch="main"), "⎇ proj · main"),
        "detached": (SessionLocation(repo="app", commit=sha), "⎇ app · 0123456"),
        "unknown": (SessionLocation(repo="app"), "⎇ app · ?"),
        "worktree": (
            SessionLocation(repo="app", worktree="app/.git/wt/x", branch="plan/x", missing=True),
            "⎇ app · plan/x",
        ),
        "none": (None, "  no repo"),
    }
    for case, (location, expected) in cases.items():
        assert where(make_record("proj-01", location=location)).plain == expected, case
    assert branch_label(None) is None
    assert branch_label(SessionLocation(repo="", commit=sha)) == "0123456"


async def test_rows_show_the_location_from_the_feed():
    client = _client(
        make_record("ws-a", started_at=_at(1), location=SessionLocation(repo="app", branch="main")),
        make_record("ws-b", started_at=_at(2)),
    )
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-a")
        assert _row_text(app, "ws-a").split("\n")[1] == "▌ ⎇ app · main"
        assert _row_text(app, "ws-b").split("\n")[1] == "    no repo"
        moved = SessionLocation(repo="", branch=None, commit="f" * 40)
        client.publish(make_record("ws-b", started_at=_at(2), location=moved))
        await settle(pilot, lambda: app.sidebar.sessions.rows["ws-b"].record.location == moved)
        assert _row_text(app, "ws-b").split("\n")[1] == "  ⎇ ws · fffffff"


async def test_info_line_and_web_link():
    client = _client(make_record("ws-a"), make_record("ws-b", started_at=_at(5)))
    app = make_app(client, focus=None)
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot)
        info = str(app.sidebar.info.render())
        assert "2 running · local ↗" in info
        with patch("archie_cli.tui.app.webbrowser.open") as opened:
            app.action_open_web()
        opened.assert_called_once_with("http://127.0.0.1:7600/")


# --- Initial focus --------------------------------------------------------------------


async def test_focus_shows_the_named_session():
    client = _client(make_record("ws-a", started_at=_at(1)), make_record("ws-b", started_at=_at(2)))
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot)
        assert app.current == "ws-a"


@pytest.mark.parametrize("focus", [None, "ws-gone"], ids=["no-focus", "unknown-focus"])
async def test_without_a_known_focus_the_newest_live_session_shows(focus):
    client = _client(
        make_record("ws-a", started_at=_at(1)),
        make_record("ws-b", started_at=_at(3)),
        make_record("ws-c", started_at=_at(2)),
    )
    app = make_app(client, focus=focus)
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot)
        assert app.current == "ws-b"


async def test_no_live_sessions_shows_the_empty_pane():
    app = make_app(FakeOrchestrator(), focus=None)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, lambda: str(app.query_one(EmptyPane).render()) == EMPTY_TEXT)
        assert not app.query(SessionPane)
        assert app.current is None
        assert "No sessions" in str(app.sidebar.sessions.query_one(".session-list-empty").render())
        # A session that starts later is listed; clicking it shows it.
        app._client.publish(make_record("ws-new"))
        await settle(pilot, lambda: "ws-new" in app.sidebar.sessions.rows)
        await pilot.click(app.sidebar.sessions.rows["ws-new"])
        await attached(app, pilot, "ws-new")
        assert not app.query(EmptyPane)


# --- Switching ------------------------------------------------------------------------


def _three(**logs):
    return _client(
        make_record("ws-a", started_at=_at(1)),
        make_record("ws-b", started_at=_at(2)),
        make_record("ws-c", started_at=_at(3)),
        logs=logs,
    )


async def test_switching_back_reuses_the_controller_without_refetching():
    client = _three(**{"ws-a": [_user("u1", "hello a")], "ws-b": [_user("u2", "hello b")]})
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-a")
        await app.switch_to("ws-b")
        await attached(app, pilot, "ws-b")
        await app.switch_to("ws-a")
        pane = await attached(app, pilot, "ws-a")
        assert client.full_loads("ws-a") == 1
        assert client.opened("ws-a") == 1
        assert [row.key for row in pane.conversation.rows] == ["msg-u1"]
        assert pane.header.record.session_id == "ws-a"


async def test_rapid_switches_leave_exactly_one_pane():
    client = _three()
    client.records["ws-d"] = make_record("ws-d", started_at=_at(4))
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-a")
        # The first switch runs at once; of those waiting for it, only the newest runs.
        await asyncio.gather(
            app.switch_to("ws-b"),
            app.switch_to("ws-c"),
            app.switch_to("ws-a"),
            app.switch_to("ws-d"),
        )
        await attached(app, pilot, "ws-d")
        assert len(app.query(SessionPane)) == 1
        assert app.pane.header.record.session_id == "ws-d"
        assert "ws-c" not in app.controllers  # its switch was overtaken before it ran
        assert list(app.controllers) == ["ws-a", "ws-b", "ws-d"]
        # Keys queue switches the same way.
        for _ in range(5):
            await pilot.press("alt+down")
        await settle(
            pilot,
            lambda: (
                not app._switch_lock.locked()
                and app.pane is not None
                and app.pane.controller is not None
            ),
        )
        assert len(app.query(SessionPane)) == 1
        assert app.pane.header.record.session_id == app.current


_LONG_REPLY = "\n\n".join(
    f"Paragraph {n} with `code` and **bold**.\n\n- one\n- two\n\n```python\nx = {n}\n```"
    for n in range(4)
)


async def test_switching_never_draws_the_widgets_of_a_pane_being_removed():
    # Removing a pane prunes its widgets one by one, and the layout drops them only
    # once the whole pane is gone. A frame drawn meanwhile rendered the already-pruned
    # MessageInput and raised KeyError 'text-area--gutter' (a TextArea's component
    # styles are cleared when it is pruned).
    events = []
    for n in range(1, 61):
        events += [_user(f"u{n}", f"q {n}", turn=n), _assistant(f"a{n}", _LONG_REPLY, turn=n)]
    client = _three(**{"ws-a": events})
    app = make_app(client, focus="ws-a")
    drawn_while_pruning: list[str] = []
    original = MessageInput.render_lines

    def render_lines(self, crop):
        if self._pruning:
            drawn_while_pruning.append(repr(self))
        return original(self, crop)

    with patch.object(MessageInput, "render_lines", render_lines):
        async with app.run_test(size=SIZE) as pilot:
            await attached(app, pilot, "ws-a")
            await pilot.pause()
            for _ in range(5):
                for target in ("ws-b", "ws-a"):
                    await app.switch_to(target)
                    await attached(app, pilot, target)
                    await pilot.pause()
            assert len(app.query(SessionPane)) == 1
    assert drawn_while_pruning == []


async def test_client_rows_and_the_draft_survive_a_switch():
    client = _three(**{"ws-a": [_user("u1", "first"), _assistant("a1", "reply")]})
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot, "ws-a")
        app.controller.client_error("Something local")
        pane.input.focus()
        await pilot.press(*"half typed")
        pane.input._history = ["earlier"]
        await app.switch_to("ws-b")
        await attached(app, pilot, "ws-b")
        assert app.pane.input.text == ""
        await app.switch_to("ws-a")
        pane = await attached(app, pilot, "ws-a")
        await pilot.pause()
        errors = pane.conversation.query(ClientErrorMessage)
        assert len(errors) == 1 and errors.first().get_copy_text() == "Something local"
        keys = [row.key for row in pane.conversation.rows]
        assert keys[:2] == ["msg-u1", "msg-a1"] and keys[2].startswith("local-")
        assert pane.input.text == "half typed"
        assert pane.input._history == ["earlier"]


async def test_sidebar_click_alt_keys_and_numbers_switch():
    app = make_app(_three(), focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-a")
        await pilot.click(app.sidebar.sessions.rows["ws-c"])
        await attached(app, pilot, "ws-c")
        await pilot.press("alt+up")
        await attached(app, pilot, "ws-b")
        await pilot.press("alt+down", "alt+down")  # wraps past the last
        await attached(app, pilot, "ws-a")
        await pilot.press("alt+3")
        await attached(app, pilot, "ws-c")
        await pilot.press("alt+9")  # no ninth session
        await pilot.pause()
        assert app.current == "ws-c"
        assert app.sidebar.sessions.rows["ws-c"].current
        assert not app.sidebar.sessions.rows["ws-a"].current


async def test_sixth_visited_session_evicts_the_least_recent_evictable_controller():
    records = [make_record(f"ws-{n}", started_at=_at(n)) for n in range(1, 8)]
    client = _client(*records)
    app = make_app(client, focus="ws-1")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-1")
        app.controllers["ws-1"].answer_in_flight = "q1"  # an answer being sent: kept
        for n in range(2, 7):
            await app.switch_to(f"ws-{n}")
            await attached(app, pilot, f"ws-{n}")
        await settle(pilot, lambda: "ws-2" not in app.controllers)
        assert list(app.controllers) == ["ws-1", "ws-3", "ws-4", "ws-5", "ws-6"]
        await settle(pilot, lambda: not client.stream_for("ws-2").connected)
        client.stream_for("ws-2").disconnect.assert_awaited()
        assert client.stream_for("ws-3").connected

        app.controllers["ws-1"].answer_in_flight = None
        await app.switch_to("ws-7")
        await attached(app, pilot, "ws-7")
        await settle(pilot, lambda: "ws-1" not in app.controllers)
        assert list(app.controllers) == ["ws-3", "ws-4", "ws-5", "ws-6", "ws-7"]


async def test_tab_moves_between_the_filter_the_list_the_transcript_and_the_input():
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot)
        assert app.focused is pane.input
        await pilot.press("tab")
        assert app.focused is app.sidebar.filter
        await pilot.press("tab")
        assert app.focused is app.sidebar.sessions
        await pilot.press("tab")
        assert app.focused is pane.conversation
        await pilot.press("tab")
        assert app.focused is pane.input
        await pilot.press("shift+tab")
        assert app.focused is pane.conversation


# --- Session lifecycle in the pane -----------------------------------------------------


async def test_starting_session_waits_for_the_feed_before_connecting():
    client = _client(make_record("ws-a", status="starting"))
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, lambda: app.pane is not None)
        pane = app.pane
        assert "Starting…" in str(pane.loading_line.render())
        assert client.opened("ws-a") == 0 and not client.requests
        assert "STARTING" in pane.header.render().plain

        client.publish(make_record("ws-a", status="idle"))
        await attached(app, pilot, "ws-a")
        assert client.opened("ws-a") == 1
        assert not pane.loading_line.display
        assert "READY" in pane.header.render().plain


async def test_ended_session_loads_its_log_without_a_stream():
    client = _client(logs={"ws-a": [_user("u1", "old question")]})
    client.records["ws-a"] = make_record("ws-a", status="stopped")
    app = make_app(client, focus=None)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, lambda: app._snapshot_seen)
        assert app.current is None  # the snapshot holds live sessions only
        app.records["ws-a"] = client.records["ws-a"]
        await app.switch_to("ws-a")
        pane = await attached(app, pilot, "ws-a")
        assert client.opened("ws-a") == 0
        assert client.full_loads("ws-a") == 1
        assert [row.key for row in pane.conversation.rows] == ["msg-u1"]
        assert pane.input.disabled
        assert "ENDED" in pane.header.render().plain


async def test_loading_line_shows_until_the_first_replay_lands():
    gate = asyncio.Event()

    class SlowClient(FakeOrchestrator):
        async def events(self, session_id, after):
            await gate.wait()
            return await super().events(session_id, after)

    client = SlowClient(sessions=[make_record("s1")])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, lambda: app.pane is not None and client.opened("s1") == 1)
        assert app.pane.loading_line.display
        assert "Loading…" in str(app.pane.loading_line.render())
        gate.set()
        await attached(app, pilot)
        assert not app.pane.loading_line.display


async def test_a_turn_sends_shows_tools_question_and_status():
    from archie_cli.tui.transcript_view import ToolRow
    from archie_shared.events import Handshake, PendingQuestion, QuestionOption

    client = _client(make_record("s1", location=SessionLocation(repo="app", branch="feat/x")))
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot)
        assert pane.status.git_branch == "feat/x"
        await pilot.press(*"hello", "enter")
        await settle(pilot, lambda: client.latest.sent)
        assert client.latest.sent == [("message", "hello")]
        assert pane.input.disabled

        call = ToolCall(
            id="c1",
            turn=1,
            iteration=1,
            scope=None,
            request_id="r1",
            tool_use_id="t1",
            name="read",
            input={"path": "a.py"},
        )
        question = PendingQuestion(
            id="q1", question="Which?", options=[QuestionOption(label="A")], recommended=0
        )
        await apply_events(
            app,
            pilot,
            Handshake(id="h", protocol_version=4, session_id="agent-s1"),
            _user("u1", "hello"),
            call,
            SessionStatus(
                id="st", model_key="m", git_branch="main", state="blocked", question=question
            ),
        )
        assert pane.conversation.query(ToolRow)
        assert pane.question.display and not pane.input.disabled
        # The agent's own git_branch is not the source: the record's location is (D31).
        assert pane.status.git_branch == "feat/x"
        await pilot.press("1")
        await settle(pilot, lambda: len(client.latest.sent) == 2)
        assert client.latest.sent[1][1].option == 0
        await apply_events(
            app,
            pilot,
            ToolResult(
                id="tr",
                turn=1,
                iteration=1,
                scope=None,
                request_id="r1",
                tool_use_id="t1",
                content="ok",
                is_error=False,
                duration_ms=3,
            ),
            TurnComplete(id="tc", turn=1, scope=None, stop_reason="end_turn"),
        )
        assert not pane.input.disabled
        assert not pane.question.display
        # The header shows the ID of an unnamed session; the status bar shows it only
        # once the session has a name (D31).
        assert pane.query_one(StatusBar).session_id == ""
        client.publish(make_record("s1", name="Fix the parser"))
        await settle(pilot, lambda: pane.status.session_id == "s1")
        detached = SessionLocation(repo="app", commit="abcdef0123" * 4)
        client.publish(make_record("s1", location=detached))
        await settle(pilot, lambda: pane.status.git_branch == "abcdef0")
        client.publish(make_record("s1"))
        await settle(pilot, lambda: pane.status.git_branch == "—")


async def test_header_follows_the_feed_and_animates_while_working():
    client = _client(make_record("s1"))
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot)
        assert "● s1" in pane.query_one(SessionHeader).render().plain
        client.publish(make_record("s1", status="working", name="Busy"))
        await settle(pilot, lambda: "WORKING" in pane.header.render().plain)
        frames = set()
        for _ in range(20):
            await asyncio.sleep(animation.INTERVAL)
            frames.add(pane.header.render().plain[0])
            if len(frames) > 1:
                break
        assert len(frames) > 1  # the spinner moves
        client.publish(make_record("s1", status="idle", unseen=True, name="Busy"))
        await settle(pilot, lambda: "NEW REPLY" in pane.header.render().plain)


async def test_feed_reconnects_and_its_snapshot_resyncs(monkeypatch):
    monkeypatch.setattr(tui_app, "FEED_RETRY_DELAYS", (0.01,))
    client = _client(make_record("ws-a", started_at=_at(1)), make_record("ws-b", started_at=_at(2)))
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-a")
        client.drop_feed()
        # Changed while the feed was down: only the next snapshot carries it.
        client.records["ws-b"] = make_record("ws-b", status="blocked", started_at=_at(2))
        await settle(pilot, lambda: client.feed_connects == 2)
        await settle(pilot, lambda: app.sidebar.sessions.rows["ws-b"].record.status == "blocked")
        assert app.current == "ws-a"
        assert len(app.query(SessionPane)) == 1


# --- TranscriptView.load ----------------------------------------------------------------


class _ViewHost(App):
    def compose(self):
        yield TranscriptView()


def _turn_status(anchor):
    return TurnStatusEntry(anchor, 1.0, 1, 0, 0, 1, 0.01, None)


async def test_load_rebuilds_rows_and_puts_local_entries_after_their_anchors():
    builder = TranscriptBuilder()
    for event in (
        _user("u1", "one"),
        _assistant("a1", "first reply"),
        _user("u2", "two", turn=2),
        _assistant("a2", "second reply", turn=2),
    ):
        builder.apply(event)
    locals_ = [
        ClientErrorEntry(None, "before anything"),
        _turn_status("msg-a1"),
        ShellOutputEntry("msg-u2", "ls", "out", 0),
        ClientErrorEntry("gone-node", "anchor removed"),
        ClientErrorEntry("msg-a2", "after the last reply"),
    ]
    app = _ViewHost()
    async with app.run_test() as pilot:
        view = app.query_one(TranscriptView)
        view.load(builder, locals_)
        await pilot.pause()
        kinds = [
            row.key if row.node is not None else type(row.widget).__name__ for row in view.rows
        ]
        assert kinds == [
            "ClientErrorMessage",
            "msg-u1",
            "msg-a1",
            "TurnStatus",
            "msg-u2",
            "ShellOutput",
            "msg-a2",
            "ClientErrorMessage",
            "ClientErrorMessage",
        ]
        texts = [w.get_copy_text() for w in view.query(ClientErrorMessage)]
        assert texts == ["before anything", "after the last reply", "anchor removed"]
        assert view.query(TurnStatus) and view.query(ShellOutput)
        assert view.window_is_latest


async def test_load_respects_the_window():
    builder = TranscriptBuilder()
    for n in range(1, 151):
        builder.apply(_user(f"u{n}", f"q {n}", turn=n))
        builder.apply(_assistant(f"a{n}", f"r {n}", turn=n))
    app = _ViewHost()
    async with app.run_test() as pilot:
        view = app.query_one(TranscriptView)
        view.load(builder)
        await pilot.pause()
        assert len(view.rows) == 300
        mounted = [row for row in view.rows if row.widget is not None]
        assert len(mounted) == WINDOW_ROWS
        assert view.window == (300 - WINDOW_ROWS, 300)


# --- Odds and ends ----------------------------------------------------------------------


async def test_input_is_scoped_to_the_pane():
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot)
        assert pane.query_one(MessageInput) is pane.input
        assert not app.query("#input")


async def test_patch_is_recorded_by_the_fake():
    client = _client(make_record("s1", unseen=True))
    record = await client.patch("s1", {"unseen": False})
    assert client.patches == [("s1", {"unseen": False})]
    assert record.unseen is False
    assert msgspec.structs.asdict(client.records["s1"])["unseen"] is False


# --- HttpOrchestrator: feed and PATCH ----------------------------------------------------


def _mock_httpx(monkeypatch, handler) -> None:
    import archie_cli.tui.client as client_module
    import httpx

    real = httpx.AsyncClient
    monkeypatch.setattr(
        client_module.httpx,
        "AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs),
    )


async def test_http_feed_parses_snapshot_and_session_events(monkeypatch):
    import httpx
    from archie_cli.tui.client import HttpOrchestrator

    first = msgspec.json.encode({"sessions": [make_record("ws-a")]}).decode()
    second = msgspec.json.encode(make_record("ws-a", status="working")).decode()
    body = (
        f": ping\n\nevent: snapshot\ndata: {first}\n\n"
        ": ping\n\n"
        f"event: session\ndata: {second}\n\n"
        "event: other\ndata: {}\n\n"
        "event: session\ndata: not json\n\n"
    )
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    _mock_httpx(monkeypatch, handler)
    events = [event async for event in HttpOrchestrator("http://orch:7600").feed()]
    assert paths == ["/api/sessions/feed"]
    assert [kind for kind, _ in events] == ["snapshot", "session"]
    assert [r.session_id for r in events[0][1]] == ["ws-a"]
    assert events[1][1].status == "working"


async def test_http_feed_and_patch_report_failures(monkeypatch):
    import httpx
    from archie_cli.tui.client import HttpOrchestrator, OrchestratorError

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content))
        if request.method == "PATCH" and request.url.path.endswith("/ws-a"):
            return httpx.Response(200, content=msgspec.json.encode(make_record("ws-a", name="N")))
        return httpx.Response(404, json={"error": "no"})

    _mock_httpx(monkeypatch, handler)
    client = HttpOrchestrator("http://orch:7600")
    record = await client.patch("ws-a", {"name": "N"})
    assert record.name == "N"
    assert seen[0] == ("PATCH", "/api/sessions/ws-a", b'{"name":"N"}')
    with pytest.raises(OrchestratorError) as patch_error:
        await client.patch("ws-x", {"unseen": False})
    assert patch_error.value.status_code == 404
    with pytest.raises(OrchestratorError) as feed_error:
        async for _ in client.feed():
            pass
    assert feed_error.value.status_code == 404


async def test_http_create_workspaces_and_delete_carry_the_servers_error_text(monkeypatch):
    import httpx
    from archie_cli.tui.client import HttpOrchestrator, RequestRefused

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content))
        if request.method == "POST":
            if b"nope" in request.content:
                return httpx.Response(400, json={"error": "No workspace named 'nope'"})
            record = make_record("ws-new", status="starting", backend="kiro")
            return httpx.Response(202, content=msgspec.json.encode(record))
        if request.url.path == "/api/workspaces":
            return httpx.Response(200, json=[{"name": "alpha", "live_sessions": 1}])
        if request.method == "DELETE" and request.url.path.endswith("/ws-a"):
            return httpx.Response(
                200, content=msgspec.json.encode(make_record("ws-a", status="stopped"))
            )
        if request.method == "DELETE":
            return httpx.Response(409, json={"error": "Session 'ws-b' is still starting"})
        return httpx.Response(500, text="boom")

    _mock_httpx(monkeypatch, handler)
    client = HttpOrchestrator("http://orch:7600")
    record = await client.create("ws", "kiro")
    assert (record.session_id, record.status) == ("ws-new", "starting")
    assert seen[0] == ("POST", "/api/sessions", b'{"workspace":"ws","backend":"kiro"}')
    with pytest.raises(RequestRefused, match="No workspace named 'nope'") as refused:
        await client.create("nope", "native")
    assert refused.value.status_code == 400
    assert [w.name for w in await client.workspaces()] == ["alpha"]
    assert (await client.delete("ws-a")).status == "stopped"
    with pytest.raises(RequestRefused) as conflict:
        await client.delete("ws-b")
    assert (str(conflict.value), conflict.value.status_code) == (
        "Session 'ws-b' is still starting",
        409,
    )
    with pytest.raises(OrchestratorError) as failed:
        await client.session("ws-c")
    assert not isinstance(failed.value, RequestRefused)
    assert str(failed.value) == "session request failed: 500"


# --- The pane's look (M4) ---------------------------------------------------------------


def _call(tool_use_id: str = "t1", name: str = "read", tool_input: dict | None = None) -> ToolCall:
    return ToolCall(
        id=f"c-{tool_use_id}",
        turn=1,
        iteration=1,
        scope=None,
        request_id="r1",
        tool_use_id=tool_use_id,
        name=name,
        input=tool_input if tool_input is not None else {"path": "/workspace/app/a.py"},
    )


def _result(tool_use_id: str = "t1", content: str = "File: a.py (3 lines)") -> ToolResult:
    return ToolResult(
        id=f"r-{tool_use_id}",
        turn=1,
        iteration=1,
        scope=None,
        request_id="r1",
        tool_use_id=tool_use_id,
        content=content,
        is_error=False,
        duration_ms=6,
        result_lines=len(content.splitlines()),
    )


async def test_user_message_has_a_bar_and_no_you_header():
    from archie_cli.tui.conversation import UserMessage as UserRow
    from textual.widgets import Static

    client = _client(make_record("s1"), logs={"s1": [_user("u1", "hello there")]})
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot)
        await settle(pilot, lambda: pane.conversation.query(UserRow))
        message = pane.conversation.query_one(UserRow)
        assert str(message.query_one(Static).render()) == "hello there"
        assert "You" not in str(message.query_one(Static).render())
        kind, colour = message.styles.border_left
        assert kind == "thick" and colour.a == 1


async def test_tool_rows_are_two_lines_in_the_pane():
    from archie_cli.tui.transcript_view import ToolRow

    client = _client(make_record("s1"))
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot)
        await apply_events(app, pilot, _user("u1", "go"), _call(), _result())
        await pilot.pause()
        row = pane.conversation.query_one(ToolRow)
        assert row.size.height == 2
        assert row.render().plain == "● Read · 3 lines · 6ms\n╰ app/a.py"


async def test_activity_line_keeps_its_height_and_follows_the_turn():
    from archie_shared.events import IterationStart, TextDelta

    client = _client(make_record("s1"))
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot)
        line = pane.activity
        await pilot.pause()
        heights = (line.outer_size.height, pane.conversation.outer_size.height)
        assert heights[0] == 2
        assert line.activity is None and line.render().plain == ""

        await pilot.press(*"go", "enter")
        await settle(pilot, lambda: client.latest.sent)
        assert line.activity == "Thinking"
        await apply_events(
            app, pilot, _user("u1", "go"), IterationStart(id="i1", turn=1, iteration=1, scope=None)
        )
        assert line.render().plain.endswith("Thinking… (esc to cancel)")
        assert (line.outer_size.height, pane.conversation.outer_size.height) == heights

        delta = TextDelta(id="d1", turn=1, iteration=1, scope=None, request_id="r1", text="Hm")
        await apply_events(app, pilot, delta)
        assert line.activity == "Responding"
        assert "Responding…" in line.render().plain

        await apply_events(app, pilot, _call())
        assert line.activity == "Working"
        assert "Working…" in line.render().plain
        assert (line.outer_size.height, pane.conversation.outer_size.height) == heights

        await apply_events(
            app, pilot, _result(), TurnComplete(id="tc", turn=1, scope=None, stop_reason="end_turn")
        )
        assert line.activity is None and line.render().plain == ""
        assert line.outer_size.height == heights[0]


async def test_animation_clock_runs_only_while_something_animates():
    from archie_cli.tui.transcript_view import ToolRow

    client = _client(make_record("s1"))
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot)
        clock = app.animation
        await pilot.pause()
        assert not clock.running and not clock.widgets

        # A turn: the activity line, then a running tool row, register.
        await pilot.press(*"go", "enter")
        await settle(pilot, lambda: clock.running)
        assert clock.widgets == {pane.activity}
        frame = clock.frame
        await asyncio.sleep(animation.INTERVAL * 4)
        assert clock.frame > frame
        await apply_events(app, pilot, _user("u1", "go"), _call())
        row = pane.conversation.query_one(ToolRow)
        assert row in clock.widgets

        # A working session: its sidebar row and the pane's header.
        client.publish(make_record("s1", status="working"))
        sidebar_row = app.sidebar.sessions.rows["s1"]
        await settle(pilot, lambda: sidebar_row in clock.widgets)
        assert pane.header in clock.widgets

        # Everything stops: the clock stops with it.
        await apply_events(
            app, pilot, _result(), TurnComplete(id="tc", turn=1, scope=None, stop_reason="end_turn")
        )
        assert row not in clock.widgets and pane.activity not in clock.widgets
        assert clock.running
        client.publish(make_record("s1", status="idle"))
        await settle(pilot, lambda: not clock.running)
        assert not clock.widgets
        frame = clock.frame
        await asyncio.sleep(animation.INTERVAL * 4)
        assert clock.frame == frame


async def test_a_removed_pane_leaves_nothing_on_the_clock():
    client = _client(
        make_record("ws-a", status="working", started_at=_at(1)),
        make_record("ws-b", started_at=_at(2)),
    )
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot, "ws-a")
        assert pane.header in app.animation.widgets
        await app.switch_to("ws-b")
        await attached(app, pilot, "ws-b")
        # Only the working session's sidebar row animates now.
        assert app.animation.widgets == {app.sidebar.sessions.rows["ws-a"]}


# --- Notices, unseen and reconnect (M6) ---------------------------------------------


def _record_notices(app) -> list[tuple[str, str, str, str | None]]:
    """Wrap ``app.notice`` so every notice posted is recorded as (title, text, severity, id)."""
    posted: list[tuple[str, str, str, str | None]] = []
    original = app.notice

    def notice(title, text="", severity="information", **kwargs):
        posted.append((title, text, severity, kwargs.get("session_id")))
        original(title, text, severity, **kwargs)

    app.notice = notice
    return posted


def _question(text: str = "Which file should I change?"):
    from archie_shared.events import PendingQuestion, QuestionOption

    return PendingQuestion(id="q1", question=text, options=[QuestionOption(label="A")])


def _error_rows(pane) -> list[str]:
    return [w.get_copy_text() for w in pane.conversation.query(ClientErrorMessage)]


@pytest.fixture
def fast_retries(monkeypatch):
    """Reconnect the feed and streams after a few milliseconds."""
    from archie_cli.tui.session import SessionController

    monkeypatch.setattr(tui_app, "FEED_RETRY_DELAYS", (0.01,))
    monkeypatch.setattr(SessionController, "RECONNECT_DELAYS", (0.01,))


async def _until(predicate, timeout: float = 5.0) -> None:
    """Wait in real time until ``predicate()`` holds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


async def test_notice_per_transition_and_none_for_the_on_screen_session():
    client = _client(
        make_record("ws-a", started_at=_at(1)),
        make_record("ws-b", started_at=_at(2), name="Fix the parser"),
        make_record("ws-c", status="starting", started_at=_at(3), name="New one"),
    )
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-a")
        posted = _record_notices(app)
        long_question = "Which file " + "really " * 30 + "should I change?"
        steps = [
            make_record("ws-b", started_at=_at(2), name="Fix the parser", status="working"),
            make_record("ws-b", started_at=_at(2), name="Fix the parser", unseen=True),
            # The same state again (say, a new location): no second notice.
            make_record(
                "ws-b",
                started_at=_at(2),
                name="Fix the parser",
                unseen=True,
                location=SessionLocation(repo="app", branch="main"),
            ),
            make_record(
                "ws-b",
                started_at=_at(2),
                name="Fix the parser",
                status="blocked",
                question=_question(long_question),
            ),
            make_record("ws-b", started_at=_at(2), name="Fix the parser", status="error"),
            make_record("ws-b", started_at=_at(2), name="Fix the parser", status="stopped"),
            make_record(
                "ws-c", started_at=_at(3), name="New one", status="failed", error="no image"
            ),
            # The on-screen session: none of these notify.
            make_record("ws-a", started_at=_at(1), unseen=True),
            make_record("ws-a", started_at=_at(1), status="blocked", question=_question()),
            make_record("ws-a", started_at=_at(1), status="error"),
        ]
        for record in steps:
            client.publish(record)
        await settle(pilot, lambda: app.records["ws-a"].status == "error")
        assert [(title, text.split(":")[0], sev, sid) for title, text, sev, sid in posted] == [
            ("Fix the parser", "New reply", "information", "ws-b"),
            ("Fix the parser", "Needs an answer", "warning", "ws-b"),
            ("Fix the parser", "Turn failed", "error", "ws-b"),
            ("Fix the parser", "Session ended", "information", "ws-b"),
            ("New one", "Failed to start", "error", "ws-c"),
        ]
        question_text = posted[1][1]
        assert question_text.startswith("Needs an answer: Which file really")
        assert question_text.endswith("…") and len(question_text) <= len("Needs an answer: ") + 80
        assert posted[4][1] == "Failed to start: no image"
        # The newest three show as cards in the sidebar, bold title over the text.
        cards = app.sidebar.notices.notices
        assert [(n.title, n.text) for n in cards] == [
            ("Fix the parser", "Turn failed"),
            ("Fix the parser", "Session ended"),
            ("New one", "Failed to start: no image"),
        ]
        assert cards[-1].has_class("-error")
        assert str(cards[-1].render()) == "New one\nFailed to start: no image"


async def test_no_notices_from_the_initial_or_a_resync_snapshot(fast_retries):
    client = _client(
        make_record("ws-a", started_at=_at(1)),
        make_record("ws-b", started_at=_at(2), status="blocked", question=_question()),
    )
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        posted = _record_notices(app)
        await attached(app, pilot, "ws-a")
        client.drop_feed()
        # Changed while the feed was down: the re-sync snapshot carries it silently.
        client.records["ws-b"] = make_record("ws-b", started_at=_at(2), unseen=True)
        await settle(pilot, lambda: client.feed_connects == 2)
        await settle(pilot, lambda: app.records["ws-b"].unseen)
        assert posted == []
        assert not app.sidebar.notices.notices
        # Feed events after it notify again.
        client.publish(make_record("ws-b", started_at=_at(2), status="error"))
        await settle(pilot, lambda: bool(posted))
        assert [text for _, text, _, _ in posted] == ["Turn failed"]


async def test_clicking_a_notice_switches_to_its_session():
    client = _client(make_record("ws-a", started_at=_at(1)), make_record("ws-b", started_at=_at(2)))
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-a")
        client.publish(make_record("ws-b", started_at=_at(2), unseen=True))
        await settle(pilot, lambda: app.sidebar.notices.notices)
        (card,) = app.sidebar.notices.notices
        await pilot.pause()
        await pilot.click(card)
        await attached(app, pilot, "ws-b")
        await settle(pilot, lambda: not app.sidebar.notices.query("Notice"))
        # A notice that is not about a session only goes away.
        app.notice("Copied to clipboard")
        await pilot.pause()
        await pilot.click(app.sidebar.notices.notices[0])
        await settle(pilot, lambda: not app.sidebar.notices.query("Notice"))
        assert app.current == "ws-b"


async def test_at_most_three_notices_and_each_times_out():
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot)
        for n in range(5):
            app.notice(f"Title {n}", f"text {n}", timeout=30)
        await pilot.pause()
        assert [n.title for n in app.sidebar.notices.notices] == ["Title 2", "Title 3", "Title 4"]
        await settle(pilot, lambda: len(app.sidebar.notices.query("Notice")) == 3)
        # The stack sits between the session list and the key hints.
        children = list(app.sidebar.children)
        assert children.index(app.sidebar.notices) == children.index(app.sidebar.sessions) + 1
        app.notice("Brief", severity="warning", timeout=0.05)
        await settle(pilot, lambda: "Brief" in [n.title for n in app.sidebar.notices.notices])
        assert app.sidebar.notices.notices[-1].has_class("-warning")
        await _until(lambda: "Brief" not in [n.title for n in app.sidebar.notices.notices])


async def test_copy_posts_a_notice_not_a_toast():
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot)
        with (
            patch.object(app, "copy_to_clipboard") as copied,
            patch.object(app.screen, "get_selected_text", return_value="copy me"),
        ):
            app.action_copy_block()
        copied.assert_called_once_with("copy me")
        await pilot.pause()
        assert [n.title for n in app.sidebar.notices.notices] == ["Copied to clipboard"]
        assert not app._notifications


async def test_unseen_is_cleared_on_switch_and_on_update_while_shown_not_repeated():
    client = _client(
        make_record("ws-a", started_at=_at(1)),
        make_record("ws-b", started_at=_at(2), unseen=True),
    )
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-a")
        assert client.patches == []  # ws-a has no unseen; ws-b is not shown
        client.patch_gate = asyncio.Event()
        await app.switch_to("ws-b")
        await attached(app, pilot, "ws-b")
        await settle(pilot, lambda: client.patches)
        assert client.patches == [("ws-b", {"unseen": False})]

        # Feed updates while that PATCH runs, or for other sessions, don't repeat it.
        client.publish(make_record("ws-b", started_at=_at(2), unseen=True, name="Renamed"))
        client.publish(make_record("ws-a", started_at=_at(1), status="working"))
        await settle(pilot, lambda: app.records["ws-a"].status == "working")
        assert len(client.patches) == 1
        client.patch_gate.set()
        await settle(pilot, lambda: not app._seen_inflight)
        client.publish(make_record("ws-a", started_at=_at(1)))
        await settle(pilot, lambda: app.records["ws-a"].status == "idle")
        assert len(client.patches) == 1

        # The server's echo, then a later turn end while shown: one more PATCH.
        client.publish(make_record("ws-b", started_at=_at(2), name="Renamed"))
        client.publish(make_record("ws-b", started_at=_at(2), unseen=True, name="Renamed"))
        await settle(pilot, lambda: len(client.patches) == 2)
        assert client.patches[1] == ("ws-b", {"unseen": False})


async def test_a_failed_unseen_patch_is_logged_not_shown(caplog):
    client = _client(make_record("s1", unseen=True))
    client.patch = AsyncMock(side_effect=OrchestratorError("session update failed: 500", 500))
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot)
        await settle(pilot, lambda: client.patch.await_count == 1 and not app._seen_inflight)
        assert "Could not mark session s1 seen" in caplog.text
        assert not app.sidebar.notices.notices and _error_rows(pane) == []


async def test_reconnect_after_an_orchestrator_restart(fast_retries):
    # The orchestrator goes away: the feed and the stream drop and connects fail for a
    # while; the turn that ran meanwhile is only in the log.
    client = _client(make_record("ws-a"), logs={"ws-a": [_user("u1", "before")]})
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot, "ws-a")
        client.fail_feed(*(OrchestratorError("refused") for _ in range(4)))
        client.fail_connects(*(OSError("refused") for _ in range(4)))
        client.drop_feed()
        client.latest.drop(1006)
        client.logs["ws-a"] += [_user("u2", "during", turn=2), _assistant("a2", "r", turn=2)]
        client.records["ws-a"] = make_record("ws-a", status="idle", name="Back again")

        await _until(lambda: client.opened("ws-a") == 2 and not app.controller.reconnecting)
        await _until(lambda: client.feed_connects == 6 and app.records["ws-a"].name == "Back again")
        await pilot.pause()
        assert client.requests == [("ws-a", None), ("ws-a", "u1")]  # an ``after`` replay
        keys = [row.key for row in pane.conversation.rows]
        assert [k for k in keys if k.startswith("msg-")] == ["msg-u1", "msg-u2", "msg-a2"]
        assert _error_rows(pane) == [RECONNECTING_TEXT]
        assert not any("elaunch" in text.lower() for text in _error_rows(pane))
        assert app.pane is pane and pane.header.record.name == "Back again"

        # A second outage gets its own single row.
        client.fail_connects(OSError("refused"), OSError("refused"))
        client.latest.drop(1006)
        await _until(lambda: client.opened("ws-a") == 3 and not app.controller.reconnecting)
        await pilot.pause()
        assert _error_rows(pane) == [RECONNECTING_TEXT, RECONNECTING_TEXT]


async def test_background_controllers_reconnect_silently(fast_retries):
    client = _client(make_record("ws-a", started_at=_at(1)), make_record("ws-b", started_at=_at(2)))
    app = make_app(client, focus="ws-b")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-b")
        await app.switch_to("ws-a")
        await attached(app, pilot, "ws-a")
        background = app.controllers["ws-b"]
        client.fail_connects(OSError("refused"))
        client.stream_for("ws-b").drop(1006)
        await _until(lambda: client.opened("ws-b") == 2 and not background.reconnecting)
        assert background.locals == []

        # Shown during an outage, the pane gets the outage's one row.
        client.fail_connects(*(OSError("refused") for _ in range(1000)))
        client.stream_for("ws-b").drop(1006)
        await _until(lambda: background._outage)
        await app.switch_to("ws-b")
        pane = await attached(app, pilot, "ws-b")
        await pilot.pause()
        assert _error_rows(pane) == [RECONNECTING_TEXT]
        client._connect_errors.clear()
        await _until(lambda: client.opened("ws-b") == 3 and not background.reconnecting)
        await pilot.pause()
        assert _error_rows(pane) == [RECONNECTING_TEXT]


async def test_reconnect_stops_on_a_4004_close(fast_retries):
    client = _client(make_record("s1"))
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot)
        client.latest.drop(4004)
        await settle(pilot, lambda: _error_rows(pane) == [NOT_FOUND_TEXT])
        await asyncio.sleep(0.1)
        assert client.opened("s1") == 1 and not app.controller.reconnecting


async def test_reconnect_stops_once_the_record_ends_and_the_transcript_stays(fast_retries):
    client = _client(make_record("s1"), logs={"s1": [_user("u1", "hello")]})
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot)
        controller = app.controller
        client.fail_connects(*(OSError("refused") for _ in range(1000)))
        client.latest.drop(1006)
        await _until(lambda: controller._outage)
        client.publish(make_record("s1", status="exited"))
        await _until(lambda: not controller.reconnecting and not controller.live)
        attempts = len(client._connect_errors)
        await asyncio.sleep(0.1)
        assert len(client._connect_errors) == attempts  # no further connect attempts
        await pilot.pause()
        assert [row.key for row in pane.conversation.rows][0] == "msg-u1"
        assert pane.input.disabled and "ENDED" in pane.header.render().plain


async def test_reconnect_ends_when_the_session_ends_keeping_the_transcript():
    client = _client(make_record("s1"), logs={"s1": [_user("u1", "hello")]})
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot)
        controller, stream = app.controller, client.latest
        await apply_events(app, pilot, _user("u2", "still going", turn=2))
        controller.turn_active = True
        client.logs["s1"] += [_user("u2", "still going", turn=2), _assistant("a2", "last", 2)]
        client.publish(make_record("s1", status="stopped"))
        await _until(lambda: not controller.live and stream.disconnect.await_count == 1)
        await _until(lambda: ("s1", "u2") in client.requests)  # the tail, after the cursor
        await pilot.pause()
        keys = [row.key for row in pane.conversation.rows if row.key.startswith("msg-")]
        assert keys == ["msg-u1", "msg-u2", "msg-a2"]
        assert not controller.turn_active and pane.activity.activity is None
        assert _error_rows(pane) == []
        assert client.opened("s1") == 1


@pytest.mark.parametrize("known", [True, False], ids=["ended-record", "not-found"])
async def test_reconnect_fetches_an_on_screen_session_missing_from_a_snapshot(fast_retries, known):
    client = _client(make_record("ws-a", started_at=_at(1)), make_record("ws-b", started_at=_at(2)))
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        posted = _record_notices(app)
        pane = await attached(app, pilot, "ws-a")
        client.drop_feed()
        if known:
            client.records["ws-a"] = make_record("ws-a", started_at=_at(1), status="stopped")
        else:
            del client.records["ws-a"]
        await _until(lambda: client.fetched == ["ws-a"] and app.records["ws-a"].ended)
        await pilot.pause()
        assert app.records["ws-a"].status == ("stopped" if known else "exited")
        assert app.current == "ws-a" and app.pane is pane
        assert "ENDED" in pane.header.render().plain and pane.input.disabled
        assert "ws-a" in app.sidebar.sessions.rows  # still listed: it is on screen
        await _until(lambda: not app.controller.live)
        assert posted == []


async def test_reconnect_when_the_feed_is_silent_for_the_dead_feed_timeout(monkeypatch):
    from archie_cli.tui.client import HttpOrchestrator

    monkeypatch.setattr(tui_app, "FEED_RETRY_DELAYS", (0.01,))
    snapshot = msgspec.json.encode({"sessions": [make_record("s1")]}).decode()
    connects = 0
    stop = asyncio.Event()

    async def serve(reader, writer):
        # Sends the snapshot, then nothing at all: no events, no pings.
        nonlocal connects
        connects += 1
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
                b"connection: close\r\n\r\n" + f"event: snapshot\ndata: {snapshot}\n\n".encode()
            )
            await writer.drain()
            await stop.wait()
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = _client(make_record("s1"))
    client.feed = HttpOrchestrator(f"http://127.0.0.1:{port}", feed_read_timeout=0.2).feed
    app = make_app(client)
    try:
        async with app.run_test(size=SIZE) as pilot:
            await attached(app, pilot, "s1")
            await _until(lambda: connects >= 3)
            assert app.current == "s1" and len(app.query(SessionPane)) == 1
    finally:
        stop.set()
        server.close()
        await server.wait_closed()


async def test_http_session_reads_one_record(monkeypatch):
    import httpx
    from archie_cli.tui.client import HttpOrchestrator

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/sessions/ws-a":
            return httpx.Response(200, content=msgspec.json.encode(make_record("ws-a")))
        return httpx.Response(404, json={"error": "no"})

    _mock_httpx(monkeypatch, handler)
    client = HttpOrchestrator("http://orch:7600")
    assert (await client.session("ws-a")).session_id == "ws-a"
    with pytest.raises(OrchestratorError) as missing:
        await client.session("ws-x")
    assert missing.value.status_code == 404


# --- Dialogs, palette, model picker and filter (M7) --------------------------------------


def _screen_is(app, kind) -> bool:
    return isinstance(app.screen, kind)


async def _open_new_session_dialog(app, pilot):
    from archie_cli.tui.dialogs import NewSessionDialog

    await pilot.press("ctrl+n")
    await settle(pilot, lambda: _screen_is(app, NewSessionDialog))
    return app.screen


async def test_new_session_dialog_posts_the_workspace_and_default_backend_and_switches():
    from archie_shared.workspaces import Workspace

    client = _client(make_record("ws-a", workspace="beta"))
    client.workspace_list = [Workspace(name="beta"), Workspace(name="alpha")]
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-a")
        dialog = await _open_new_session_dialog(app, pilot)
        # The workspaces from /api/workspaces, the on-screen session's workspace and backend.
        assert [str(option[0]) for option in dialog.workspace._options] == ["alpha", "beta"]
        assert dialog.workspace.value == "beta"
        assert dialog.backend.value == "native"
        await pilot.press("enter")
        await settle(pilot, lambda: app.current == "beta-new01")
        assert client.created == [{"workspace": "beta", "backend": "native"}]
        pane = app.pane
        assert pane is not None and pane.record.session_id == "beta-new01"
        await settle(pilot, lambda: pane.loading_line.display)
        assert "Starting…" in str(pane.loading_line.render())
        assert client.opened("beta-new01") == 0

        client.publish(make_record("beta-new01", workspace="beta", status="idle"))
        await attached(app, pilot, "beta-new01")
        assert client.opened("beta-new01") == 1
        assert not pane.loading_line.display


async def test_new_session_dialog_kiro_choice_and_a_failed_start_shows_its_error():
    client = _client(make_record("ws-a"))
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-a")
        dialog = await _open_new_session_dialog(app, pilot)
        dialog.backend.value = "kiro"
        await pilot.press("ctrl+j")  # ctrl+enter in most terminals
        await settle(pilot, lambda: app.current == "ws-new01")
        assert client.created == [{"workspace": "ws", "backend": "kiro"}]

        client.publish(
            make_record("ws-new01", backend="kiro", status="failed", error="No image archie:kiro")
        )
        pane = app.pane
        await settle(pilot, lambda: pane.record.status == "failed")
        assert str(pane.loading_line.render()) == "Failed to start: No image archie:kiro"
        assert pane.input.disabled
        assert ("ws-new01", None) not in client.requests  # it never ran: nothing to load
        assert "ws-new01" in app.sidebar.sessions.rows  # ended, but on screen


async def test_new_session_dialog_defaults_to_the_on_screen_kiro_backend_and_cancels():
    from archie_cli.tui.dialogs import NewSessionDialog

    client = _client(make_record("ws-a", backend="kiro"))
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-a")
        dialog = await _open_new_session_dialog(app, pilot)
        assert dialog.backend.value == "kiro"
        await pilot.press("escape")
        await settle(pilot, lambda: not _screen_is(app, NewSessionDialog))
        assert client.created == [] and app.current == "ws-a"


async def test_new_session_dialog_without_a_session_defaults_to_archie_and_errors_notice():
    from archie_cli.tui.client import RequestRefused

    client = _client()
    client.workspace_list = []
    app = make_app(client, focus=None)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, lambda: app._snapshot_seen)
        posted = _record_notices(app)
        await pilot.press("ctrl+n")
        await settle(pilot, lambda: bool(posted))
        assert posted[-1][:3] == (
            "No workspaces",
            "Add a directory under the workspace root.",
            "warning",
        )

        from archie_shared.workspaces import Workspace

        client.workspace_list = [Workspace(name="alpha")]
        dialog = await _open_new_session_dialog(app, pilot)
        assert (dialog.workspace.value, dialog.backend.value) == ("alpha", "native")
        client.create_error = RequestRefused("No workspace named 'alpha'", 400)
        await pilot.press("enter")
        await settle(pilot, lambda: len(posted) == 2)
        assert posted[-1][:3] == ("Session not started", "No workspace named 'alpha'", "error")
        assert app.current is None

        client.workspaces_error = OrchestratorError("connection refused")
        await pilot.press("ctrl+n")
        await settle(pilot, lambda: len(posted) == 3)
        assert posted[-1][:3] == ("Could not list workspaces", "connection refused", "error")


async def _open_rename_dialog(app, pilot):
    from archie_cli.tui.dialogs import RenameDialog

    app.action_rename_session()
    await settle(pilot, lambda: _screen_is(app, RenameDialog))
    return app.screen


async def test_rename_dialog_patches_the_name_resets_it_and_rejects_invalid_names():
    from archie_cli.tui.dialogs import RenameDialog

    client = _client(make_record("s1", name="Fix the parser", name_source="llm"))
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "s1")
        dialog = await _open_rename_dialog(app, pilot)
        # Over the pane, which is dimmed meanwhile; prefilled with the model's title.
        assert dialog.over == app.query_one(tui_app.Main).region and dialog.over.x == 48
        assert app.query_one(tui_app.Main).has_class("-dimmed")
        assert dialog.input.value == "Fix the parser"

        for invalid, error in (
            ("x" * 81, "at most 80 characters"),
            ("tab\there", "control characters"),
        ):
            dialog.input.value = invalid
            await pilot.press("enter")
            await pilot.pause()
            assert dialog.error.display and error in str(dialog.error.render())
            assert _screen_is(app, RenameDialog)
        assert client.patches == []

        dialog.input.value = "  Parser fix  "
        await pilot.press("enter")
        await settle(pilot, lambda: bool(client.patches))
        assert client.patches == [("s1", {"name": "Parser fix"})]
        await settle(pilot, lambda: not app.query_one(tui_app.Main).has_class("-dimmed"))
        await settle(pilot, lambda: app.records["s1"].name == "Parser fix")

        dialog = await _open_rename_dialog(app, pilot)
        dialog.input.value = ""
        await pilot.press("enter")
        await settle(pilot, lambda: len(client.patches) == 2)
        assert client.patches[-1] == ("s1", {"name": None})
        await settle(pilot, lambda: app.records["s1"].name_source == "id")
        assert app.pane.header.record.name == "s1"


async def test_rename_dialog_starts_empty_for_an_id_name_and_shows_a_refusal_in_the_pane():
    from archie_cli.tui.client import RequestRefused

    client = _client(make_record("s1"))
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot, "s1")
        dialog = await _open_rename_dialog(app, pilot)
        assert dialog.input.value == "" and dialog.input.placeholder == "s1"
        client.patch = AsyncMock(side_effect=RequestRefused("Session store unavailable", 400))
        dialog.input.value = "New"
        await pilot.press("enter")
        await settle(pilot, lambda: bool(_error_rows(pane)))
        assert _error_rows(pane) == ["Rename failed: Session store unavailable"]


async def _open_stop_dialog(app, pilot):
    from archie_cli.tui.dialogs import ConfirmDialog

    app.action_stop_session()
    await settle(pilot, lambda: _screen_is(app, ConfirmDialog))
    return app.screen


async def test_stop_dialog_sends_delete_only_when_confirmed():
    from textual.widgets import Button

    client = _client(make_record("s1", name="Fix the parser"))
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot, "s1")
        dialog = await _open_stop_dialog(app, pilot)
        assert "Fix the parser" in str(dialog.message) and "log is kept" in str(dialog.message)
        assert dialog.over == app.query_one(tui_app.Main).region
        assert isinstance(dialog.focused, Button) and dialog.focused.id == "cancel"
        await pilot.press("enter")  # Cancel is focused: destructive dialogs default to safe
        await settle(pilot, lambda: app.screen is app.screen_stack[0])
        assert client.deleted == []

        dialog = await _open_stop_dialog(app, pilot)
        await pilot.press("right")
        assert dialog.focused.id == "confirm"
        await pilot.press("enter")
        await settle(pilot, lambda: app.records["s1"].status == "stopped")
        assert client.deleted == ["s1"]
        assert pane.input.disabled
        assert "s1" in app.sidebar.sessions.rows  # ended, still on screen


async def test_stop_dialog_shows_the_servers_409_message_in_the_pane():
    from archie_cli.tui.client import RequestRefused

    client = _client(make_record("s1"))
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot, "s1")
        client.delete_error = RequestRefused("Session 's1' is already stopping", 409)
        await _open_stop_dialog(app, pilot)
        await pilot.press("right", "enter")
        await settle(pilot, lambda: bool(_error_rows(pane)))
        assert _error_rows(pane) == ["Could not stop the session: Session 's1' is already stopping"]
        assert client.deleted == ["s1"]


def _command_names(app) -> list[str]:
    from archie_cli.tui.commands import session_commands

    return [command.name for command in session_commands(app)]


ALWAYS = ["Open web interface", "Keyboard shortcuts", "Quit"]


async def test_palette_lists_for_native_kiro_ended_and_no_session():
    client = _client(
        make_record("ws-a", started_at=_at(1)),
        make_record("ws-b", started_at=_at(2), backend="kiro"),
        make_record("ws-c", started_at=_at(3)),
    )
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-a")
        session = ["Rename session…", "Change model…"]
        assert _command_names(app) == [
            "New session…",
            *session,
            "Compact context",
            "Stop session…",
            *ALWAYS,
        ]
        await app.switch_to("ws-b")
        await attached(app, pilot, "ws-b")
        assert _command_names(app) == ["New session…", *session, "Stop session…", *ALWAYS]
        await app.switch_to("ws-c")
        await attached(app, pilot, "ws-c")
        client.publish(make_record("ws-c", started_at=_at(3), status="exited"))
        await settle(pilot, lambda: app.records["ws-c"].ended)
        assert _command_names(app) == ["New session…", *ALWAYS]

    empty = make_app(_client(), focus=None)
    async with empty.run_test(size=SIZE) as pilot:
        await settle(pilot, lambda: empty._snapshot_seen)
        assert _command_names(empty) == ["New session…", *ALWAYS]


def _palette_options(app) -> list:
    from textual.command import CommandList

    options = app.screen.query_one(CommandList)
    return [options.get_option_at_index(i) for i in range(options.option_count)]


async def test_palette_shows_only_our_commands_and_runs_them():
    from archie_cli.tui.dialogs import KeysDialog
    from textual.command import CommandPalette

    client = _client(make_record("s1", status="starting"))
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, lambda: app.pane is not None)
        await pilot.press("ctrl+p")
        await settle(pilot, lambda: _screen_is(app, CommandPalette) and _palette_options(app))
        # No Textual system commands (theme, screenshot, …); a starting session can be
        # renamed or stopped, not switched or compacted yet.
        assert [option.hit.text for option in _palette_options(app)] == [
            "New session…",
            "Rename session…",
            "Stop session…",
            *ALWAYS,
        ]
        await pilot.press(*"keyboard")
        await settle(pilot, lambda: len(_palette_options(app)) == 1)
        await pilot.press("enter")
        await settle(pilot, lambda: _screen_is(app, KeysDialog))


async def test_model_picker_palette_marks_the_current_model_and_switches(monkeypatch):
    from archie_cli.tui import commands
    from archie_shared.commands import SwitchModelCommand
    from archie_shared.models import BedrockProvider, CostConfig, ModelEntry, Rate
    from textual.command import CommandPalette

    def entry(name: str, rate: Rate) -> ModelEntry:
        return ModelEntry(
            name=name, context=200_000, provider=BedrockProvider(model_id="ep"),
            cost=CostConfig(rates=[rate]),
        )  # fmt: skip

    catalog = {
        "model-haiku": entry("Claude Haiku 4.5", Rate(input=1.0, output=5.0)),
        "model-sonnet": entry("Claude Sonnet 4.6", Rate(input=3.0, output=15.0)),
    }
    monkeypatch.setattr(commands, "_get_catalog", lambda: catalog)
    client = _client(make_record("s1"))
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "s1")
        app.controller.model_key = "model-sonnet"
        await pilot.press("alt+m")
        await settle(pilot, lambda: _screen_is(app, CommandPalette) and _palette_options(app))
        lines = [option.hit.prompt.plain for option in _palette_options(app)]
        assert lines == [
            "  Claude Haiku 4.5     $1.00 /  $5.00",
            "✓ Claude Sonnet 4.6    $3.00 / $15.00",
        ]
        await pilot.press(*"haiku")
        await settle(pilot, lambda: len(_palette_options(app)) == 1)
        await pilot.press("enter")
        await settle(pilot, lambda: bool(client.stream_for("s1").sent))
        (kind, command), *_ = client.stream_for("s1").sent
        assert kind == "command" and command == SwitchModelCommand(model_key="model-haiku")


async def test_compact_palette_action_sends_compact_for_native_sessions_only():
    client = _client(
        make_record("ws-a", started_at=_at(1)),
        make_record("ws-b", started_at=_at(2), backend="kiro"),
    )
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-a")
        app.action_compact()
        await settle(pilot, lambda: bool(client.stream_for("ws-a").sent))
        assert client.stream_for("ws-a").sent == [("message", "/compact")]
        await app.switch_to("ws-b")
        await attached(app, pilot, "ws-b")
        app.action_compact()
        await pilot.pause()
        assert client.stream_for("ws-b").sent == []


async def test_filter_matches_by_branch_workspace_name_and_id():
    client = _client(
        make_record(
            "alpha-01",
            started_at=_at(1),
            name="Fix the parser",
            location=SessionLocation(repo="app", branch="fix/lexer-tail"),
        ),
        make_record("alpha-02", started_at=_at(2), location=SessionLocation(repo="")),
        make_record("beta-01", started_at=_at(3), name="Tidy the README"),
    )
    app = make_app(client, focus="alpha-01")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "alpha-01")
        await pilot.press("ctrl+f")
        assert app.focused is app.sidebar.filter
        order = app.sidebar.sessions
        for query, expected in (
            ("LEXER", ["alpha-01"]),  # branch, any case
            ("beta", ["beta-01"]),  # workspace (and ID)
            ("readme", ["beta-01"]),  # name
            ("alpha-02", ["alpha-02"]),  # session ID
            ("app", ["alpha-01"]),  # repo
            ("nothing", []),
        ):
            app.sidebar.filter.value = query
            await settle(pilot, lambda e=expected: order.order == e)
        assert "No matching sessions" in str(order._empty.render())
        await pilot.press("escape")  # clears the filter; nothing is interrupted
        await settle(pilot, lambda: order.order == ["alpha-01", "alpha-02", "beta-01"])
        assert app.sidebar.filter.value == ""
        await pilot.press(*"beta", "enter")
        await settle(pilot, lambda: order.order == ["beta-01"])
        assert app.focused is order


async def test_ctrl_q_opens_the_quit_dialog_and_confirming_exits_without_delete():
    from archie_cli.tui.dialogs import QuitDialog

    client = _client(make_record("ws-a", status="working"), make_record("ws-b", started_at=_at(5)))
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "ws-a")
        await pilot.press("ctrl+q")
        await settle(pilot, lambda: _screen_is(app, QuitDialog))
        message = str(app.screen.message)
        assert "2 sessions keep running" in message and "1 of them mid-turn" in message
        await pilot.press("escape")
        await settle(pilot, lambda: not _screen_is(app, QuitDialog))
        assert app.return_code is None
        await pilot.press("ctrl+q")
        await settle(pilot, lambda: _screen_is(app, QuitDialog))
        await pilot.press("enter")
        await _until(lambda: app.return_code is not None)
    assert app.return_code == 0
    assert client.deleted == []


async def test_f1_shows_the_keys_dialog_and_esc_closes_it_first():
    from archie_cli.tui.dialogs import KEYS, KeysDialog

    client = _client(make_record("s1", status="working"))
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await attached(app, pilot, "s1")
        await pilot.press("f1")
        await settle(pilot, lambda: _screen_is(app, KeysDialog))
        text = str(app.screen.query_one(".dialog-body").render())
        for _section, rows in KEYS:
            for keys, what in rows:
                assert keys in text and what in text
        for key in ("ctrl+n", "alt+m", "ctrl+p", "ctrl+f", "f1", "ctrl+q", "ctrl+x"):
            assert key in text
        await pilot.press("escape")
        await settle(pilot, lambda: not _screen_is(app, KeysDialog))
        assert client.stream_for("s1").sent == []  # the dialog took Esc: no interrupt


async def test_status_bar_keeps_the_cost_visible_at_120_columns():
    client = _client(
        make_record(
            "s1",
            name="Fix the parser",
            location=SessionLocation(repo="app", branch="build-it/084-multi-session-tui"),
        )
    )
    app = make_app(client)
    async with app.run_test(size=(120, 40)) as pilot:
        pane = await attached(app, pilot, "s1")
        status = pane.status
        status.model_name = "Claude Sonnet 4.6"
        status.session_input = 123_456
        status.cache_read = 2_345_678
        status.cache_write = 34_567
        status.session_output = 45_678
        status.context_tokens = 156_000
        status.context_pct = 78.0
        status.pricing_label = "$12.3456 · 4.25 credits"
        await pilot.pause()
        line = status.render().plain
        assert status.size.width < 80
        assert line.rstrip().endswith("$12.3456 · 4.25 credits")
        assert len(line) <= status.size.width
        assert "s1" not in line and "2.3K" not in line  # session ID and cache dropped first
        assert "Claude Sonnet 4.6" in line
        # Wide enough, everything shows, the session ID on the right.
        wide = status.fit(200).plain
        assert "2345.7K" in wide and "build-it/084-multi-session-tui" in wide
        assert wide.rstrip().endswith("s1")
        # Very narrow: the model name shortens, the cost stays.
        narrow = status.fit(30).plain
        assert narrow.rstrip().endswith("$12.3456 · 4.25 credits")

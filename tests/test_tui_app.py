"""The multi-session TUI: sidebar from the feed, switching, controllers and the pane (084 M2).

Everything runs against ``FakeOrchestrator`` (``tests/tui_fakes.py``) with ``run_test``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import patch

import archie_cli.tui.app as tui_app
import msgspec
import pytest
from archie_cli.tui.app import EMPTY_TEXT, EmptyPane
from archie_cli.tui.conversation import ClientErrorMessage, ShellOutput, TurnStatus
from archie_cli.tui.input import MessageInput
from archie_cli.tui.pane import SessionHeader, SessionPane
from archie_cli.tui.session import ClientErrorEntry, ShellOutputEntry, TurnStatusEntry
from archie_cli.tui.sidebar import SessionRow, WorkspaceBand, age, state_badge, state_glyph
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


async def test_tab_moves_between_the_list_the_transcript_and_the_input():
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot)
        assert app.focused is pane.input
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

    client = _client(make_record("s1"))
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot)
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
        assert pane.status.git_branch == "main"
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
        assert pane.query_one(StatusBar).session_id == "agent-s1"


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
            await asyncio.sleep(tui_app.ANIMATION_INTERVAL)
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

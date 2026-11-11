"""Smoke tests for the ``archie tui`` prototype: discovery, replay and the mock flows."""

from datetime import timedelta
from pathlib import Path

import pytest
from archie_cli.tui import transcript_view
from archie_cli.tui.prototype import app as proto_app
from archie_cli.tui.prototype import data, rows
from archie_cli.tui.prototype.app import PrototypeApp
from archie_cli.tui.prototype.commands import model_price
from archie_cli.tui.prototype.dialogs import ConfirmDialog
from archie_cli.tui.prototype.panes import ToolDetailView
from archie_cli.tui.prototype.widgets import SessionList
from archie_cli.tui.question import QuestionPanel
from archie_cli.tui.transcript_view import ToolClicked
from archie_shared.events import (
    AssistantMessage,
    LLMRequest,
    SessionStarted,
    TurnComplete,
    UserMessage,
    encode_event,
)
from archie_shared.models import load_models
from archie_shared.transcript import ToolNode
from textual.widgets import ContentSwitcher


def _write_log(sessions_dir: Path, sid: str, prompt: str, cost: float) -> Path:
    events = [
        SessionStarted(
            id=f"{sid}-s", schema_version=1, sent_at="2026-11-01T10:00:00+00:00", model_key="m"
        ),
        UserMessage(id=f"{sid}-u", turn=1, scope=None, content=prompt),
        LLMRequest(
            id=f"{sid}-r",
            scope=None,
            turn=1,
            iteration=1,
            model_key="m",
            sent_at="2026-11-01T10:00:01+00:00",
            duration_ms=5,
            status="completed",
            input_tokens=100,
            output_tokens=10,
            cache_read_tokens=0,
            cache_write_tokens=0,
            context_tokens=110,
            cost_usd=cost,
        ),
        AssistantMessage(
            id=f"{sid}-a",
            turn=1,
            iteration=1,
            scope=None,
            request_id="r",
            content="done",
            interrupted=False,
        ),
        TurnComplete(id=f"{sid}-t", turn=1, scope=None, stop_reason="end_turn"),
    ]
    path = sessions_dir / f"{sid}.jsonl"
    path.write_text("\n".join(encode_event(e) for e in events) + "\n")
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    for n in range(3):
        _write_log(sessions, f"proj-0{n}", f"task number {n}\nmore detail", 0.5 + n)
    return tmp_path


def test_discover_scans_logs_without_an_index_and_scripts_states(home):
    sessions = data.discover(home, limit=10)

    assert {s.session_id for s in sessions} == {"proj-00", "proj-01", "proj-02"}
    assert [s.state for s in sessions] == ["working", "blocked", "idle"]
    assert all(s.title.startswith("task number") for s in sessions)
    assert not (home / "metrics.db").exists()  # discovery never creates the index


def test_discover_honours_session_prefixes_in_order(home):
    sessions = data.discover(home, limit=10, session_ids=("proj-02", "proj-00"))

    assert [s.session_id for s in sessions] == ["proj-02", "proj-00"]


def test_load_replays_the_log_into_transcript_and_totals(home):
    (session,) = data.discover(home, limit=10, session_ids=("proj-01",))
    data.load(session)

    assert session.loaded
    assert len(session.transcript.turns) == 1
    assert session.accounting.cost_usd == pytest.approx(1.5)
    assert session.accounting.context_tokens == 110


@pytest.mark.asyncio
async def test_switching_answering_and_stopping(home, monkeypatch):
    monkeypatch.setattr(proto_app, "BACKGROUND_FINISH_AFTER", 3600)
    sessions = data.discover(home, limit=10)
    app = PrototypeApp(sessions)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        assert app.current is sessions[0]

        blocked = next(s for s in sessions if s.state == "blocked")
        await app.switch_to(blocked.session_id)
        await pilot.pause(0.2)
        panel = app.query_one("#question", QuestionPanel)
        assert panel.display and panel.question is not None

        app.query_one("#input").focus()
        await pilot.press("1")
        await pilot.pause(0.1)
        assert blocked.state == "working"
        assert not panel.display

        app.action_stop_session()
        await pilot.pause(0.1)
        assert isinstance(app.screen, ConfirmDialog)
        app.screen.dismiss(True)
        await pilot.pause(0.1)
        assert blocked.state == "ended"
        assert app.query_one("#input").disabled

        order = app.query_one("#session-list", SessionList).order
        assert blocked.session_id in order  # still listed while it's on screen
        await app.switch_to(sessions[0].session_id)
        await pilot.pause(0.1)
        order = app.query_one("#session-list", SessionList).order
        assert blocked.session_id not in order  # then it leaves the sidebar


@pytest.mark.asyncio
async def test_sidebar_groups_by_workspace_oldest_first_live_only(tmp_path, monkeypatch):
    monkeypatch.setattr(proto_app, "BACKGROUND_FINISH_AFTER", 3600)
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    for sid in ("zeta-01", "alpha-02", "alpha-01", "old-01", "old-02"):
        _write_log(sessions_dir, sid, f"task {sid}", 0.1)
    sessions = data.discover(tmp_path, limit=20)
    by_id = {s.session_id: s for s in sessions}
    for s in sessions:
        s.state = "ended" if s.session_id.startswith("old") else "idle"
    by_id["alpha-01"].started_at = by_id["alpha-02"].started_at - timedelta(hours=1)

    app = PrototypeApp(sessions)
    async with app.run_test(size=(140, 60)) as pilot:
        await pilot.pause(0.3)
        sidebar = app.query_one("#session-list", SessionList)
        order = sidebar.order
        row = sidebar.get_option("alpha-01").prompt.plain

    # Workspaces by name, oldest first inside each; ended sessions aren't listed.
    assert order == ["alpha-01", "alpha-02", "zeta-01"]
    # One line: glyph, the id without the workspace prefix (never the first prompt), age.
    assert "\n" not in row and "01" in row and "alpha" not in row and "task" not in row


def test_compact_rows_put_shell_and_exec_on_one_line():
    command = "cd /repo\ngit status\ngit log -3"
    plain = ToolNode(id="a", tool_use_id="a", name="shell", input={"command": command})
    described = ToolNode(
        id="b",
        tool_use_id="b",
        name="exec",
        input={"source": "x = 1\ny = 2", "description": "Count the open plans"},
    )

    assert "\n" not in rows.render_compact_row(plain).plain
    assert rows.render_compact_row(described).plain.endswith("Exec Count the open plans")


@pytest.mark.asyncio
async def test_compact_rows_are_restored_when_the_app_exits(home, monkeypatch):
    monkeypatch.setattr(proto_app, "BACKGROUND_FINISH_AFTER", 3600)
    original = transcript_view.render_tool_row
    app = PrototypeApp(data.discover(home, limit=10))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.2)
        assert transcript_view.render_tool_row is rows.render_compact_row
    assert transcript_view.render_tool_row is original


@pytest.mark.asyncio
async def test_tool_view_opens_in_the_pane_and_esc_returns(home, monkeypatch):
    monkeypatch.setattr(proto_app, "BACKGROUND_FINISH_AFTER", 3600)
    sessions = data.discover(home, limit=10)
    sessions[0].state = "idle"
    app = PrototypeApp(sessions)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        tool = ToolNode(id="t1", tool_use_id="t1", name="shell", input={"command": "ls"})
        await app.on_tool_clicked(ToolClicked(tool))
        await pilot.pause(0.1)
        switcher = app.query_one("#views", ContentSwitcher)
        assert isinstance(switcher.visible_content, ToolDetailView)
        assert app.screen is app.screen_stack[0]  # no modal: sidebar and header stay
        # One level: the tool's name and call id, not the session name again.
        assert [c.plain for c in switcher.visible_content.crumbs] == ["shell  t1"]

        await pilot.press("escape")
        await pilot.pause(0.1)
        assert switcher.current == f"session-{sessions[0].session_id}"


@pytest.mark.asyncio
async def test_rename_shows_the_name_in_the_sidebar(home, monkeypatch):
    monkeypatch.setattr(proto_app, "BACKGROUND_FINISH_AFTER", 3600)
    sessions = data.discover(home, limit=10)
    app = PrototypeApp(sessions)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        app.action_rename_session()
        await pilot.pause(0.1)
        await pilot.press(*"WAF review", "enter")
        await pilot.pause(0.1)
        sidebar = app.query_one("#session-list", SessionList)
        prompt = sidebar.get_option(app.current.session_id).prompt

    assert app.current.name == "WAF review"
    assert "WAF review" in prompt.plain


def test_model_prices_align():
    prices = [model_price(key) for key in load_models()]
    priced = [p for p in prices if p.startswith(("$", " "))]
    assert priced and len({len(p) for p in priced}) == 1
    assert all("per M" not in p and "tiered" not in p for p in prices)

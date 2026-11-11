"""Smoke tests for the ``archie tui`` prototype: discovery, replay and the mock flows."""

from datetime import timedelta
from pathlib import Path

import pytest
from archie_cli.tui import theme, transcript_view
from archie_cli.tui.conversation import UserMessage as UserMessageRow
from archie_cli.tui.prototype import app as proto_app
from archie_cli.tui.prototype import data, rows
from archie_cli.tui.prototype.app import PrototypeApp
from archie_cli.tui.prototype.commands import SessionCommands, model_price
from archie_cli.tui.prototype.dialogs import ConfirmDialog
from archie_cli.tui.prototype.panes import ToolDetailView
from archie_cli.tui.prototype.widgets import ActivityLine, Notice, NoticeStack, SessionList
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


@pytest.mark.asyncio
async def test_activity_line_follows_the_turn_until_it_ends(home, monkeypatch):
    monkeypatch.setattr(proto_app, "BACKGROUND_FINISH_AFTER", 3600)
    sessions = data.discover(home, limit=10)
    sessions[0].state = "idle"
    app = PrototypeApp(sessions)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        line = app.query_one("#activity", ActivityLine)
        views = app.query_one("#views")
        height = views.region.height
        assert str(line.render()).strip() == ""  # nothing running: blank, not hidden

        app.query_one("#input").focus()
        await pilot.press(*"hi", "enter")
        await pilot.pause(0.3)
        assert "Thinking… (esc to cancel)" in str(line.render())
        assert views.region.height == height  # the transcript didn't shift

        await pilot.pause(1.2)  # the mock turn's tool call
        assert "Working…" in str(line.render())

        await pilot.pause(2.6)  # thinking again (until 3.8s in), then the reply streams
        assert "Responding…" in str(line.render())

        await pilot.press("escape")
        await pilot.pause(0.1)
        assert str(line.render()).strip() == ""
        assert views.region.height == height


@pytest.mark.asyncio
async def test_notifications_stack_in_the_sidebar_above_the_key_hints(home, monkeypatch):
    monkeypatch.setattr(proto_app, "BACKGROUND_FINISH_AFTER", 3600)
    app = PrototypeApp(data.discover(home, limit=10))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        for n in range(5):
            app.notify(f"note [{n}]", title="Heads up", timeout=0.6)
        await pilot.pause(0.1)
        notices = list(app.query(Notice))
        stack = app.query_one("#notices", NoticeStack)
        keys = app.query_one("#sidebar-keys")

        # Newest LIMIT kept, in order; plain text (brackets aren't markup); no toasts.
        assert [str(n.render()).splitlines()[-1] for n in notices] == [
            f"note [{n}]" for n in range(5 - NoticeStack.LIMIT, 5)
        ]
        assert not app.query("Toast")
        assert stack.region.bottom <= keys.region.y
        assert stack.region.right <= app.query_one("#sidebar").region.right

        await pilot.pause(0.8)  # past the timeout
        assert not app.query(Notice)


def _tool(name, tool_input, result=None, *, status="ok", ms=0):
    tool = ToolNode(id="t", tool_use_id="t", name=name, input=tool_input)
    if result is not None:
        tool.result, tool.status, tool.duration_ms = result, status, ms
    return tool


def _lines(tool, width=100):
    return rows.render_row(tool, width).plain.split("\n")


def test_every_row_is_two_lines_with_a_connector():
    for tool in (
        _tool("read", {"path": "/workspace/a.py"}),
        _tool("read", {"path": "/workspace/a.py"}, "File: a.py (80 lines)\n", ms=6),
        _tool("grep", {"pattern": "x"}, "PathValidationError: no", status="error"),
        _tool("mystery", {"q": "roadmap"}, "ok"),
    ):
        first, second = _lines(tool)
        assert second.startswith("╰ ")


def test_read_metrics_and_range():
    tool = _tool(
        "read",
        {"path": "/workspace/repo/t.py", "offset": 120, "limit": 141},
        "File: repo/t.py (880 lines)\nShowing lines 120-260 of 880\n",
        ms=4,
    )
    assert _lines(tool) == ["● Read · 141 of 880 lines · 4ms", "╰ repo/t.py:120-260"]


def test_errors_go_on_line_one_without_the_class_or_the_repeated_target():
    tool = _tool(
        "read",
        {"path": "/tmp/x.png"},
        "FileNotFoundError: File not found: /tmp/x.png",
        status="error",
        ms=6,
    )
    assert _lines(tool) == ["✕ Read · 6ms · File not found", "╰ /tmp/x.png"]

    failed = _tool(
        "exec", {"source": "x"}, "error: KeyError: 'team'\n\ntraceback:\n…", status="error"
    )
    assert _lines(failed)[0] == "✕ Exec · KeyError: 'team'"  # exec keeps the class


def test_shell_rows_strip_workspace_and_keep_the_working_directory():
    tool = _tool(
        "shell",
        {"command": "cd /workspace/repo && pytest -q " + "x " * 60, "cwd": "/workspace/repo"},
        "[exit: 1]\n...\n1 failed, 10 passed in 8.91s\n",
        status="error",
        ms=9200,
    )
    first, second = _lines(tool, 80)
    assert first == "✕ Shell · exit 1 · 2 lines · 9.2s · 1 failed, 10 passed in 8.91s"
    assert second.startswith("╰ $ cd repo && pytest") and "/workspace" not in second
    assert second.endswith("… · in repo") and len(second) <= 80  # cut, directory kept

    at_root = _tool("shell", {"command": "ls", "cwd": "/workspace"}, "[exit: 0]\na\n")
    assert _lines(at_root)[1] == "╰ $ ls"
    described = _tool("shell", {"command": "ls", "description": "List the files"})
    assert _lines(described) == ["○ Shell · running", "╰ List the files"]


def test_long_paths_lose_their_middle():
    path = "/workspace/archie-nexus/cli/src/archie_cli/tui/prototype/data.py"
    second = _lines(_tool("edit", {"path": path}, "+a\n-b\n"), 40)[1]
    assert len(second) == 40 and "…" in second and second.endswith("prototype/data.py")


def test_tool_names_are_blue():
    line = rows.render_row(_tool("read", {"path": "a.py"}), 80)
    name = line.plain.index("Read")
    assert any(
        span.start <= name < span.end and "#5fafff" in str(span.style) for span in line.spans
    )


@pytest.mark.asyncio
async def test_user_messages_have_no_header(home, monkeypatch):
    monkeypatch.setattr(proto_app, "BACKGROUND_FINISH_AFTER", 3600)
    sessions = data.discover(home, limit=10)
    app = PrototypeApp(sessions)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.5)
        messages = list(app.query(rows.PlainUserMessage))
        texts = [str(m.query_one("Static").render()) for m in messages]

    assert messages and all("You" not in text for text in texts)
    assert any(text.startswith("task number") for text in texts)


def test_edit_counts_are_green_and_red():
    line = rows.render_row(_tool("edit", {"path": "a.py"}, "+++ b\n+x\n+y\n-z\n"), 80)
    assert line.plain.split("\n")[0] == "● Edit · +2 −1"

    def colour_at(text):
        index = line.plain.index(text)
        return [str(s.style) for s in line.spans if s.start <= index < s.end]

    assert theme.SUCCESS in colour_at("+2")
    assert theme.ERROR in colour_at("−1")


@pytest.mark.asyncio
async def test_profile_link_and_palette_entry_open_the_web_interface(home, monkeypatch):
    monkeypatch.setattr(proto_app, "BACKGROUND_FINISH_AFTER", 3600)
    app = PrototypeApp(data.discover(home, limit=10))
    opened: list[str] = []
    monkeypatch.setattr(app, "open_url", lambda url, **_: opened.append(url))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        info = app.query_one("#brand-info")
        plain = str(info.render())
        assert plain.endswith("running · local ↗") and "prototype" not in plain

        await pilot.click("#brand-info", offset=(2 + plain.index("local") + 1, 1))
        await pilot.pause(0.1)
        assert opened == ["http://127.0.0.1:7600/ui/sessions"]

        names = [name for name, _help, _cb in SessionCommands(app.screen)._commands()]
        assert "Open web interface" in names


def test_question_and_task_rows():
    question = _tool("question", {"question": "Which?"}, "User selected option 1: A coding choice")
    assert _lines(question) == ["● Question · → A coding choice", "╰ Which?"]
    waiting = _tool("question", {"question": "Which?"})
    assert _lines(waiting)[0] == "? Question · waiting for you"

    tasks = {"tasks": [{"agent": "archie-worker", "prompt": "a"}, {"agent": "archie-worker"}]}
    assert _lines(_tool("task", tasks, "done", ms=155_000)) == [
        "● Task · 2 agents · 2m35s",
        "╰ 2 × archie-worker",
    ]


@pytest.mark.asyncio
async def test_prototype_rows_are_restored_when_the_app_exits(home, monkeypatch):
    monkeypatch.setattr(proto_app, "BACKGROUND_FINISH_AFTER", 3600)
    original = transcript_view.ToolRow
    app = PrototypeApp(data.discover(home, limit=10))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.2)
        assert transcript_view.ToolRow is rows.TwoLineToolRow
        assert transcript_view.UserMessage is rows.PlainUserMessage
    assert transcript_view.ToolRow is original
    assert transcript_view.UserMessage is UserMessageRow


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
        status_right = str(app.query_one("#status-right").render())

    assert app.current.name == "WAF review"
    assert "WAF review" in prompt.plain
    # Named: the status bar shows the id instead (and never the version).
    assert app.current.session_id in status_right and "v0." not in status_right


def test_model_prices_align():
    prices = [model_price(key) for key in load_models()]
    priced = [p for p in prices if p.startswith(("$", " "))]
    assert priced and len({len(p) for p in priced}) == 1
    assert all("per M" not in p and "tiered" not in p for p in prices)

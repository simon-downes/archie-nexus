"""TUI popups: tool detail, subagent transcript, breadcrumbs and Escape."""

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import archie_cli.tui.app as tui_app
import pytest
from archie_cli.tui import theme
from archie_cli.tui.app import ArchieApp
from archie_cli.tui.detail import SubagentScreen, ToolDetailScreen, render_crumbs
from archie_cli.tui.transcript_view import (
    Conversation,
    IterationBlock,
    SubagentRow,
    ToolClicked,
    ToolRow,
)
from archie_shared.events import (
    AssistantMessage,
    ToolCall,
    ToolResult,
    TurnComplete,
    UserMessage,
)
from rich.syntax import Syntax
from textual.widgets import Static


def _call(tool_use_id, name, tool_input, *, scope=None, index=None, iteration=1):
    return ToolCall(
        id=f"c-{tool_use_id}",
        turn=1,
        iteration=iteration,
        scope=scope,
        subagent_index=index,
        request_id="r1",
        tool_use_id=tool_use_id,
        name=name,
        input=tool_input,
    )


def _result(tool_use_id, content, *, scope=None, index=None, is_error=False):
    return ToolResult(
        id=f"r-{tool_use_id}",
        turn=1,
        iteration=1,
        scope=scope,
        subagent_index=index,
        request_id="r1",
        tool_use_id=tool_use_id,
        content=content,
        is_error=is_error,
        duration_ms=7,
    )


USER = UserMessage(id="u1", turn=1, scope=None, content="go")
TASK = _call("tk", "task", {"tasks": [{"agent": "general", "prompt": "look"}]})
CHILD_TEXT = AssistantMessage(
    id="ca",
    turn=1,
    iteration=1,
    scope="tk",
    subagent_index=0,
    request_id="c1",
    content="child says hello",
    interrupted=False,
)
CHILD_SHELL = _call("cs", "shell", {"command": "ls -la"}, scope="tk", index=0, iteration=2)


class _App(ArchieApp):
    """ArchieApp with the network stubbed out."""

    CSS_PATH = Path(tui_app.__file__).parent / "archie.tcss"  # relative paths resolve here

    def __init__(self):
        super().__init__(ws_url="ws://x", api_url="http://x", container_name="x")
        self._ws.connect = AsyncMock(side_effect=OSError("offline"))
        self._ws.send_command = AsyncMock()
        self._ws.send_interrupt = AsyncMock()


async def _feed(app, pilot, *events):
    with (
        patch.object(app, "_update_accounting_status"),
        patch.object(app, "_estimate_context_pct", return_value=1.0),
    ):
        for event in events:
            app._apply_event(event)
    await pilot.pause()


def _text(widget) -> str:
    return str(widget.render())


def _static_text(screen, selector) -> str:
    """Plain text of a Static's content, whatever renderable it holds."""
    from rich.console import Console

    console = Console(width=200, record=True, file=open(os.devnull, "w"))  # noqa: SIM115
    console.print(screen.query_one(selector, Static).content)
    return console.export_text()


def _section_renderables(screen):
    return list(screen.query_one("#tool-sections", Static).content.renderables)


@pytest.mark.asyncio
async def test_clicking_a_tool_row_opens_its_detail_popup():
    app = _App()
    async with app.run_test(size=(120, 40)) as pilot:
        await _feed(app, pilot, USER, _call("t1", "exec", {"source": "x = 1\nprint(x)"}))
        await pilot.click(ToolRow)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ToolDetailScreen)
        assert screen.tool.tool_use_id == "t1"
        assert "Running" in _static_text(screen, "#tool-sections")
        assert screen.crumbs == ["Session", "Exec (2 lines)"]

        # The open popup updates when the result arrives.
        await _feed(app, pilot, _result("t1", '{"answer": 42}'))
        rendered = _section_renderables(screen)
        syntaxes = [r for group in rendered for r in getattr(group, "renderables", [])]
        lexers = [s.lexer.name for s in syntaxes if isinstance(s, Syntax)]
        assert lexers == ["Python", "JSON"]
        assert "running" not in _static_text(screen, ".detail-status")


@pytest.mark.asyncio
async def test_escape_closes_only_the_popup_and_does_not_interrupt():
    app = _App()
    async with app.run_test(size=(120, 40)) as pilot:
        await _feed(app, pilot, USER, _call("t1", "read", {"path": "a.py"}))
        app._turn_active = True
        await pilot.click(ToolRow)
        await pilot.pause()
        assert isinstance(app.screen, ToolDetailScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ToolDetailScreen)
        app._ws.send_interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_subagent_row_opens_live_transcript_with_clickable_tools():
    app = _App()
    async with app.run_test(size=(120, 40)) as pilot:
        await _feed(app, pilot, USER, TASK, CHILD_TEXT)
        rows = app.query(SubagentRow)
        assert len(rows) == 1 and "general #0" in _text(rows.first())
        await pilot.click(SubagentRow)
        await pilot.pause()
        sub_screen = app.screen
        assert isinstance(sub_screen, SubagentScreen)
        assert sub_screen.crumbs == ["Session", "general #0"]
        rows = [type(w).__name__ for w in sub_screen.view.children]
        assert rows == ["AssistantMessage"]

        # A live child tool call appears in the open transcript.
        await _feed(app, pilot, CHILD_SHELL)
        blocks = sub_screen.view.query(IterationBlock)
        assert len(blocks) == 1
        assert "ls -la" in _text(blocks.first().query_one(ToolRow))
        # The main view shows the child only as its row under the task tool.
        assert len(app.query_one(Conversation).query(IterationBlock)) == 1

        # Its tools open the same popup, one level deeper.
        row = sub_screen.view.query_one(ToolRow)
        row.post_message(ToolClicked(row.tool))
        await pilot.pause()
        tool_screen = app.screen
        assert isinstance(tool_screen, ToolDetailScreen)
        assert tool_screen.crumbs == ["Session", "general #0", "Shell ls -la"]
        assert render_crumbs(tool_screen.crumbs).plain == "Session › general #0"

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is sub_screen
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, (SubagentScreen, ToolDetailScreen))


@pytest.mark.asyncio
async def test_clicking_a_tool_inside_the_subagent_screen():
    app = _App()
    async with app.run_test(size=(120, 40)) as pilot:
        await _feed(app, pilot, USER, TASK, CHILD_SHELL)
        app.open_child_detail(("tk", 0))
        await pilot.pause()
        sub_screen = app.screen
        assert isinstance(sub_screen, SubagentScreen)
        row = sub_screen.view.query_one(ToolRow)
        await pilot.click(row)
        await pilot.pause()
        assert isinstance(app.screen, ToolDetailScreen)
        assert app.screen.tool.tool_use_id == "cs"


@pytest.mark.asyncio
async def test_stop_child_targets_the_open_subagent_screen():
    app = _App()
    async with app.run_test(size=(120, 40)) as pilot:
        await _feed(app, pilot, USER, TASK, CHILD_TEXT)
        assert app._active_child_key is None
        await pilot.click(SubagentRow)
        await pilot.pause()
        assert app._active_child_key == ("tk", 0)
        app.action_stop_child()
        await pilot.pause()
        command = app._ws.send_command.await_args.args[0]
        assert (command.scope, command.subagent_index) == ("tk", 0)
        await pilot.press("escape")
        await pilot.pause()
        assert app._active_child_key is None


@pytest.mark.asyncio
async def test_subagent_status_updates_in_row_and_popup():
    app = _App()
    async with app.run_test(size=(120, 40)) as pilot:
        await _feed(app, pilot, USER, TASK, CHILD_TEXT)
        await pilot.click(SubagentRow)
        await pilot.pause()
        screen = app.screen
        await _feed(
            app,
            pilot,
            TurnComplete(id="tc", turn=1, scope="tk", subagent_index=0, stop_reason="end_turn"),
        )
        assert "complete" in _static_text(screen, ".detail-status")
        row = app.screen_stack[0].query_one(SubagentRow)
        assert "Completed" in _text(row)


@pytest.mark.asyncio
async def test_full_reset_closes_popups():
    app = _App()
    async with app.run_test(size=(120, 40)) as pilot:
        await _feed(app, pilot, USER, _call("t1", "read", {"path": "a"}))
        await pilot.click(ToolRow)
        await pilot.pause()
        assert isinstance(app.screen, ToolDetailScreen)
        with patch.object(app, "_update_accounting_status"):
            await app._reset_replay_state()
        await pilot.pause()
        assert not isinstance(app.screen, ToolDetailScreen)


@pytest.mark.asyncio
async def test_long_results_are_capped_in_the_popup():
    app = _App()
    async with app.run_test(size=(120, 40)) as pilot:
        await _feed(
            app, pilot, USER, _call("t1", "web_fetch", {"url": "u"}), _result("t1", "y" * 9000)
        )
        await pilot.click(ToolRow)
        await pilot.pause()
        assert "1,000 more characters" in _static_text(app.screen, "#tool-sections")


def test_breadcrumbs_name_the_levels_above_the_popup():
    text = render_crumbs(["sess", "general #0", "Read a.py"])
    # The popup's own level is on the status line, so the crumbs stop at its parent.
    assert text.plain == "sess › general #0"
    assert text.spans[0].style == f"bold {theme.ERROR}"
    assert render_crumbs(["sess", "Read a.py"]).plain == "sess"


@pytest.mark.asyncio
async def test_clicking_the_backdrop_closes_only_the_top_popup():
    app = _App()
    async with app.run_test(size=(120, 40)) as pilot:
        await _feed(app, pilot, USER, TASK, CHILD_SHELL)
        app.open_child_detail(("tk", 0))
        await pilot.pause()
        sub_screen = app.screen
        await pilot.click(sub_screen.view.query_one(ToolRow))
        await pilot.pause()
        assert isinstance(app.screen, ToolDetailScreen)
        # Inside the frame: stays open.
        await pilot.click("#tool-sections")
        await pilot.pause()
        assert isinstance(app.screen, ToolDetailScreen)
        # The top-left corner is outside the 92% x 88% frame.
        await pilot.click(offset=(0, 0))
        await pilot.pause()
        assert app.screen is sub_screen
        await pilot.click(offset=(0, 0))
        await pilot.pause()
        assert not isinstance(app.screen, (SubagentScreen, ToolDetailScreen))


def test_popup_escape_binding_is_screen_local():
    bindings = {b.key: b.action for b in ToolDetailScreen.BINDINGS}
    assert bindings["escape"] == "dismiss"


@pytest.mark.asyncio
async def test_child_event_before_the_subagent_screen_mounts():
    """A live event between push and mount must not raise or duplicate rows."""
    app = _App()
    async with app.run_test(size=(120, 40)) as pilot:
        await _feed(app, pilot, USER, TASK, CHILD_TEXT)
        app.open_child_detail(("tk", 0))
        with (
            patch.object(app, "_update_accounting_status"),
            patch.object(app, "_estimate_context_pct", return_value=1.0),
        ):
            app._apply_event(CHILD_SHELL)  # no pause: the screen is not mounted yet
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SubagentScreen)
        assert [row.key for row in screen.view.rows] == [item.id for item in screen.sub.items]
        assert len(screen.view.query(IterationBlock)) == 1
        assert "ls -la" in _text(screen.view.query_one(ToolRow))


@pytest.mark.asyncio
async def test_event_before_the_tool_popup_mounts_still_draws_it():
    app = _App()
    async with app.run_test(size=(120, 40)) as pilot:
        await _feed(app, pilot, USER, _call("t1", "read", {"path": "a.py"}), _result("t1", "ok"))
        row = app.query_one(ToolRow)
        app.on_tool_clicked(ToolClicked(row.tool))
        await _feed(app, pilot, _call("t2", "read", {"path": "b.py"}))
        screen = app.screen
        assert isinstance(screen, ToolDetailScreen)
        assert "a.py" in _static_text(screen, ".detail-status")
        assert _section_renderables(screen)


@pytest.mark.asyncio
async def test_child_error_text_is_not_parsed_as_markup():
    from archie_shared.events import TurnError

    app = _App()
    async with app.run_test(size=(120, 40)) as pilot:
        error = TurnError(
            id="te", turn=1, scope="tk", subagent_index=0, message="bad [/] list[str]"
        )
        await _feed(app, pilot, USER, TASK, CHILD_TEXT, error)
        app.open_child_detail(("tk", 0))
        await pilot.pause()
        await pilot.pause()
        from archie_cli.tui.conversation import ErrorMessage

        assert "bad [/] list[str]" in _text(app.screen.view.query_one(ErrorMessage))

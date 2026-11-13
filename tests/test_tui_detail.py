"""In-pane detail views: tool calls, subagent transcripts, crumbs, Esc and ctrl+x (084 D13).

The views open on the on-screen session pane's stack, in the transcript's place, in the
multi-session app over the fake orchestrator client.
"""

from archie_cli.tui import theme
from archie_cli.tui.app import ArchieApp
from archie_cli.tui.detail import (
    Crumb,
    DetailView,
    SubagentView,
    ToolDetailView,
    crumb_line,
    is_error_section,
    section_facts,
)
from archie_cli.tui.pane import SessionPane
from archie_cli.tui.transcript_view import (
    Conversation,
    IterationBlock,
    SubagentRow,
    ToolClicked,
    ToolRow,
)
from archie_shared.commands import InterruptCommand
from archie_shared.events import (
    AssistantMessage,
    ToolCall,
    ToolResult,
    TurnComplete,
    UserMessage,
)
from archie_shared.transcript import DetailSection
from rich.text import Text
from textual.widgets import Static

from tests.tui_fakes import (
    FakeOrchestrator,
    apply_events,
    attached,
    make_app,
    make_record,
    settle,
)

SIZE = (170, 40)


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


def _app():
    """The app showing one idle session over the fake client."""
    return make_app()


async def _feed(app, pilot, *events):
    """Apply events to the on-screen session once its pane has loaded."""
    await attached(app, pilot)
    await apply_events(app, pilot, *events)


def _open_child(app, key):
    app.pane.open_subagent(app.controller.builder.subagents[key])


def _text(widget) -> str:
    return str(widget.render())


def _plain(static: Static) -> str:
    content = static.content
    return content.plain if isinstance(content, Text) else str(content)


def _titles(view: ToolDetailView) -> list[str]:
    return [_plain(panel.title) for panel in view.panels]


def _top(app) -> DetailView | None:
    return app.pane.top_view


def _views(app) -> list[DetailView]:
    """Every detail view mounted anywhere in the app."""
    return list(app.query(DetailView))


async def test_clicking_a_tool_row_opens_its_view_in_the_pane():
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _feed(app, pilot, USER, _call("t1", "exec", {"source": "x = 1\nprint(x)"}))
        pane = app.pane
        await pilot.click(ToolRow)
        await pilot.pause()
        view = _top(app)
        assert isinstance(view, ToolDetailView)
        assert view.tool.tool_use_id == "t1"
        assert pane.views == (view,)
        assert view.crumbs == [Crumb("Exec (2 lines)", "t1")]
        # It takes the transcript's place; the rest of the pane stays.
        assert not pane.conversation.display and pane.conversation.is_mounted
        for widget in (pane.header, pane.activity, pane.input, pane.status):
            assert widget.display
        assert view.parent is pane
        assert view.running.display
        assert _titles(view) == ["SOURCE"]
        assert _plain(view.panels[0].facts) == "python · 2 lines"
        assert view.card in app.animation.widgets

        # The open view updates when the result arrives.
        await _feed(app, pilot, _result("t1", '{"answer": 42}'))
        assert not view.running.display
        assert _titles(view) == ["SOURCE", "RESULT"]
        assert "running" not in _text(view.card).lower()
        assert view.card not in app.animation.widgets
        # The Python input and the JSON result are both syntax-highlighted.
        for panel, snippet in zip(view.panels, ("print", "answer"), strict=True):
            body = panel.body.content
            assert isinstance(body, Text)
            start = body.plain.index(snippet)
            assert any(
                span.start <= start < span.end and getattr(span.style, "color", None)
                for span in body.spans
            ), snippet


async def test_a_running_tool_view_follows_its_result_from_the_stream():
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot)
        await pilot.press(*"go", "enter")
        stream = app.controller.ws
        await settle(pilot, lambda: stream.sent)
        stream.push(USER, _call("t1", "shell", {"command": "make test"}))
        await settle(pilot, lambda: pane.conversation.query(ToolRow))
        await pilot.click(ToolRow)
        await pilot.pause()
        view = _top(app)
        assert isinstance(view, ToolDetailView) and view.running.display
        assert _titles(view) == ["COMMAND"]

        stream.push(_result("t1", "[exit: 1]\nboom", is_error=True))
        await settle(pilot, lambda: not view.running.display)
        assert _titles(view) == ["COMMAND", "ERROR"]
        assert view.panels[1].has_class("-error")
        assert not view.panels[0].has_class("-error")
        assert "boom" in _plain(view.panels[1].body)


async def test_tool_view_text_can_be_selected_and_copied():
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _feed(
            app,
            pilot,
            USER,
            _call("t1", "exec", {"source": "x = 1\nprint(x)"}),
            _result("t1", "hello world"),
        )
        await pilot.click(ToolRow)
        await pilot.pause()
        view = _top(app)
        source = view.panels[0].body
        # Drag over both code lines (the body has a padding of 1 row and 2 columns).
        await pilot.mouse_down(source, offset=(2, 1))
        await pilot.hover(source, offset=(10, 2))
        await pilot.mouse_up(source, offset=(10, 2))
        assert app.screen.get_selected_text() == "x = 1\nprint(x)"

        await pilot.press("ctrl+c")
        assert app.clipboard == "x = 1\nprint(x)"

        # A plain (unhighlighted) result uses the default colour.
        content = view.panels[1].body.content
        assert not content.style and not content.spans
        assert content.plain == "hello world"
        # Copying the focused view takes every section.
        assert view.panels[0].get_copy_text() == "x = 1\nprint(x)"
        assert view.get_copy_text() == "SOURCE\nx = 1\nprint(x)\n\nRESULT\nhello world"


async def test_escape_pops_the_view_without_interrupting_then_interrupts():
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot)
        await pilot.press(*"go", "enter")
        stream = app.controller.ws
        await settle(pilot, lambda: stream.sent)
        stream.push(USER, _call("t1", "read", {"path": "a.py"}))
        await settle(pilot, lambda: pane.conversation.query(ToolRow))
        assert app.controller.turn_active
        await pilot.click(ToolRow)
        await pilot.pause()
        assert isinstance(_top(app), ToolDetailView)

        await pilot.press("escape")
        await pilot.pause()
        assert pane.views == () and not _views(app)
        assert pane.conversation.display
        assert ("interrupt", None) not in stream.sent

        await pilot.press("escape")
        await settle(pilot, lambda: ("interrupt", None) in stream.sent)


async def test_escape_kills_a_running_shell_before_popping_a_view():
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _feed(app, pilot, USER, _call("t1", "read", {"path": "a.py"}))
        killed = []

        class Proc:
            def kill(self):
                killed.append(True)

        controller = app.controller
        controller.shell_active, controller._shell_proc = True, Proc()
        await pilot.click(ToolRow)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert killed == [True]
        assert isinstance(_top(app), ToolDetailView)
        controller.shell_active, controller._shell_proc = False, None
        await pilot.press("escape")
        await pilot.pause()
        assert _top(app) is None


async def test_popping_the_last_view_shows_the_transcript_where_it_was():
    events = [USER]
    for n in range(30):
        events.append(_call(f"t{n}", "read", {"path": f"f{n}.py"}, iteration=n + 1))
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _feed(app, pilot, *events)
        conversation = app.pane.conversation
        await settle(pilot, lambda: conversation.max_scroll_y > 10)
        await settle(pilot)  # the jump to the latest rows lands
        conversation.scroll_to(y=5, animate=False, immediate=True)
        await settle(pilot)
        assert conversation.scroll_y == 5
        row = conversation.query(ToolRow)[3]
        row.post_message(ToolClicked(row.tool))
        await pilot.pause()
        assert not conversation.display
        # A row added while the view is open doesn't move the reader.
        await apply_events(app, pilot, _call("late", "read", {"path": "z.py"}, iteration=99))
        await pilot.press("escape")
        await pilot.pause()
        await pilot.pause()
        assert conversation.display
        assert conversation.scroll_y == 5


async def test_a_transcript_following_the_end_shows_rows_added_behind_a_view():
    events = [USER]
    for n in range(30):
        events.append(_call(f"t{n}", "read", {"path": f"f{n}.py"}, iteration=n + 1))
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _feed(app, pilot, *events)
        conversation = app.pane.conversation
        await settle(pilot, lambda: conversation.max_scroll_y > 10)
        await settle(pilot)
        assert conversation.following and conversation.scroll_y == conversation.max_scroll_y
        end = conversation.max_scroll_y
        await pilot.click(conversation.query(ToolRow).last())
        await pilot.pause()
        later = [
            _call(f"late{n}", "read", {"path": f"z{n}.py"}, iteration=40 + n) for n in range(5)
        ]
        await apply_events(app, pilot, *later)
        await pilot.press("escape")
        await settle(
            pilot, lambda: conversation.scroll_y == conversation.max_scroll_y > end, tries=20
        )
        assert conversation.display


async def test_subagent_row_opens_a_live_transcript_with_clickable_tools():
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _feed(app, pilot, USER, TASK, CHILD_TEXT)
        pane = app.pane
        rows = app.query(SubagentRow)
        assert len(rows) == 1 and "general #0" in _text(rows.first())
        await pilot.click(SubagentRow)
        await pilot.pause()
        sub_view = _top(app)
        assert isinstance(sub_view, SubagentView)
        assert sub_view.crumbs == [Crumb("general #0")]
        rows = [type(w).__name__ for w in sub_view.view.children]
        assert rows == ["PromptMessage", "AssistantMessage"]

        # A live child tool call appears in the open transcript.
        await _feed(app, pilot, CHILD_SHELL)
        blocks = sub_view.view.query(IterationBlock)
        assert len(blocks) == 1
        assert "ls -la" in _text(blocks.first().query_one(ToolRow))
        # The main transcript shows the child only as its row under the task tool.
        assert len(app.query_one(Conversation).query(IterationBlock)) == 1

        # Its tools open a view one level deeper; the subagent view stays, hidden.
        row = sub_view.view.query_one(ToolRow)
        row.post_message(ToolClicked(row.tool))
        await pilot.pause()
        tool_view = _top(app)
        assert isinstance(tool_view, ToolDetailView)
        assert pane.views == (sub_view, tool_view)
        assert not sub_view.display and sub_view.is_mounted
        assert tool_view.crumbs == [Crumb("general #0"), Crumb("Shell ls -la", "cs")]
        line = tool_view.query_one(".detail-crumbs", Static).content
        assert line.plain == "← esc  general #0 › Shell ls -la  cs"

        await pilot.press("escape")
        await pilot.pause()
        assert _top(app) is sub_view and sub_view.display
        await pilot.press("escape")
        await pilot.pause()
        assert _top(app) is None and not _views(app)


async def test_clicking_a_tool_inside_the_subagent_view():
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _feed(app, pilot, USER, TASK, CHILD_SHELL)
        _open_child(app, ("tk", 0))
        await pilot.pause()
        sub_view = _top(app)
        assert isinstance(sub_view, SubagentView)
        await pilot.click(sub_view.view.query_one(ToolRow))
        await pilot.pause()
        tool_view = _top(app)
        assert isinstance(tool_view, ToolDetailView)
        assert tool_view.tool.tool_use_id == "cs"

        # The deeper view follows the child's result too.
        await _feed(app, pilot, _result("cs", "total 0", scope="tk", index=0))
        assert _titles(tool_view) == ["COMMAND", "RESULT"]


async def test_ctrl_x_stops_the_child_of_the_open_subagent_view():
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _feed(app, pilot, USER, TASK, CHILD_SHELL)
        pane = app.pane
        stream = app.controller.ws
        assert app.active_child_key is None
        # Without a subagent view, ctrl+x is the input's (cut the line).
        pane.input.focus()
        await pilot.press(*"abc", "ctrl+x")
        await pilot.pause()
        assert pane.input.text == "" and not stream.sent

        await pilot.click(SubagentRow)
        await pilot.pause()
        assert app.active_child_key == ("tk", 0)
        # A tool view over it: ctrl+x still stops the subagent under it.
        _top(app).view.query_one(ToolRow).post_message(
            ToolClicked(_top(app).view.query_one(ToolRow).tool)
        )
        await pilot.pause()
        assert isinstance(_top(app), ToolDetailView)
        pane.input.focus()
        await pilot.press("ctrl+x")
        await settle(pilot, lambda: stream.sent)
        assert stream.sent == [("command", InterruptCommand(scope="tk", subagent_index=0))]

        await pilot.press("escape", "escape")
        await pilot.pause()
        assert app.active_child_key is None


async def test_subagent_status_updates_in_row_and_view():
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _feed(app, pilot, USER, TASK, CHILD_TEXT)
        await pilot.click(SubagentRow)
        await pilot.pause()
        view = _top(app)
        assert view.card in app.animation.widgets
        await _feed(
            app,
            pilot,
            TurnComplete(id="tc", turn=1, scope="tk", subagent_index=0, stop_reason="end_turn"),
        )
        assert "complete" in _text(view.card)
        assert view.card not in app.animation.widgets
        row = app.pane.conversation.query_one(SubagentRow)
        assert "Completed" in _text(row)


async def test_full_reset_closes_the_views():
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _feed(app, pilot, USER, _call("t1", "read", {"path": "a"}))
        await pilot.click(ToolRow)
        await pilot.pause()
        assert isinstance(_top(app), ToolDetailView)
        await app.controller.reset()
        await pilot.pause()
        assert _top(app) is None and not _views(app)
        assert app.pane.conversation.display


async def test_switching_sessions_with_a_view_open_leaves_one_pane_and_no_view():
    client = FakeOrchestrator(
        sessions=[
            make_record("ws-a", started_at="2026-11-12T10:01:00+00:00"),
            make_record("ws-b", started_at="2026-11-12T10:02:00+00:00"),
        ],
        logs={"ws-a": [USER, TASK, CHILD_SHELL]},
    )
    app = make_app(client, focus="ws-a")
    async with app.run_test(size=SIZE) as pilot:
        pane = await attached(app, pilot, "ws-a")
        await settle(pilot, lambda: pane.conversation.query(SubagentRow))
        await pilot.click(SubagentRow)
        await pilot.pause()
        assert isinstance(_top(app), SubagentView)
        await app.switch_to("ws-b")
        await attached(app, pilot, "ws-b")
        assert len(app.query(SessionPane)) == 1 and not _views(app)
        await app.switch_to("ws-a")
        pane = await attached(app, pilot, "ws-a")
        await pilot.pause()
        assert len(app.query(SessionPane)) == 1 and not _views(app)
        assert pane.views == () and pane.conversation.display
        # Later changes go to the new pane only, without errors.
        await apply_events(app, pilot, _result("cs", "ok", scope="tk", index=0))
        assert not _views(app)


async def test_long_results_are_capped_in_the_view():
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _feed(
            app, pilot, USER, _call("t1", "web_fetch", {"url": "u"}), _result("t1", "y" * 9000)
        )
        await pilot.click(ToolRow)
        await pilot.pause()
        result = _top(app).panels[-1]
        assert _plain(result.facts).endswith("1 line · 1,000 more chars")
        assert _plain(result.body).endswith("… 1,000 more characters")


def test_crumb_line_names_every_level_and_marks_the_current_one():
    text = crumb_line([Crumb("general #0"), Crumb("Read a.py", "tu_1")])
    assert text.plain == "← esc  general #0 › Read a.py  tu_1"
    styles = {text.plain[span.start : span.end]: str(span.style) for span in text.spans}
    assert styles["general #0"] == theme.MUTED
    assert styles["Read a.py"] == f"bold {theme.PRIMARY}"
    assert styles["  tu_1"] == theme.MUTED
    assert crumb_line([Crumb("Read a.py")]).plain == "← esc  Read a.py"


def test_section_facts_name_the_language_lines_and_cut():
    assert section_facts(DetailSection("Source", "a\nb\n", "python")) == "python · 2 lines"
    assert section_facts(DetailSection("Result", "one")) == "1 line"
    guessed = DetailSection("Content", "x = 1", filename="a.py", omitted=1200)
    assert section_facts(guessed) == "python · 1 line · 1,200 more chars"
    assert is_error_section(DetailSection("Error", "bad"))
    assert not is_error_section(DetailSection("Result", "fine"))


async def test_child_event_before_the_subagent_view_mounts():
    """A live event between push and mount must not raise or duplicate rows."""
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _feed(app, pilot, USER, TASK, CHILD_TEXT)
        _open_child(app, ("tk", 0))
        app.controller.apply(CHILD_SHELL)  # no pause: the view is not mounted yet
        await pilot.pause()
        view = _top(app)
        assert isinstance(view, SubagentView)
        assert [row.key for row in view.view.rows] == [
            "local-1",  # the task prompt
            *(item.id for item in view.sub.items),
        ]
        assert len(view.view.query(IterationBlock)) == 1
        assert "ls -la" in _text(view.view.query_one(ToolRow))


async def test_event_before_the_tool_view_mounts_still_draws_it():
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _feed(app, pilot, USER, _call("t1", "read", {"path": "a.py"}), _result("t1", "ok"))
        row = app.query_one(ToolRow)
        app.pane.open_tool(row.tool)
        await _feed(app, pilot, _call("t2", "read", {"path": "b.py"}))
        view = _top(app)
        assert isinstance(view, ToolDetailView)
        assert "a.py" in _text(view.card)
        assert _titles(view) == ["INPUT", "RESULT"]


async def test_child_error_text_is_not_parsed_as_markup():
    from archie_cli.tui.conversation import ErrorMessage
    from archie_shared.events import TurnError

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        error = TurnError(
            id="te", turn=1, scope="tk", subagent_index=0, message="bad [/] list[str]"
        )
        await _feed(app, pilot, USER, TASK, CHILD_TEXT, error)
        _open_child(app, ("tk", 0))
        await pilot.pause()
        await pilot.pause()
        view = _top(app)
        assert "bad [/] list[str]" in _text(view.view.query_one(ErrorMessage))
        assert "bad [/] list[str]" in _text(view.card)


def test_prompt_preview_caps_lines_and_characters():
    from archie_cli.tui.conversation import PROMPT_PREVIEW_CHARS, prompt_preview

    assert prompt_preview("short\nprompt\n") == ("short\nprompt", 0)
    lines = "\n".join(f"line {n}" for n in range(13))
    text, hidden = prompt_preview(lines)
    assert text.split("\n") == [f"line {n}" for n in range(10)] and hidden == 3
    # One huge line is cut at the character cap and counted as hidden.
    text, hidden = prompt_preview("x" * (PROMPT_PREVIEW_CHARS + 50) + "\nnext")
    assert text == "x" * PROMPT_PREVIEW_CHARS + "…" and hidden == 2


async def test_subagent_view_shows_its_prompt_collapsed_and_expandable():
    from archie_cli.tui.conversation import PromptMessage, PromptToggle

    prompt = "\n".join(f"step {n}" for n in range(14))
    task = _call("tk", "task", {"tasks": [{"agent": "general", "prompt": prompt}]})
    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _feed(app, pilot, USER, task, CHILD_TEXT)
        assert app.controller.builder.subagents[("tk", 0)].prompt == prompt
        await pilot.click(SubagentRow)
        await pilot.pause()
        message = _top(app).view.query_one(PromptMessage)
        body = message.query_one(".prompt-body", Static)
        assert "step 9" in body.content.plain and "step 10" not in body.content.plain
        toggle = message.query_one(PromptToggle)
        assert "+4 lines" in str(toggle.render())

        await pilot.click(toggle)
        await pilot.pause()
        assert "step 13" in body.content.plain
        assert "collapse" in str(toggle.render())
        assert message.get_copy_text() == prompt

        await pilot.click(toggle)
        await pilot.pause()
        assert "step 10" not in body.content.plain


async def test_short_prompt_has_no_toggle():
    from archie_cli.tui.conversation import PromptMessage, PromptToggle

    app = _app()
    async with app.run_test(size=SIZE) as pilot:
        await _feed(app, pilot, USER, TASK, CHILD_TEXT)
        await pilot.click(SubagentRow)
        await pilot.pause()
        message = _top(app).view.query_one(PromptMessage)
        body = message.query_one(".prompt-body", Static).content
        assert "look" in body.plain
        assert not message.query(PromptToggle)
        # Only the header is coloured; the prompt text uses the default colour.
        assert not body.style
        header_end = body.plain.index("\n") + 1
        assert all(span.end <= header_end for span in body.spans)


def test_detail_views_are_widgets_not_screens():
    import archie_cli.tui.detail as detail
    from textual.screen import Screen

    screens = [
        name
        for name, value in vars(detail).items()
        if isinstance(value, type) and issubclass(value, Screen)
    ]
    assert screens == []
    # Row clicks reach the pane, which owns the stack; the app has no popup handlers.
    assert not hasattr(ArchieApp, "on_tool_clicked")
    assert not hasattr(ArchieApp, "on_subagent_clicked")

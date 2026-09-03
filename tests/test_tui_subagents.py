from unittest.mock import MagicMock, patch

from archie_cli.tui.app import ArchieApp
from archie_cli.tui.subagents import ChildActivityState, SubagentActivity, SubagentScreen
from archie_shared.events import LLMRequest, TextDelta, ToolCall, ToolResult, TurnComplete
from archie_shared.tool_summaries import format_tool_complete, format_tool_pending
from rich.text import Text


def _app() -> ArchieApp:
    return ArchieApp(
        ws_url="ws://localhost/stream",
        api_url="http://localhost",
        container_name="container",
    )


def test_live_scoped_llm_cost_is_included_in_status_accounting():
    app = _app()
    app._update_accounting_status = MagicMock()
    event = LLMRequest(
        id="request-1",
        scope="task-1",
        subagent_index=0,
        turn=1,
        iteration=0,
        model_key="model",
        sent_at="2025-01-01T00:00:00Z",
        duration_ms=10,
        status="completed",
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=0,
        cache_write_tokens=0,
        context_tokens=100,
        cost_usd=0.0085,
    )
    with patch.object(app, "query_one", return_value=MagicMock()):
        app._apply_event(event)

    assert app._cumulative_cost == 0.0085
    app._update_accounting_status.assert_called_once()


def test_child_activity_keeps_three_lines_and_renders_agent():
    state = ChildActivityState(scope="task-1", index=1, agent="reviewer")
    for line in ("one", "two", "three", "four"):
        state.add_line(line)

    widget = SubagentActivity(state)

    assert state.lines == ["two", "three", "four"]
    rendered = widget.render_text()
    assert isinstance(rendered, Text)
    assert "reviewer #1" in rendered.plain
    assert "one" not in rendered.plain


def test_child_activity_status_icons_and_colours():
    state = ChildActivityState(scope="task-1", index=0, agent="worker")
    widget = SubagentActivity(state)
    rendered = widget.render_text()
    assert "○" in rendered.plain
    assert rendered.spans[0].style == "bold #6871ff"

    state.status = "complete"
    rendered = widget.render_text()
    assert "●" in rendered.plain
    assert rendered.spans[0].style == "bold #67c26d"

    state.status = "error"
    state.error = "Error: failed"
    rendered = widget.render_text()
    assert "●" in rendered.plain
    assert rendered.spans[0].style == "bold #ff6d67"


def test_child_activity_shows_current_tool():
    state = ChildActivityState(scope="task-1", index=0, agent="worker")
    state.set_activity("Read foo.py")
    assert "Read foo.py" in SubagentActivity(state).render_text().plain


def test_tool_error_does_not_make_child_terminal():
    app = _app()
    with patch.object(app, "_render_child"):
        app._handle_scoped_event(
            ToolCall(
                id="c1",
                turn=1,
                iteration=1,
                scope="task-1",
                subagent_index=0,
                request_id="r1",
                tool_use_id="child-tool",
                name="read",
                input={"path": "missing.py"},
            )
        )
        app._handle_scoped_event(
            ToolResult(
                id="r1e",
                turn=1,
                iteration=1,
                scope="task-1",
                subagent_index=0,
                request_id="r1",
                tool_use_id="child-tool",
                content="not found",
                is_error=True,
                duration_ms=5,
                result_bytes=9,
            )
        )

    child = app._child_activity[("task-1", 0)]
    assert child.status == "running"
    assert "Read" in child.activity


def test_child_exec_activity_has_explicit_prefix():
    app = _app()
    with patch.object(app, "_render_child"):
        app._handle_scoped_event(
            ToolCall(
                id="c1",
                turn=1,
                iteration=1,
                scope="task-1",
                subagent_index=0,
                request_id="r1",
                tool_use_id="child-tool",
                name="exec",
                input={"source": "print('hello')"},
            )
        )

    assert app._child_activity[("task-1", 0)].activity == "Exec (1 lines)"


def test_child_activity_uses_shared_tool_summaries():
    pending = format_tool_pending("read", {"path": "README.md"})
    complete = format_tool_complete(
        "read", {"path": "README.md"}, "File: README.md (2 lines)", False
    )
    assert "Read" in pending
    assert "README.md" in complete


def test_child_reducer_uses_shared_summaries_and_status():
    app = _app()
    with patch.object(app, "_render_child"):
        app._handle_scoped_event(
            ToolCall(
                id="c1",
                turn=1,
                iteration=1,
                scope="task-1",
                subagent_index=0,
                request_id="r1",
                tool_use_id="child-tool",
                name="read",
                input={"path": "README.md"},
            )
        )
        app._handle_scoped_event(
            ToolResult(
                id="r1e",
                turn=1,
                iteration=1,
                scope="task-1",
                subagent_index=0,
                request_id="r1",
                tool_use_id="child-tool",
                content="File: README.md (2 lines)",
                is_error=False,
                duration_ms=5,
                result_bytes=10,
            )
        )
        app._handle_scoped_event(
            TurnComplete(
                id="done",
                turn=1,
                scope="task-1",
                subagent_index=0,
                stop_reason="end_turn",
            )
        )

    child = app._child_activity[("task-1", 0)]
    assert child.agent == "child"
    assert "Read" in child.lines[0]
    assert "README.md" in child.lines[1]
    assert child.status == "complete"


def test_modal_uses_mutable_state():
    state = ChildActivityState(scope="task-1", index=0, agent="researcher")
    modal = SubagentScreen(state)
    state.add_line("new live output")
    state.status = "complete"

    assert modal.state is state
    assert modal.state.lines == ["new live output"]
    assert modal.state.status == "complete"


def test_replay_scoped_event_routes_to_child_state():
    app = _app()
    event = TextDelta(
        id="delta",
        turn=1,
        iteration=1,
        scope="task-1",
        subagent_index=1,
        request_id="r1",
        text="replayed output",
    )
    with patch.object(app, "_render_child"):
        app._apply_event(event)

    assert app._child_activity[("task-1", 1)].activity == "Responding..."
    assert app._transient_assistant_text

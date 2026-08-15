from unittest.mock import patch

from archie_cli.tui.app import ArchieApp
from archie_cli.tui.subagents import ChildActivityState, SubagentActivity, SubagentScreen
from archie_shared.canonical_events import TextDelta, ToolCall, ToolResult, TurnComplete
from archie_shared.tool_summaries import format_tool_complete, format_tool_pending


def _app() -> ArchieApp:
    return ArchieApp(
        ws_url="ws://localhost/stream",
        api_url="http://localhost",
        container_name="container",
    )


def test_child_activity_keeps_three_lines_and_renders_agent():
    state = ChildActivityState(scope="task-1", index=1, agent="reviewer")
    for line in ("one", "two", "three", "four"):
        state.add_line(line)

    widget = SubagentActivity(state)

    assert state.lines == ["two", "three", "four"]
    assert "reviewer #1" in widget.render_text()
    assert "one" not in widget.render_text()


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
                turn_iteration="1.1",
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
                turn_iteration="1.1",
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
        turn_iteration="1.1",
        scope="task-1",
        subagent_index=1,
        request_id="r1",
        text="replayed output",
    )
    with patch.object(app, "_render_child"):
        app._render_canonical(event)

    assert app._child_activity[("task-1", 1)].lines == ["replayed output"]

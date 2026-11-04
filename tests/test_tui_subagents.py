"""TUI subagent display: shared model state, rows and the status screen."""

from unittest.mock import MagicMock, patch

from archie_cli.tui.app import ArchieApp
from archie_cli.tui.subagents import SubagentScreen
from archie_cli.tui.transcript_view import render_subagent_row
from archie_shared.events import (
    AssistantMessage,
    LLMRequest,
    TextDelta,
    ToolCall,
    ToolResult,
    TurnComplete,
)
from archie_shared.transcript import CHILD_CONTENT_FILTERED_MESSAGE, SubagentNode
from rich.text import Text


def _app() -> ArchieApp:
    return ArchieApp(
        ws_url="ws://localhost/stream",
        api_url="http://localhost",
        container_name="container",
    )


def _child_tool_call(name="read", tool_input=None, tool_use_id="child-tool"):
    return ToolCall(
        id=f"c-{tool_use_id}",
        turn=1,
        iteration=1,
        scope="task-1",
        subagent_index=0,
        request_id="r1",
        tool_use_id=tool_use_id,
        name=name,
        input=tool_input if tool_input is not None else {"path": "README.md"},
    )


def _child_tool_result(content, *, is_error=False, tool_use_id="child-tool"):
    return ToolResult(
        id=f"r-{tool_use_id}",
        turn=1,
        iteration=1,
        scope="task-1",
        subagent_index=0,
        request_id="r1",
        tool_use_id=tool_use_id,
        content=content,
        is_error=is_error,
        duration_ms=5,
        result_bytes=len(content),
    )


def _apply(app, *events):
    with patch.object(app, "query_one", return_value=MagicMock()):
        for event in events:
            app._apply_event(event)
    return app.transcript


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
    assert app.transcript.subagents[("task-1", 0)].cost_usd == 0.0085


def test_subagent_row_renders_agent_and_status_glyphs():
    sub = SubagentNode("sub-x", "task-1", 1, agent="reviewer", activity="Read foo.py")
    rendered = render_subagent_row(sub)
    assert isinstance(rendered, Text)
    assert "reviewer #1" in rendered.plain and "Read foo.py" in rendered.plain
    assert "○" in rendered.plain
    assert any(span.style == "bold #6871ff" for span in rendered.spans)

    sub.status = "complete"
    rendered = render_subagent_row(sub)
    assert "●" in rendered.plain and "Completed" in rendered.plain
    assert any(span.style == "bold #67c26d" for span in rendered.spans)

    sub.status, sub.error = "error", "Error: failed"
    rendered = render_subagent_row(sub)
    assert "Error: failed" in rendered.plain
    assert any(span.style == "bold #ff6d67" for span in rendered.spans)


def test_tool_error_does_not_make_child_terminal():
    app = _app()
    model = _apply(
        app,
        _child_tool_call(tool_input={"path": "missing.py"}),
        _child_tool_result("not found", is_error=True),
    )
    child = model.subagents[("task-1", 0)]
    assert child.status == "running"
    assert "Read" in child.activity


def test_child_exec_activity_has_explicit_prefix():
    app = _app()
    model = _apply(app, _child_tool_call("exec", {"source": "print('hello')"}))
    assert model.subagents[("task-1", 0)].activity == "Exec (1 lines)"


def test_child_tools_are_kept_for_the_transcript():
    app = _app()
    model = _apply(
        app,
        _child_tool_call(),
        _child_tool_result("File: README.md (2 lines)"),
        TurnComplete(id="done", turn=1, scope="task-1", subagent_index=0, stop_reason="end_turn"),
    )
    child = model.subagents[("task-1", 0)]
    assert child.agent == "child"
    (group,) = child.items
    assert group.tools[0].name == "read" and group.tools[0].status == "ok"
    assert child.status == "complete"


def test_content_filtered_child_is_shown_as_error():
    app = _app()
    model = _apply(
        app,
        TurnComplete(
            id="done", turn=1, scope="task-1", subagent_index=0, stop_reason="content_filtered"
        ),
    )
    child = model.subagents[("task-1", 0)]
    assert child.status == "error"
    assert child.error == CHILD_CONTENT_FILTERED_MESSAGE


def test_status_screen_reads_the_live_node():
    sub = SubagentNode("sub-x", "task-1", 0, agent="researcher")
    modal = SubagentScreen(sub)
    sub.status = "complete"
    assert modal.sub is sub and modal.sub.status == "complete"


def test_replay_scoped_delta_streams_into_child_transcript():
    app = _app()
    model = _apply(
        app,
        TextDelta(
            id="delta",
            turn=1,
            iteration=1,
            scope="task-1",
            subagent_index=1,
            request_id="r1",
            text="replayed output",
        ),
    )
    child = model.subagents[("task-1", 1)]
    assert child.activity == "Responding..."
    assert child.items[0].text == "replayed output"


def test_child_assistant_replaces_its_stream():
    app = _app()
    model = _apply(
        app,
        TextDelta(
            id="d",
            turn=1,
            iteration=1,
            scope="task-1",
            subagent_index=0,
            request_id="r1",
            text="partial ",
        ),
        AssistantMessage(
            id="a",
            turn=1,
            iteration=1,
            scope="task-1",
            subagent_index=0,
            request_id="r1",
            content="partial response",
            interrupted=False,
        ),
    )
    (item,) = model.subagents[("task-1", 0)].items
    assert item.kind == "text" and item.content == "partial response"

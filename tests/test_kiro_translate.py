"""TurnTranslator on frames recorded from real Kiro ACP V3 runs (077 spike)."""

from __future__ import annotations

import json
from pathlib import Path

from archie_agent.kiro.translate import NO_COMPLETION_ERROR, TurnTranslator
from archie_shared.events import (
    AssistantMessage,
    IterationStart,
    LLMRequest,
    TextDelta,
    ToolCall,
    ToolResult,
    TurnComplete,
    TurnError,
    TurnInterrupted,
)

FIXTURES = Path(__file__).parent / "fixtures" / "kiro"
MODEL_KEY = "kiro/gpt-5.6-luna"


def _run(name: str, *, price=lambda c: c * 0.02, error: str | None = None):
    frames = [json.loads(line) for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines()]
    tr = TurnTranslator(Path("/tmp/x.jsonl"), MODEL_KEY, 3)
    events = []
    stop = None
    sent = frames[0]["t"] - 0.5
    last = sent
    for frame in frames:
        last = frame["t"]
        if "update" in frame:
            events.extend(tr.feed(frame["update"], frame["t"]))
        else:
            stop = frame["response"]["stopReason"]
    events.extend(
        tr.finish(
            stop_reason=None if error else stop, error=error, sent_at=sent, at=last, price=price
        )
    )
    return events


def _kinds(events, scope=None):
    return [type(e).__name__ for e in events if getattr(e, "scope", None) == scope]


def test_text_tool_load_text_shell_opens_iterations_with_distinct_request_ids():
    events = _run("run1_parallel_shell")
    root = [e for e in events if e.scope is None]
    starts = [e for e in root if isinstance(e, IterationStart)]
    # Kiro reports A's result before B's call, so per D12 B opens its own iteration.
    assert [s.iteration for s in starts] == [0, 1, 2, 3]
    calls = [e for e in root if isinstance(e, ToolCall)]
    assert [c.iteration for c in calls] == [1, 2]
    # tool_load is dropped: only the two Archie shell calls are logged.
    assert [c.name for c in calls] == ["shell", "shell"]
    assert calls[0].input == {"command": "sleep 5; echo A"}
    messages = [e for e in root if isinstance(e, AssistantMessage)]
    assert [m.iteration for m in messages] == [0, 1, 3]
    assert messages[0].content.startswith("I need to load the shell tool")
    # Each iteration's assistant_message precedes its tool calls.
    assert root.index(messages[1]) < root.index(calls[0])
    request_ids = {m.request_id for m in messages}
    assert len(request_ids) == len(messages) == 3
    # Text deltas carry the request key of their own segment.
    deltas = [e for e in root if isinstance(e, TextDelta)]
    assert {d.request_id for d in deltas} == request_ids


def test_parallel_shell_results_and_accounting():
    events = _run("run1_parallel_shell")
    results = [e for e in events if isinstance(e, ToolResult)]
    assert [r.content for r in results] == ["[exit: 0]\nA\n", "[exit: 0]\nB\n"]
    assert not any(r.is_error for r in results)
    assert all(r.duration_ms > 1000 for r in results)
    request = next(e for e in events if isinstance(e, LLMRequest))
    assert request.credits == round(0.1349044320066335, 6)
    assert request.cost_usd == round(0.1349044320066335 * 0.02, 6)
    assert request.status == "completed"
    assert request.model_key == MODEL_KEY
    assert (request.input_tokens, request.output_tokens, request.iteration) == (0, 0, 0)
    assert isinstance(events[-1], TurnComplete)
    assert events[-1].stop_reason == "end_turn"
    assert events[-2] is request


def test_skill_load_maps_to_skill_tool():
    events = _run("run2_skill")
    calls = [e for e in events if isinstance(e, ToolCall)]
    assert [(c.name, c.input) for c in calls] == [("skill", {"name": "canary-skill"})]
    result = next(e for e in events if isinstance(e, ToolResult))
    assert result.tool_use_id == calls[0].tool_use_id
    assert "Skill directory" in result.content


def test_image_metadata_from_data_url():
    events = _run("run2_image")
    result = next(e for e in events if isinstance(e, ToolResult))
    assert len(result.images) == 1
    image = result.images[0]
    assert (image.format, image.width, image.height) == ("png", 64, 64)
    assert image.bytes > 0


def test_subagent_dispatch_maps_to_scoped_task():
    events = _run("run2_subagent")
    dispatch = next(e for e in events if isinstance(e, ToolCall) and e.scope is None)
    assert dispatch.name == "task"
    assert dispatch.input["tasks"][0]["agent"] == "archie-worker"
    assert "SUBAGENT-OK-5512" in dispatch.input["tasks"][0]["prompt"]
    scope = dispatch.tool_use_id
    child = [e for e in events if e.scope == scope]
    child_calls = [e for e in child if isinstance(e, ToolCall)]
    assert [c.name for c in child_calls] == ["shell"]
    assert all(e.subagent_index == 0 for e in child)
    # Subagent Response and tool_load are dropped; the child ends with its terminal.
    assert isinstance(child[-1], TurnComplete)
    child_results = [e for e in child if isinstance(e, ToolResult)]
    assert "SUBAGENT-OK-5512" in child_results[0].content
    parent_result = next(e for e in events if isinstance(e, ToolResult) and e.tool_use_id == scope)
    assert "SUBAGENT-OK-5512" in parent_result.content
    # The child terminal precedes the parent's result for the dispatch.
    assert events.index(child[-1]) < events.index(parent_result)
    child_text = [e for e in child if isinstance(e, AssistantMessage)]
    assert [m.iteration for m in child_text] == [0, 2]
    assert child_calls[0].iteration == 1


def test_cancelled_turn_is_interrupted_and_charged():
    events = _run("run2_cancel")
    result = next(e for e in events if isinstance(e, ToolResult))
    assert result.is_error
    request = next(e for e in events if isinstance(e, LLMRequest))
    assert request.status == "interrupted"
    assert request.cost_usd > 0
    assert isinstance(events[-1], TurnInterrupted)


def test_error_without_completion_is_no_usage_turn_error():
    tr = TurnTranslator(Path("/tmp/x.jsonl"), MODEL_KEY, 1)
    events = tr.feed(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}, 1.0
    )
    events += tr.finish(stop_reason=None, error="boom", sent_at=0.0, at=2.0, price=lambda c: 1.0)
    request = next(e for e in events if isinstance(e, LLMRequest))
    assert (request.status, request.credits, request.cost_usd) == ("no_usage", 0.0, 0.0)
    assert request.error == "boom"
    assert request.duration_ms == 2000
    assert isinstance(events[-1], TurnError)
    assert events[-1].message == "boom"
    assert any(isinstance(e, AssistantMessage) and e.content == "hi" for e in events)


def test_missing_completion_on_success_records_reason():
    tr = TurnTranslator(Path("/tmp/x.jsonl"), MODEL_KEY, 1)
    events = tr.finish(stop_reason="end_turn", error=None, sent_at=0.0, at=1.0, price=lambda c: 0)
    request = next(e for e in events if isinstance(e, LLMRequest))
    assert request.error == NO_COMPLETION_ERROR
    assert isinstance(events[-1], TurnComplete)


def test_unmapped_tool_is_logged_under_its_tool_id():
    tr = TurnTranslator(Path("/tmp/x.jsonl"), MODEL_KEY, 1)
    events = tr.feed(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "w1",
            "title": "Searching the web",
            "status": "pending",
            "rawInput": {"query": "x"},
            "_meta": {"kiro": {"toolId": "web_search"}},
        },
        1.0,
    )
    events += tr.feed(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "w1",
            "status": "completed",
            "rawOutput": "found",
        },
        2.0,
    )
    call = next(e for e in events if isinstance(e, ToolCall))
    assert (call.name, call.input) == ("web_search", {"query": "x"})
    result = next(e for e in events if isinstance(e, ToolResult))
    assert (result.content, result.duration_ms) == ("found", 1000)


def test_tui_streams_both_text_segments_live():
    """The TUI streams text after a tool call because each segment has its own request id."""
    from unittest.mock import MagicMock, patch

    from archie_cli.tui.app import ArchieApp

    app = ArchieApp(
        ws_url="ws://127.0.0.1:7600/sessions/p-01/stream",
        api_url="http://127.0.0.1:7600/sessions/p-01",
        container_name="archie-p-01",
    )
    events = [e for e in _run("run1_parallel_shell") if e.scope is None]
    conv = MagicMock()
    streams: list[MagicMock] = []

    def begin_streaming():
        stream = MagicMock()
        streams.append(stream)
        return stream

    conv.begin_streaming.side_effect = begin_streaming
    status = MagicMock()

    def query_one(selector, _type=None):
        return {"#conversation": conv, "#status": status}.get(selector, MagicMock())

    with (
        patch.object(app, "query_one", side_effect=query_one),
        patch.object(app, "_update_accounting_status"),
    ):
        for event in events:
            app._apply_event(event)
    streamed = ["".join(c.args[0] for c in s.append.call_args_list) for s in streams]
    messages = [e.content for e in events if isinstance(e, AssistantMessage)]
    assert streamed == messages
    assert len(messages) == 3

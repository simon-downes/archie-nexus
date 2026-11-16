"""TurnTranslator on frames recorded from real Kiro ACP V3 runs (077 spike)."""

from __future__ import annotations

import json
from pathlib import Path

import msgspec
from archie_agent.kiro.translate import (
    NO_COMPLETION_ERROR,
    RootContext,
    TurnTranslator,
    context_pct,
)
from archie_shared.events import (
    AssistantMessage,
    CompactionComplete,
    CompactionStart,
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


def _context_frame(pct, **meta):
    return {
        "sessionUpdate": "session_info_update",
        "_meta": {"kiro": {"kind": "context_usage", "usagePercentage": pct, **meta}},
    }


def _finish(tr):
    return tr.finish(stop_reason="end_turn", error=None, sent_at=0.0, at=1.0, price=lambda c: c)


def test_turn_context_fields_from_root_frames():
    """Last, max and distinct count of root frames; subagent frames don't count (089)."""
    tr = TurnTranslator(Path("/tmp/x.jsonl"), MODEL_KEY, 1)
    for pct in (2.5, 2.5, 4.0, 3.0):
        assert tr.feed(_context_frame(pct), 1.0) == []
    tr.feed(_context_frame(9.0, agentSubtaskId="sub-1"), 1.0)
    tr.feed(
        {
            "sessionUpdate": "session_info_update",
            "_meta": {
                "kiro": {
                    "kind": "turn_completion",
                    "status": "success",
                    "promptTurnSummaries": [{"unit": "credit", "usage": 0.1}],
                    "requestIds": ["a", "b", "c"],
                }
            },
        },
        1.0,
    )
    request = next(e for e in _finish(tr) if isinstance(e, LLMRequest))
    assert (
        request.context_pct,
        request.context_pct_max,
        request.context_samples,
        request.kiro_requests,
    ) == (3.0, 4.0, 3, 3)


def test_turn_without_context_frames_leaves_fields_unset():
    events = _run("run1_parallel_shell")
    request = next(e for e in events if isinstance(e, LLMRequest))
    for name in ("context_pct", "context_pct_max", "context_samples"):
        assert getattr(request, name) is msgspec.UNSET
    tr = TurnTranslator(Path("/tmp/x.jsonl"), MODEL_KEY, 1)
    tr.feed(_context_frame(5.0, agentSubtaskId="sub-1"), 1.0)
    request = next(e for e in _finish(tr) if isinstance(e, LLMRequest))
    assert request.context_pct is msgspec.UNSET and request.kiro_requests is msgspec.UNSET


def test_context_pct_reads_only_numeric_root_context_frames():
    assert context_pct(_context_frame(1.5)) == 1.5
    assert context_pct(_context_frame(2)) == 2.0
    assert context_pct(_context_frame("3")) is None
    assert context_pct(_context_frame(True)) is None
    assert context_pct(_context_frame(1.5, agentSubtaskId="s")) is None
    assert context_pct({"sessionUpdate": "agent_message_chunk"}) is None
    frame = _context_frame(1.5)
    frame["_meta"]["kiro"]["kind"] = "turn_completion"
    assert context_pct(frame) is None


def test_current_archie_tool_format_uses_direct_raw_input():
    tr = TurnTranslator(Path("/tmp/x.jsonl"), MODEL_KEY, 1)
    events = tr.feed(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "r1",
            "title": "@archie/read",
            "status": "pending",
            "rawInput": {"path": "foo.py"},
        },
        1.0,
    )

    call = next(e for e in events if isinstance(e, ToolCall))
    assert (call.name, call.input) == ("read", {"path": "foo.py"})


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
    from tests.tui_fakes import make_controller

    controller, listener = make_controller(session_id="p-01")
    events = [e for e in _run("run1_parallel_shell") if e.scope is None]
    for event in events:
        controller.apply(event)
    # Each assistant segment streamed into its own row (one first-delta append per
    # segment) and the final messages replaced those streams in the transcript.
    builder = controller.builder
    texts = [
        item.content
        for item in [*builder.preamble, *(i for turn in builder.turns for i in turn.items)]
        if getattr(item, "role", "") == "assistant"
    ]
    stream_ids = [
        change.node.id
        for change in listener.changes()
        if change.kind == "append" and getattr(change.node, "kind", "") == "stream"
    ]
    messages = [e.content for e in events if isinstance(e, AssistantMessage)]
    assert texts == messages
    assert len(stream_ids) == len(messages)
    assert len(messages) == 3


# --- automatic compaction (091) -------------------------------------------------------


def _info(kind: str, **meta) -> dict:
    return {"sessionUpdate": "session_info_update", "_meta": {"kiro": {"kind": kind, **meta}}}


def _started() -> dict:
    return _info("summarization_started", summarization={"status": "running"})


def _completed(summary: str = "## Overview\nAuto.") -> dict:
    return _info(
        "summarization_completed",
        summarization={"status": "success", "summary": {"conversationSummary": summary}},
        conversationSummary=summary,
        truncated=False,
    )


def _failed(reason: str) -> dict:
    return _info("summarization_failed", summarization={"status": reason}, reason=reason)


def _ctx(pct: float) -> dict:
    return _info("context_usage", usagePercentage=pct)


def _say(text: str) -> dict:
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}


def _feed(updates, *, stop="end_turn", finish_at=20.0):
    tr = TurnTranslator(Path("/tmp/x.jsonl"), MODEL_KEY, 2)
    events = []
    for at, update in enumerate(updates, start=10):
        events.extend(tr.feed(update, float(at)))
    events.extend(
        tr.finish(stop_reason=stop, error=None, sent_at=9.0, at=finish_at, price=lambda c: c)
    )
    return events


def test_automatic_compaction_maps_to_a_threshold_compaction():
    events = _feed([_say("Answer."), _started(), _completed(), _ctx(20), _ctx(20), _ctx(4.2)])
    start = next(e for e in events if isinstance(e, CompactionStart))
    complete = next(e for e in events if isinstance(e, CompactionComplete))
    assert (start.reason, start.tokens_before, start.model_key, start.scope) == (
        "threshold",
        0,
        MODEL_KEY,
        None,
    )
    assert (complete.status, complete.summary, complete.request_id) == (
        "success",
        "## Overview\nAuto.",
        start.request_id,
    )
    assert (complete.tokens_after, complete.duration_ms) == (None, 1000)
    # The text before the compaction is flushed first.
    names = [type(e).__name__ for e in events]
    assert names.index("AssistantMessage") < names.index("CompactionStart")
    request = next(e for e in events if isinstance(e, LLMRequest))
    assert (request.context_pct, request.context_pct_max, request.context_samples) == (4.2, 4.2, 1)


def test_text_after_a_compaction_opens_a_new_iteration():
    events = _feed([_say("Answer."), _started(), _completed(), _say("Ready.")])
    deltas = [e for e in events if isinstance(e, TextDelta)]
    assert [d.iteration for d in deltas] == [0, 1]
    assert deltas[0].request_id != deltas[1].request_id


def test_completed_without_started_synthesises_the_start():
    events = _feed([_completed()])
    start, complete = [e for e in events if isinstance(e, CompactionStart | CompactionComplete)]
    assert complete.request_id == start.request_id and complete.duration_ms == 0


def test_failed_and_cancelled_compactions():
    failed = _feed([_started(), _failed("error")])
    (complete,) = [e for e in failed if isinstance(e, CompactionComplete)]
    assert (complete.status, complete.error) == ("failure", "Kiro compaction failed: error")
    cancelled = _feed([_started(), _failed("canceled")])
    (complete,) = [e for e in cancelled if isinstance(e, CompactionComplete)]
    assert complete.status == "interrupted"


def test_turn_ending_mid_compaction_interrupts_it_before_the_terminal():
    events = _feed([_started()], stop="cancelled")
    names = [type(e).__name__ for e in events]
    assert names.index("CompactionComplete") < names.index("TurnInterrupted")
    (complete,) = [e for e in events if isinstance(e, CompactionComplete)]
    assert complete.status == "interrupted" and complete.duration_ms == 10_000


def test_turn_error_mid_compaction_keeps_the_turn_error_text():
    tr = TurnTranslator(Path("/tmp/x.jsonl"), MODEL_KEY, 2)
    tr.feed(_started(), 10.0)
    events = tr.finish(
        stop_reason=None, error="Kiro backend stopped", sent_at=9.0, at=11.0, price=lambda c: c
    )
    request = next(e for e in events if isinstance(e, LLMRequest))
    assert request.error == "Kiro backend stopped"
    assert next(e for e in events if isinstance(e, TurnError)).message == "Kiro backend stopped"
    cancelled = _feed([_started()], stop="cancelled")
    # No turn_completion came, so the usual error, not the compaction's.
    assert next(e for e in cancelled if isinstance(e, LLMRequest)).error == NO_COMPLETION_ERROR


def test_root_context_drops_the_placeholder_after_a_summary():
    ctx = RootContext()
    assert ctx.feed(_ctx(20)) == 20.0  # not armed: a real 20 passes
    assert ctx.feed(_completed()) is None
    assert [ctx.feed(_ctx(20)), ctx.feed(_ctx(20)), ctx.feed(_ctx(4.2))] == [None, None, 4.2]
    assert ctx.feed(_ctx(20)) == 20.0  # disarmed by the real figure
    sub = _completed()
    sub["_meta"]["kiro"]["agentSubtaskId"] = "sub1"
    assert ctx.feed(sub) is None
    assert ctx.feed(_ctx(20)) == 20.0  # a subagent's compaction doesn't arm it

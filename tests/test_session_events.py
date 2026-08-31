"""Canonical event scope and structured turn-field contract tests."""

from __future__ import annotations

from archie_shared.canonical_events import (
    AssistantMessage,
    IterationStart,
    LLMRequest,
    TextDelta,
    ToolCall,
    ToolResult,
    UserMessage,
    decode_event,
    encode_event,
)


def _roundtrip(event):
    return decode_event(encode_event(event))


def test_scoped_events_roundtrip_with_none_scope():
    events = [
        UserMessage(id="u1", turn=1, scope=None, content="hi"),
        IterationStart(id="i1", turn=1, iteration=1, scope=None),
        TextDelta(id="t1", turn=1, iteration=1, scope=None, request_id="r1", text="x"),
        ToolCall(
            id="c1",
            turn=1,
            iteration=1,
            scope=None,
            request_id="r1",
            tool_use_id="tu1",
            name="read",
            input={"path": "/x"},
        ),
        ToolResult(
            id="tr1",
            turn=1,
            iteration=1,
            scope=None,
            request_id="r1",
            tool_use_id="tu1",
            content="ok",
            is_error=False,
            duration_ms=1,
            result_bytes=2,
        ),
        AssistantMessage(
            id="a1",
            turn=1,
            scope=None,
            request_ids=["r1"],
            content="done",
            interrupted=False,
        ),
    ]
    for ev in events:
        restored = _roundtrip(ev)
        assert restored.scope is None
        if isinstance(ev, (IterationStart, TextDelta, ToolCall, ToolResult)):
            assert restored.turn == 1
            assert restored.iteration == 1


def test_assistant_message_has_no_iteration_backcompat_field():
    raw = (
        '{"type":"assistant_message","id":"a1","turn":1,"scope":null,'
        '"request_ids":["r1"],"content":"response","interrupted":false}'
    )

    event = decode_event(raw)

    assert isinstance(event, AssistantMessage)
    assert event.content == "response"
    assert not hasattr(event, "turn_iteration")


def test_scoped_events_roundtrip_with_child_scope():
    child = "tu-launch-123"
    events = [
        UserMessage(id="u1", turn=1, scope=child, content="hi"),
        IterationStart(id="i1", turn=1, iteration=1, scope=child),
        LLMRequest(
            id="r1",
            scope=child,
            turn=1,
            iteration=1,
            model_key="m",
            sent_at="2026-07-01T10:00:00+00:00",
            duration_ms=1,
            status="completed",
            input_tokens=1,
            output_tokens=1,
            cache_read_tokens=0,
            cache_write_tokens=0,
            context_tokens=1,
            cost_usd=0.0,
        ),
        ToolCall(
            id="c1",
            turn=1,
            iteration=1,
            scope=child,
            request_id="r1",
            tool_use_id="tu2",
            name="read",
            input={},
        ),
    ]
    for ev in events:
        assert _roundtrip(ev).scope == child


def _fake_subagent_stream():
    """Produce a canonical stream for root → child → nested-child."""
    root_launch = ToolCall(
        id="c-root",
        turn=1,
        iteration=1,
        scope=None,
        request_id="r-root",
        tool_use_id="child-a",
        name="task",
        input={"prompt": "..."},
    )
    child_launch = ToolCall(
        id="c-child",
        turn=1,
        iteration=1,
        scope="child-a",
        request_id="r-child",
        tool_use_id="child-b",
        name="task",
        input={"prompt": "..."},
    )
    root_req = LLMRequest(
        id="r-root",
        scope=None,
        turn=1,
        iteration=1,
        model_key="m",
        sent_at="2026-07-01T10:00:00+00:00",
        duration_ms=1,
        status="completed",
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        context_tokens=1,
        cost_usd=0.01,
    )
    child_req = LLMRequest(
        id="r-child",
        scope="child-a",
        turn=1,
        iteration=1,
        model_key="m",
        sent_at="2026-07-01T10:00:01+00:00",
        duration_ms=1,
        status="completed",
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        context_tokens=1,
        cost_usd=0.02,
    )
    nested_req = LLMRequest(
        id="r-nested",
        scope="child-b",
        turn=1,
        iteration=1,
        model_key="m",
        sent_at="2026-07-01T10:00:02+00:00",
        duration_ms=1,
        status="completed",
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        context_tokens=1,
        cost_usd=0.04,
    )
    return [root_req, root_launch, child_req, child_launch, nested_req]


def test_fake_subagent_scope_chain_reconstructable():
    events = [_roundtrip(e) for e in _fake_subagent_stream()]

    parent_of = {e.tool_use_id: e.scope for e in events if isinstance(e, ToolCall)}
    assert parent_of["child-a"] is None
    assert parent_of["child-b"] == "child-a"

    reqs = [e for e in events if isinstance(e, LLMRequest)]
    keys = {(r.scope, r.turn, r.iteration, r.id) for r in reqs}
    assert len(keys) == 3


def test_two_children_same_turn_and_iteration_kept_distinct_by_scope():
    a = LLMRequest(
        id="r-a",
        scope="child-a",
        turn=1,
        iteration=1,
        model_key="m",
        sent_at="t",
        duration_ms=1,
        status="completed",
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        context_tokens=1,
        cost_usd=0.01,
    )
    b = LLMRequest(
        id="r-b",
        scope="child-b",
        turn=1,
        iteration=1,
        model_key="m",
        sent_at="t",
        duration_ms=1,
        status="completed",
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        context_tokens=1,
        cost_usd=0.02,
    )
    keys = {(a.scope, a.turn, a.iteration), (b.scope, b.turn, b.iteration)}
    assert len(keys) == 2

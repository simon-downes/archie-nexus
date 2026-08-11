"""Canonical event scope-field contract tests (029 M6).

Validates that scope is present and round-trips on all scoped canonical events,
and that a fake scoped event producer (representing a future subagent) yields a
consistent scope chain.
"""

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


# ---------------------------------------------------------------------------
# Scope fields present and round-trip on all scoped events
# ---------------------------------------------------------------------------


def test_scoped_events_roundtrip_with_none_scope():
    events = [
        UserMessage(id="u1", turn=1, scope=None, content="hi"),
        IterationStart(id="i1", turn_iteration="1.1", scope=None, index=1),
        TextDelta(id="t1", turn_iteration="1.1", scope=None, request_id="r1", text="x"),
        ToolCall(
            id="c1",
            turn_iteration="1.1",
            scope=None,
            request_id="r1",
            tool_use_id="tu1",
            name="read",
            input={"path": "/x"},
        ),
        ToolResult(
            id="tr1",
            turn_iteration="1.1",
            scope=None,
            request_id="r1",
            tool_use_id="tu1",
            content="ok",
            is_error=False,
            duration_ms=1,
            result_bytes=2,
        ),
        AssistantMessage(
            id="a1", turn=1, scope=None, request_ids=["r1"], content="done", interrupted=False
        ),
    ]
    for ev in events:
        assert _roundtrip(ev).scope is None


def test_scoped_events_roundtrip_with_child_scope():
    child = "tu-launch-123"
    events = [
        UserMessage(id="u1", turn=1, scope=child, content="hi"),
        IterationStart(id="i1", turn_iteration="1.1", scope=child, index=1),
        LLMRequest(
            id="r1",
            scope=child,
            turn_iteration="1.1",
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
            turn_iteration="1.1",
            scope=child,
            request_id="r1",
            tool_use_id="tu2",
            name="read",
            input={},
        ),
    ]
    for ev in events:
        assert _roundtrip(ev).scope == child


# ---------------------------------------------------------------------------
# Fake scoped event producer (future subagent) — scope-chain consistency
# ---------------------------------------------------------------------------


def _fake_subagent_stream():
    """Produce a canonical stream for root → child → nested-child.

    Child scope == launching tool_use_id per the subagent scope contract.
    """
    root_launch = ToolCall(
        id="c-root",
        turn_iteration="1.1",
        scope=None,
        request_id="r-root",
        tool_use_id="child-a",
        name="task",
        input={"prompt": "..."},
    )
    child_launch = ToolCall(
        id="c-child",
        turn_iteration="1.1",
        scope="child-a",
        request_id="r-child",
        tool_use_id="child-b",
        name="task",
        input={"prompt": "..."},
    )
    root_req = LLMRequest(
        id="r-root",
        scope=None,
        turn_iteration="1.1",
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
        turn_iteration="1.1",
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
        turn_iteration="1.1",
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

    # parent_of(child) reconstructed from the launching tool_call's scope
    parent_of = {e.tool_use_id: e.scope for e in events if isinstance(e, ToolCall)}
    assert parent_of["child-a"] is None  # root launched child-a
    assert parent_of["child-b"] == "child-a"  # child-a launched child-b

    # request identity (scope, turn_iteration, request_id/id) is distinct per scope
    reqs = [e for e in events if isinstance(e, LLMRequest)]
    keys = {(r.scope, r.turn_iteration, r.id) for r in reqs}
    assert len(keys) == 3


def test_two_children_same_turn_iteration_kept_distinct_by_scope():
    a = LLMRequest(
        id="r-a",
        scope="child-a",
        turn_iteration="1.1",
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
        turn_iteration="1.1",
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
    keys = {(a.scope, a.turn_iteration), (b.scope, b.turn_iteration)}
    assert len(keys) == 2

"""Scope-based cost aggregation for canonical request events (029 M6).

Validates the subagent scope contract: child scope = launching tool_use_id,
one llm_request per child request, and direct vs inclusive cost rollup by scope.
"""

from __future__ import annotations

from archie_shared.canonical_events import LLMRequest, ToolCall
from archie_shared.session import scope_direct_costs, scope_inclusive_costs

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _llm(
    scope: str | None,
    cost: float,
    *,
    request_id: str = "r1",
    turn_iteration: str = "1.1",
) -> LLMRequest:
    return LLMRequest(
        id=f"req-{scope}-{request_id}",
        scope=scope,
        turn_iteration=turn_iteration,
        model_key="bedrock-anthropic.claude-sonnet-4-6",
        sent_at="2026-07-01T10:00:00+00:00",
        duration_ms=100,
        status="completed",
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=0,
        cache_write_tokens=0,
        context_tokens=100,
        cost_usd=cost,
    )


def _launch(parent_scope: str | None, child_scope: str, *, turn_iteration: str = "1.1") -> ToolCall:
    """A tool_call issued *by* parent_scope that launches child_scope (tool_use_id)."""
    return ToolCall(
        id=f"tc-{child_scope}",
        turn_iteration=turn_iteration,
        scope=parent_scope,
        request_id="r1",
        tool_use_id=child_scope,
        name="task",
        input={},
    )


# ---------------------------------------------------------------------------
# Root only
# ---------------------------------------------------------------------------


def test_root_direct_and_inclusive():
    events = [_llm(None, 0.01), _llm(None, 0.02)]
    assert scope_direct_costs(events) == {None: 0.03}
    assert scope_inclusive_costs(events) == {None: 0.03}


# ---------------------------------------------------------------------------
# Single child
# ---------------------------------------------------------------------------


def test_child_direct_isolated_from_root():
    events = [
        _llm(None, 0.01),
        _launch(None, "child-a"),
        _llm("child-a", 0.05),
    ]
    direct = scope_direct_costs(events)
    assert direct == {None: 0.01, "child-a": 0.05}


def test_child_rolls_up_into_root_inclusive():
    events = [
        _llm(None, 0.01),
        _launch(None, "child-a"),
        _llm("child-a", 0.05),
    ]
    inclusive = scope_inclusive_costs(events)
    assert inclusive["child-a"] == 0.05
    assert inclusive[None] == 0.06  # root direct 0.01 + child 0.05


# ---------------------------------------------------------------------------
# Nested child
# ---------------------------------------------------------------------------


def test_nested_child_rolls_up_through_chain():
    events = [
        _llm(None, 0.01),
        _launch(None, "child-a"),
        _llm("child-a", 0.02),
        _launch("child-a", "child-b"),  # grandchild launched by child-a
        _llm("child-b", 0.04),
    ]
    direct = scope_direct_costs(events)
    assert direct == {None: 0.01, "child-a": 0.02, "child-b": 0.04}

    inclusive = scope_inclusive_costs(events)
    assert inclusive["child-b"] == 0.04
    assert inclusive["child-a"] == 0.06  # own 0.02 + grandchild 0.04
    assert inclusive[None] == 0.07  # root 0.01 + child-a 0.02 + child-b 0.04


def test_child_model_and_cost_independent_of_parent():
    """A child using a different (e.g. zero-cost local) model does not affect the parent's direct cost."""
    events = [
        _llm(None, 0.10),
        _launch(None, "child-a"),
        LLMRequest(
            id="req-child-local",
            scope="child-a",
            turn_iteration="1.1",
            model_key="ollama-qwen3:30b-a3b",
            sent_at="2026-07-01T10:00:00+00:00",
            duration_ms=100,
            status="completed",
            input_tokens=10,
            output_tokens=5,
            cache_read_tokens=0,
            cache_write_tokens=0,
            context_tokens=100,
            cost_usd=0.0,
        ),
    ]
    direct = scope_direct_costs(events)
    assert direct[None] == 0.10
    assert direct["child-a"] == 0.0
    assert scope_inclusive_costs(events)[None] == 0.10


# ---------------------------------------------------------------------------
# Edge cases from the plan
# ---------------------------------------------------------------------------


def test_child_error_before_usage_still_attributed():
    """An error request with zero cost keeps the child scope present in aggregation."""
    events = [
        _launch(None, "child-a"),
        LLMRequest(
            id="req-child-err",
            scope="child-a",
            turn_iteration="1.1",
            model_key="bedrock-anthropic.claude-sonnet-4-6",
            sent_at="2026-07-01T10:00:00+00:00",
            duration_ms=5,
            status="error",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            context_tokens=0,
            cost_usd=0.0,
            error="boom",
        ),
    ]
    assert scope_direct_costs(events) == {"child-a": 0.0}


def test_orphan_child_rolls_up_to_root():
    """A scope with no launching tool_call (unknown parent) rolls up to root."""
    events = [_llm("orphan", 0.03)]
    inclusive = scope_inclusive_costs(events)
    assert inclusive["orphan"] == 0.03
    assert inclusive[None] == 0.03


def test_cycle_guard_terminates():
    """A pathological parent cycle does not hang and does not over-count."""
    events = [
        ToolCall(
            id="tc-a",
            turn_iteration="1.1",
            scope="b",
            request_id="r1",
            tool_use_id="a",
            name="task",
            input={},
        ),
        ToolCall(
            id="tc-b",
            turn_iteration="1.1",
            scope="a",
            request_id="r1",
            tool_use_id="b",
            name="task",
            input={},
        ),
        _llm("a", 0.01),
    ]
    inclusive = scope_inclusive_costs(events)
    assert inclusive["a"] == 0.01  # own direct cost; cycle does not double-count
    assert inclusive["b"] == 0.01  # rolled up once into parent

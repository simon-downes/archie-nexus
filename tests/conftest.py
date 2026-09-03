"""Shared pytest fixtures and helpers for canonical session event streams.

These factories build canonical events (`archie_shared.events`) with
sensible defaults so tests can construct scoped request/tool streams concisely.
See the subagent scope contract in
`shared/src/archie_shared/session/accounting.py`.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from archie_shared.events import (
    AssistantMessage,
    IterationStart,
    LLMRequest,
    ToolCall,
    ToolResult,
    UserMessage,
)


@pytest.fixture
def make_llm_request() -> Callable[..., LLMRequest]:
    """Factory for `LLMRequest` events with defaults.

    Usage: ``make_llm_request(scope="tc-1", cost_usd=0.01)``.
    """

    def _make(
        scope: str | None = None,
        *,
        cost_usd: float = 0.0,
        request_id: str = "r1",
        turn: int = 1,
        iteration: int = 1,
        model_key: str = "bedrock-anthropic.claude-sonnet-4-6",
        status: str = "completed",
        input_tokens: int = 10,
        output_tokens: int = 5,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        context_tokens: int = 100,
        error: str | None = None,
    ) -> LLMRequest:
        return LLMRequest(
            id=f"req-{scope}-{request_id}",
            scope=scope,
            turn=turn,
            iteration=iteration,
            model_key=model_key,
            sent_at="2026-07-01T10:00:00+00:00",
            duration_ms=100,
            status=status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            context_tokens=context_tokens,
            cost_usd=cost_usd,
            error=error,
        )

    return _make


@pytest.fixture
def make_launch_tool_call() -> Callable[..., ToolCall]:
    """Factory for a `task` `ToolCall` issued by ``parent_scope`` that launches
    ``child_scope`` (the child's ``tool_use_id``)."""

    def _make(
        parent_scope: str | None,
        child_scope: str,
        *,
        turn: int = 1,
        iteration: int = 1,
        request_id: str = "r1",
    ) -> ToolCall:
        return ToolCall(
            id=f"tc-{child_scope}",
            turn=turn,
            iteration=iteration,
            scope=parent_scope,
            request_id=request_id,
            tool_use_id=child_scope,
            name="task",
            input={},
        )

    return _make


@pytest.fixture
def make_tool_call() -> Callable[..., ToolCall]:
    """Factory for a generic (non-launching) `ToolCall` event."""

    def _make(
        scope: str | None = None,
        *,
        name: str = "read",
        tool_use_id: str = "tu1",
        request_id: str = "r1",
        turn: int = 1,
        iteration: int = 1,
        tool_input: dict | None = None,
    ) -> ToolCall:
        return ToolCall(
            id=f"tc-{tool_use_id}",
            turn=turn,
            iteration=iteration,
            scope=scope,
            request_id=request_id,
            tool_use_id=tool_use_id,
            name=name,
            input=tool_input or {},
        )

    return _make


@pytest.fixture
def make_event_stream(make_llm_request, make_launch_tool_call):
    """Build a simple root→child→nested scoped stream for accounting tests.

    Returns a list of canonical events: a root request, a launch tool_call and
    request for one child, and a launch tool_call and request for a nested child.
    """

    def _make(
        *,
        root_cost: float = 0.01,
        child_cost: float = 0.02,
        nested_cost: float = 0.04,
        child_scope: str = "tc-child",
        nested_scope: str = "tc-nested",
    ):
        return [
            make_llm_request(None, cost_usd=root_cost),
            make_launch_tool_call(None, child_scope),
            make_llm_request(child_scope, cost_usd=child_cost, request_id="r2"),
            make_launch_tool_call(child_scope, nested_scope),
            make_llm_request(nested_scope, cost_usd=nested_cost, request_id="r3"),
        ]

    return _make


@pytest.fixture
def canonical_events():
    """Expose canonical event classes for tests that build ad-hoc streams."""

    return {
        "UserMessage": UserMessage,
        "AssistantMessage": AssistantMessage,
        "IterationStart": IterationStart,
        "LLMRequest": LLMRequest,
        "ToolCall": ToolCall,
        "ToolResult": ToolResult,
    }

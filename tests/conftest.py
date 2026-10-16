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

pytest_plugins = ["tests.datadog_fakes"]


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


class SessionLogWriter:
    """Write canonical session logs under a temporary ARCHIE_HOME_DIR."""

    def __init__(self, home) -> None:
        self.dir = home / "sessions"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._n = 0

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n:04d}"

    def write(self, session_id: str, events) -> object:
        from archie_shared.session.log import SessionLog

        log = SessionLog(self.dir / f"{session_id}.jsonl")
        for event in events:
            log.append(event)
        return log.path

    def user(self, content: str, *, turn: int = 1, scope=None, index=None) -> UserMessage:
        return UserMessage(
            id=self._id("um"), turn=turn, scope=scope, content=content, subagent_index=index
        )

    def iteration(self, *, turn: int = 1, iteration: int = 1, scope=None, index=None):
        return IterationStart(
            id=self._id("it"), turn=turn, iteration=iteration, scope=scope, subagent_index=index
        )

    def assistant(
        self,
        content: str,
        *,
        turn: int = 1,
        iteration: int = 1,
        request_id: str = "r1",
        scope=None,
        index=None,
    ) -> AssistantMessage:
        return AssistantMessage(
            id=self._id("am"),
            turn=turn,
            iteration=iteration,
            scope=scope,
            request_id=request_id,
            content=content,
            interrupted=False,
            subagent_index=index,
        )

    def llm(
        self,
        *,
        model_key: str = "m1",
        turn: int = 1,
        iteration: int = 1,
        scope=None,
        index=None,
        purpose: str = "conversation",
        input_tokens: int = 10,
        output_tokens: int = 5,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> LLMRequest:
        return LLMRequest(
            id=self._id("lr"),
            scope=scope,
            turn=turn,
            iteration=iteration,
            model_key=model_key,
            sent_at="2026-07-01T10:00:00+00:00",
            duration_ms=100,
            status="completed",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            context_tokens=100,
            cost_usd=cost_usd,
            subagent_index=index,
            purpose=purpose,
        )

    def tool_call(
        self,
        name: str,
        tool_input: dict,
        *,
        tool_use_id: str,
        turn: int = 1,
        iteration: int = 1,
        scope=None,
        index=None,
    ) -> ToolCall:
        return ToolCall(
            id=self._id("tc"),
            turn=turn,
            iteration=iteration,
            scope=scope,
            request_id="r1",
            tool_use_id=tool_use_id,
            name=name,
            input=tool_input,
            subagent_index=index,
        )

    def tool_result(
        self,
        content: str,
        *,
        tool_use_id: str,
        is_error: bool = False,
        turn: int = 1,
        iteration: int = 1,
        scope=None,
        index=None,
        duration_ms: int = 0,
    ) -> ToolResult:
        return ToolResult(
            id=self._id("tr"),
            turn=turn,
            iteration=iteration,
            scope=scope,
            request_id="r1",
            tool_use_id=tool_use_id,
            content=content,
            is_error=is_error,
            duration_ms=duration_ms,
            result_lines=len(content.splitlines()),
            result_bytes=len(content.encode()),
            subagent_index=index,
        )


@pytest.fixture
def session_logs(tmp_path, monkeypatch) -> SessionLogWriter:
    """A SessionLogWriter rooted at a temporary ARCHIE_HOME_DIR."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    return SessionLogWriter(tmp_path)


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

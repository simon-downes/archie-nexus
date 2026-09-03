"""Pure construction helpers for canonical session events."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

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
from archie_shared.models import ModelEntry, calculate_cost
from ulid import ULID

from archie_agent.loop_events import Usage


class EventFactory:
    """Create canonical events with immutable model/accounting data.

    Construction is intentionally separate from persistence and delivery. The
    path argument remains accepted for compatibility with existing callers but
    is not accessed.
    """

    def __init__(
        self,
        path: Path,
        model_key: str,
        model: ModelEntry,
        scope: str | None = None,
        subagent_index: int | None = None,
    ):
        self.path = path
        self.model_key = model_key
        self.model = model
        self.scope = scope
        self.subagent_index = subagent_index

    def request(
        self,
        *,
        turn: int,
        iteration: int,
        sent_at: str,
        duration_ms: int,
        status: Literal["completed", "interrupted", "error", "no_usage"],
        usage: Usage | None = None,
        stop_reason: str | None = None,
        error: str | None = None,
        request_id: str | None = None,
    ) -> LLMRequest:
        actual_usage = usage
        usage = usage or Usage(0, 0, 0, 0)
        context_tokens = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
        cost = (
            0.0
            if status != "completed" or actual_usage is None
            else calculate_cost(
                self.model.cost,
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
            )
        )
        return LLMRequest(
            id=request_id or str(ULID()),
            scope=self.scope,
            subagent_index=self.subagent_index,
            turn=turn,
            iteration=iteration,
            model_key=self.model_key,
            sent_at=sent_at,
            duration_ms=max(0, duration_ms),
            status=status,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            context_tokens=context_tokens,
            cost_usd=round(cost, 6),
            stop_reason=stop_reason,
            error=error,
        )

    def iteration_start(self, *, turn: int, iteration: int) -> IterationStart:
        return IterationStart(
            id=str(ULID()),
            turn=turn,
            iteration=iteration,
            scope=self.scope,
            subagent_index=self.subagent_index,
        )

    def text_delta(self, *, turn: int, iteration: int, request_id: str, text: str) -> TextDelta:
        return TextDelta(
            id=str(ULID()),
            turn=turn,
            iteration=iteration,
            scope=self.scope,
            subagent_index=self.subagent_index,
            request_id=request_id,
            text=text,
        )

    def tool_call(
        self,
        *,
        turn: int,
        iteration: int,
        request_id: str,
        tool_use_id: str,
        name: str,
        input: dict[str, object],
    ) -> ToolCall:
        return ToolCall(
            id=str(ULID()),
            turn=turn,
            iteration=iteration,
            scope=self.scope,
            subagent_index=self.subagent_index,
            request_id=request_id,
            tool_use_id=tool_use_id,
            name=name,
            input=input,
        )

    def tool_result(
        self,
        *,
        turn: int,
        iteration: int,
        request_id: str,
        tool_use_id: str,
        content: str,
        is_error: bool,
        duration_ms: int,
        result_bytes: int,
        result_lines: int = 0,
    ) -> ToolResult:
        return ToolResult(
            id=str(ULID()),
            turn=turn,
            iteration=iteration,
            scope=self.scope,
            subagent_index=self.subagent_index,
            request_id=request_id,
            tool_use_id=tool_use_id,
            content=content,
            is_error=is_error,
            duration_ms=duration_ms,
            result_bytes=result_bytes,
            result_lines=result_lines,
        )

    def assistant_message(
        self,
        *,
        turn: int,
        iteration: int,
        request_id: str,
        content: str,
        interrupted: bool,
    ) -> AssistantMessage:
        return AssistantMessage(
            id=str(ULID()),
            turn=turn,
            iteration=iteration,
            scope=self.scope,
            subagent_index=self.subagent_index,
            request_id=request_id,
            content=content,
            interrupted=interrupted,
        )

    def turn_complete(self, *, turn: int, stop_reason: str) -> TurnComplete:
        return TurnComplete(
            id=str(ULID()),
            turn=turn,
            scope=self.scope,
            subagent_index=self.subagent_index,
            stop_reason=stop_reason,
        )

    def turn_error(self, *, turn: int, message: str) -> TurnError:
        return TurnError(
            id=str(ULID()),
            turn=turn,
            scope=self.scope,
            subagent_index=self.subagent_index,
            message=message,
        )

    def turn_interrupted(self, *, turn: int) -> TurnInterrupted:
        return TurnInterrupted(
            id=str(ULID()),
            turn=turn,
            scope=self.scope,
            subagent_index=self.subagent_index,
        )


def now_utc() -> str:
    return datetime.now(UTC).isoformat()

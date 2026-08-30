"""Construction and persistence helpers for canonical session events."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from archie_shared.canonical_events import (
    AssistantMessage,
    IterationStart,
    LLMRequest,
    TextDelta,
    ToolCall,
    ToolResult,
    TurnComplete,
    TurnError,
    TurnInterrupted,
    encode_event,
)
from archie_shared.models import ModelEntry, calculate_cost
from archie_shared.session.log import append_event
from ulid import ULID

from archie_agent.events import Usage


class EventFactory:
    """Create canonical request events with immutable model/accounting data."""

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
        turn_iteration: str,
        sent_at: str,
        duration_ms: int,
        status: Literal["completed", "interrupted", "error", "no_usage"],
        usage: Usage | None = None,
        stop_reason: str | None = None,
        error: str | None = None,
        request_id: str | None = None,
    ) -> tuple[LLMRequest, str]:
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
        event = LLMRequest(
            id=request_id or str(ULID()),
            scope=self.scope,
            subagent_index=self.subagent_index,
            turn_iteration=turn_iteration,
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
        serialized = encode_event(event)
        append_event(self.path, event, serialized)
        return event, serialized

    def iteration_start(self, *, turn_iteration: str, index: int) -> tuple[IterationStart, str]:
        event = IterationStart(
            id=str(ULID()),
            turn_iteration=turn_iteration,
            scope=self.scope,
            subagent_index=self.subagent_index,
            index=index,
        )
        serialized = encode_event(event)
        append_event(self.path, event, serialized)
        return event, serialized

    def text_delta(
        self, *, turn_iteration: str, request_id: str, text: str
    ) -> tuple[TextDelta, str]:
        """Live-only event — broadcast but NOT persisted (excluded from PersistedEvent)."""
        event = TextDelta(
            id=str(ULID()),
            turn_iteration=turn_iteration,
            scope=self.scope,
            subagent_index=self.subagent_index,
            request_id=request_id,
            text=text,
        )
        return event, encode_event(event)

    def tool_call(
        self,
        *,
        turn_iteration: str,
        request_id: str,
        tool_use_id: str,
        name: str,
        input: dict[str, object],
    ) -> tuple[ToolCall, str]:
        event = ToolCall(
            id=str(ULID()),
            turn_iteration=turn_iteration,
            scope=self.scope,
            subagent_index=self.subagent_index,
            request_id=request_id,
            tool_use_id=tool_use_id,
            name=name,
            input=input,
        )
        serialized = encode_event(event)
        append_event(self.path, event, serialized)
        return event, serialized

    def tool_result(
        self,
        *,
        turn_iteration: str,
        request_id: str,
        tool_use_id: str,
        content: str,
        is_error: bool,
        duration_ms: int,
        result_bytes: int,
        result_lines: int = 0,
    ) -> tuple[ToolResult, str]:
        event = ToolResult(
            id=str(ULID()),
            turn_iteration=turn_iteration,
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
        serialized = encode_event(event)
        append_event(self.path, event, serialized)
        return event, serialized

    def assistant_message(
        self,
        *,
        turn: int,
        turn_iteration: str,
        request_ids: list[str],
        content: str,
        interrupted: bool,
    ) -> tuple[AssistantMessage, str]:
        event = AssistantMessage(
            id=str(ULID()),
            turn=turn,
            turn_iteration=turn_iteration,
            scope=self.scope,
            subagent_index=self.subagent_index,
            request_ids=list(request_ids),
            content=content,
            interrupted=interrupted,
        )
        serialized = encode_event(event)
        append_event(self.path, event, serialized)
        return event, serialized

    def turn_complete(self, *, turn: int, stop_reason: str) -> tuple[TurnComplete, str]:
        event = TurnComplete(id=str(ULID()), turn=turn, scope=self.scope, subagent_index=self.subagent_index, stop_reason=stop_reason)
        serialized = encode_event(event)
        append_event(self.path, event, serialized)
        return event, serialized

    def turn_error(self, *, turn: int, message: str) -> tuple[TurnError, str]:
        event = TurnError(id=str(ULID()), turn=turn, scope=self.scope, subagent_index=self.subagent_index, message=message)
        serialized = encode_event(event)
        append_event(self.path, event, serialized)
        return event, serialized

    def turn_interrupted(self, *, turn: int) -> tuple[TurnInterrupted, str]:
        event = TurnInterrupted(id=str(ULID()), turn=turn, scope=self.scope, subagent_index=self.subagent_index)
        serialized = encode_event(event)
        append_event(self.path, event, serialized)
        return event, serialized


def now_utc() -> str:
    return datetime.now(UTC).isoformat()

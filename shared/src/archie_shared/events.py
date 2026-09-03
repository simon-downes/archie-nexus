"""Public server-to-client event schema for live delivery and session history."""

from __future__ import annotations

from typing import ClassVar, Literal

import msgspec


class Event(msgspec.Struct, forbid_unknown_fields=True):
    """Base public event with a non-serialized persistence declaration."""

    persist: ClassVar[bool] = False


class PersistedEvent(Event):
    """Base for events that belong in durable session history."""

    persist: ClassVar[bool] = True


class SessionStarted(
    PersistedEvent, tag="session_started", tag_field="type", forbid_unknown_fields=True
):
    id: str
    schema_version: int
    sent_at: str
    model_key: str


class UserMessage(PersistedEvent, tag="user_message", tag_field="type", forbid_unknown_fields=True):
    id: str
    turn: int
    scope: str | None
    content: str
    subagent_index: int | None = None


class IterationStart(
    PersistedEvent, tag="iteration_start", tag_field="type", forbid_unknown_fields=True
):
    id: str
    turn: int
    iteration: int
    scope: str | None
    subagent_index: int | None = None


class TextDelta(Event, tag="text_delta", tag_field="type", forbid_unknown_fields=True):
    id: str
    turn: int
    iteration: int
    scope: str | None
    request_id: str
    text: str
    subagent_index: int | None = None


class LLMRequest(PersistedEvent, tag="llm_request", tag_field="type", forbid_unknown_fields=True):
    id: str
    scope: str | None
    turn: int
    iteration: int
    model_key: str
    sent_at: str
    duration_ms: int
    status: Literal["completed", "interrupted", "error", "no_usage"]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    context_tokens: int
    cost_usd: float
    stop_reason: str | None = None
    error: str | None = None
    subagent_index: int | None = None


class ToolCall(PersistedEvent, tag="tool_call", tag_field="type", forbid_unknown_fields=True):
    id: str
    turn: int
    iteration: int
    scope: str | None
    request_id: str
    tool_use_id: str
    name: str
    input: dict[str, object]
    subagent_index: int | None = None


class ToolResult(PersistedEvent, tag="tool_result", tag_field="type", forbid_unknown_fields=True):
    id: str
    turn: int
    iteration: int
    scope: str | None
    request_id: str
    tool_use_id: str
    content: str
    is_error: bool
    duration_ms: int = 0
    result_bytes: int = 0
    result_lines: int = 0
    subagent_index: int | None = None


class AssistantMessage(
    PersistedEvent, tag="assistant_message", tag_field="type", forbid_unknown_fields=True
):
    id: str
    turn: int
    iteration: int
    scope: str | None
    request_id: str
    content: str
    interrupted: bool
    subagent_index: int | None = None


class TurnComplete(
    PersistedEvent, tag="turn_complete", tag_field="type", forbid_unknown_fields=True
):
    id: str
    turn: int
    scope: str | None
    stop_reason: str
    subagent_index: int | None = None


class TurnError(PersistedEvent, tag="turn_error", tag_field="type", forbid_unknown_fields=True):
    id: str
    turn: int
    scope: str | None
    message: str
    subagent_index: int | None = None


class TurnInterrupted(
    PersistedEvent, tag="turn_interrupted", tag_field="type", forbid_unknown_fields=True
):
    id: str
    turn: int
    scope: str | None
    subagent_index: int | None = None


class Handshake(Event, tag="handshake", tag_field="type", forbid_unknown_fields=True):
    id: str
    protocol_version: int
    session_id: str


class SessionStatus(Event, tag="session_status", tag_field="type", forbid_unknown_fields=True):
    id: str
    model_key: str
    git_branch: str


class ErrorNotice(Event, tag="error_notice", tag_field="type", forbid_unknown_fields=True):
    id: str
    kind: str
    message: str


type SessionEvent = (
    SessionStarted
    | UserMessage
    | IterationStart
    | TextDelta
    | LLMRequest
    | ToolCall
    | ToolResult
    | AssistantMessage
    | TurnComplete
    | TurnError
    | TurnInterrupted
    | Handshake
    | SessionStatus
    | ErrorNotice
)

# Transitional alias retained while consumers move to the final name.
CanonicalEvent = SessionEvent


def encode_event(event: Event) -> str:
    return msgspec.json.encode(event).decode("utf-8")


def decode_event(raw: str | bytes, *, persisted: bool = False) -> SessionEvent:
    event = msgspec.json.decode(raw, type=SessionEvent)
    if persisted and not event.persist:
        raise ValueError(f"event {type(event).__name__} is live-only")
    return event

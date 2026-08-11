"""Canonical session event schema for persisted and replayable events."""

from __future__ import annotations

from typing import Literal

import msgspec


class SessionStarted(
    msgspec.Struct, tag="session_started", tag_field="type", forbid_unknown_fields=True
):
    id: str
    schema_version: int
    sent_at: str
    model_key: str


class UserMessage(msgspec.Struct, tag="user_message", tag_field="type", forbid_unknown_fields=True):
    id: str
    turn: int
    scope: str | None
    content: str


class IterationStart(
    msgspec.Struct, tag="iteration_start", tag_field="type", forbid_unknown_fields=True
):
    id: str
    turn_iteration: str
    scope: str | None
    index: int


class TextDelta(msgspec.Struct, tag="text_delta", tag_field="type", forbid_unknown_fields=True):
    id: str
    turn_iteration: str
    scope: str | None
    request_id: str
    text: str


class LLMRequest(msgspec.Struct, tag="llm_request", tag_field="type", forbid_unknown_fields=True):
    id: str
    scope: str | None
    turn_iteration: str
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


class ToolCall(msgspec.Struct, tag="tool_call", tag_field="type", forbid_unknown_fields=True):
    id: str
    turn_iteration: str
    scope: str | None
    request_id: str
    tool_use_id: str
    name: str
    input: dict[str, object]


class ToolResult(msgspec.Struct, tag="tool_result", tag_field="type", forbid_unknown_fields=True):
    id: str
    turn_iteration: str
    scope: str | None
    request_id: str
    tool_use_id: str
    content: str
    is_error: bool
    duration_ms: int
    result_bytes: int


class AssistantMessage(
    msgspec.Struct, tag="assistant_message", tag_field="type", forbid_unknown_fields=True
):
    id: str
    turn: int
    scope: str | None
    request_ids: list[str]
    content: str
    interrupted: bool


class TurnComplete(
    msgspec.Struct, tag="turn_complete", tag_field="type", forbid_unknown_fields=True
):
    id: str
    turn: int
    scope: str | None
    stop_reason: str


class TurnError(msgspec.Struct, tag="turn_error", tag_field="type", forbid_unknown_fields=True):
    id: str
    turn: int
    scope: str | None
    message: str


class TurnInterrupted(
    msgspec.Struct, tag="turn_interrupted", tag_field="type", forbid_unknown_fields=True
):
    id: str
    turn: int
    scope: str | None


class ModelSwitch(msgspec.Struct, tag="model_switch", tag_field="type", forbid_unknown_fields=True):
    id: str
    model_key: str
    sent_at: str


class ShellCommand(
    msgspec.Struct, tag="shell_command", tag_field="type", forbid_unknown_fields=True
):
    id: str
    command: str
    exit_code: int
    output: str
    turn: int | None = None
    scope: str | None = None


CanonicalEvent = (
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
    | ModelSwitch
    | ShellCommand
)
PersistedEvent = (
    SessionStarted
    | UserMessage
    | IterationStart
    | LLMRequest
    | ToolCall
    | ToolResult
    | AssistantMessage
    | TurnComplete
    | TurnError
    | TurnInterrupted
    | ModelSwitch
    | ShellCommand
)


def encode_event(event: CanonicalEvent) -> str:
    return msgspec.json.encode(event).decode("utf-8")


def decode_event(raw: str | bytes, *, persisted: bool = False) -> CanonicalEvent:
    return msgspec.json.decode(raw, type=PersistedEvent if persisted else CanonicalEvent)

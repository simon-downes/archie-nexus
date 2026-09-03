"""Provider-neutral events yielded by the pure agent loop.

These dataclasses are the boundary between the pure loop and the stateful
harness. They carry per-request data and never contain wire or persistence
behavior.
"""

from dataclasses import dataclass


@dataclass
class IterationStart:
    """A new tool-loop iteration has begun."""

    index: int


@dataclass
class TextDelta:
    """A chunk of generated assistant text."""

    text: str
    request_id: str = ""


@dataclass
class Usage:
    """Per-request provider token usage."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class RequestContext:
    """Identity and send time for one provider request."""

    request_id: str
    sent_at: str


@dataclass
class RequestFinished:
    """Accounting result emitted after one provider request completes."""

    context: RequestContext
    duration_ms: int
    status: str
    usage: Usage | None
    stop_reason: str | None
    error: str | None


@dataclass
class TurnComplete:
    """The LLM has finished generating."""

    stop_reason: str


@dataclass
class TurnError:
    """An unrecoverable error occurred during the turn."""

    error: str


@dataclass
class TurnInterrupted:
    """The user cancelled the turn."""


@dataclass
class ToolCall:
    """The model requested a tool call."""

    tool_use_id: str
    name: str
    input: dict


@dataclass
class ToolResult:
    """Result from tool execution fed back to the model."""

    tool_use_id: str
    content: str
    is_error: bool = False
    duration_ms: int = 0
    result_lines: int = 0
    result_bytes: int = 0


type AgentEvent = (
    IterationStart
    | TextDelta
    | Usage
    | TurnComplete
    | TurnError
    | TurnInterrupted
    | ToolCall
    | ToolResult
    | RequestFinished
)

"""Agent-level events yielded by the pure run loop.

These are the boundary between the pure loop and the stateful harness.
They are provider-neutral and carry per-request data (not cumulative).

NOT the same as wire protocol events (archie_shared.events) — those are
for client communication. Agent events are for internal consumption by the
harness only.

Naming convention: agent and wire events share the same base name
(TextDelta, Usage, TurnComplete, etc.), distinguished by module.
"""

from dataclasses import dataclass


@dataclass
class IterationStart:
    """A new tool-loop iteration has begun.

    Emitted at the top of each loop iteration so the client can start a fresh
    visual block deterministically, decoupled from token-usage metadata.
    """

    index: int


@dataclass
class TextDelta:
    """A chunk of generated assistant text."""

    text: str


@dataclass
class Usage:
    """Per-request token usage (provider-neutral).

    The harness accumulates these into session totals.
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class TurnComplete:
    """The LLM has finished generating. Always the last event yielded."""

    stop_reason: str


@dataclass
class TurnError:
    """An unrecoverable error occurred during the turn."""

    error: str


@dataclass
class TurnInterrupted:
    """The user cancelled the turn (interrupt signal was set)."""


@dataclass
class ToolCall:
    """The model requested a tool call (post-accumulation, one per block)."""

    tool_use_id: str
    name: str
    input: dict


@dataclass
class ToolResult:
    """Result from tool execution fed back to the model."""

    tool_use_id: str
    content: str
    is_error: bool = False


type AgentEvent = (
    IterationStart
    | TextDelta
    | Usage
    | TurnComplete
    | TurnError
    | TurnInterrupted
    | ToolCall
    | ToolResult
)

"""Internal LLM stream event types.

Separated from __init__.py to avoid circular imports with bedrock.py.
These types are internal to the agent — NOT the same as wire protocol events.
"""

from dataclasses import dataclass


@dataclass
class TextDelta:
    """A chunk of generated text."""

    text: str


@dataclass(frozen=True)
class ToolUseStart:
    """A tool_use content block has started streaming. Name known, input not yet."""

    tool_use_id: str
    name: str


@dataclass
class ToolUseEvent:
    """A complete tool call parsed from the stream."""

    tool_use_id: str
    name: str
    input: dict
    input_truncated: bool = False


@dataclass
class Usage:
    """Token usage stats."""

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_write_input_tokens: int = 0


@dataclass
class Done:
    """Generation finished."""

    stop_reason: str


type StreamEvent = TextDelta | ToolUseStart | ToolUseEvent | Usage | Done

"""Internal LLM stream event types.

Separated from __init__.py to avoid circular imports with bedrock.py.
These types are internal to the agent — NOT the same as wire protocol events.
"""

from dataclasses import dataclass

from archie_shared.models import sanitize_billable_usage


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


@dataclass(init=False)
class Usage:
    """Token usage in billable categories.

    ``input_tokens`` is the uncached input portion. The old
    ``cache_*_input_tokens`` keyword/attribute names remain accepted as a
    compatibility alias for provider and test callers.
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __init__(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        *,
        cache_read_input_tokens: int | None = None,
        cache_write_input_tokens: int | None = None,
    ) -> None:
        if cache_read_input_tokens is not None:
            cache_read_tokens = cache_read_input_tokens
        if cache_write_input_tokens is not None:
            cache_write_tokens = cache_write_input_tokens
        (
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
        ) = sanitize_billable_usage(
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
        )

    @property
    def cache_read_input_tokens(self) -> int:
        """Compatibility alias for the old internal field name."""
        return self.cache_read_tokens

    @property
    def cache_write_input_tokens(self) -> int:
        """Compatibility alias for the old internal field name."""
        return self.cache_write_tokens


@dataclass
class Done:
    """Generation finished."""

    stop_reason: str


type StreamEvent = TextDelta | ToolUseStart | ToolUseEvent | Usage | Done

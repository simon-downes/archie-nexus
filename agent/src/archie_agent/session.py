"""Session state — in-memory conversation transcript and accumulators.

A Session represents one conversation. It tracks:
- The sequence of turns (for building LLM context)
- Cumulative token usage and cost
- Context window utilisation

Persistence is handled externally by the harness (not this module).

The turn_index is per-exchange: a user message and its assistant response share
the same index. It's incremented once per user message.
"""

from dataclasses import dataclass, field

from archie_shared.models import ModelEntry, calculate_cost
from archie_shared.types import ContentBlock, TextBlock


@dataclass
class Turn:
    """A single conversational turn (in-memory, for LLM context building).

    Attributes:
        role: "user" or "assistant" — maps to the LLM's message roles.
        content: List of content blocks.
        turn_index: The exchange index this turn belongs to.
        input_tokens: Tokens reported for this request's input.
        output_tokens: Tokens the model generated.
        interrupted: True if the user cancelled generation.
    """

    role: str
    content: list[ContentBlock]
    turn_index: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    interrupted: bool = False

    @property
    def text(self) -> str:
        """Extract the text content from this turn (first TextBlock, or empty)."""
        for block in self.content:
            if isinstance(block, TextBlock):
                return block.text
        return ""


@dataclass
class Session:
    """In-memory conversation state and token accounting.

    The harness owns persistence; Session is purely in-memory transcript
    plus cumulative accumulators for cost and context tracking.
    """

    model_id: str
    model: ModelEntry
    session_id: str = ""
    turns: list[Turn] = field(default_factory=list)
    turn_index: int = field(default=0)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0

    _last_input_tokens: int = field(default=0, repr=False)

    @property
    def total_cost(self) -> float:
        """Total USD spent in this session across all turns."""
        return calculate_cost(
            self.model.cost,
            self.total_input_tokens,
            self.total_output_tokens,
            self.total_cache_read_tokens,
            self.total_cache_write_tokens,
        )

    @property
    def context_pct(self) -> float:
        """Estimated context window usage for the NEXT request (0-100)."""
        estimated = self._last_input_tokens + (self.turns[-1].output_tokens if self.turns else 0)
        return (estimated / self.model.context) * 100

    @property
    def context_warning(self) -> bool:
        """True if we're approaching the model's context limit."""
        estimated = self._last_input_tokens + (self.turns[-1].output_tokens if self.turns else 0)
        return estimated > self.model.context * self.model.context_warning_threshold

    def next_turn_index(self) -> int:
        """Increment and return the next turn index."""
        self.turn_index += 1
        return self.turn_index

    def record_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        """Update all token accumulators and context tracking from a TurnUsage event.

        This is the single entry point for recording per-request token usage.
        Keeps context-window logic encapsulated (where context_pct lives).
        """
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cache_read_tokens += cache_read_tokens
        self.total_cache_write_tokens += cache_write_tokens
        self._last_input_tokens = input_tokens

    def add_turn(
        self,
        role: str,
        content: str | list[ContentBlock],
        turn_index: int = 0,
        output_tokens: int = 0,
        interrupted: bool = False,
    ) -> Turn:
        """Record a turn in memory (for LLM context building). Does NOT write to disk."""
        if isinstance(content, str):
            blocks: list[ContentBlock] = [TextBlock(text=content)]
        else:
            blocks = content

        turn = Turn(
            role=role,
            content=blocks,
            turn_index=turn_index,
            output_tokens=output_tokens,
            interrupted=interrupted,
        )
        self.turns.append(turn)
        return turn

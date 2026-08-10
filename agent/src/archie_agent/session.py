"""Session state — in-memory transcript and billable usage accumulators."""

from dataclasses import dataclass, field

from archie_shared.models import ModelEntry, calculate_cost
from archie_shared.types import ContentBlock, TextBlock


@dataclass
class Turn:
    """A single conversational turn used to build provider input."""

    role: str
    content: list[ContentBlock]
    turn_index: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    interrupted: bool = False

    @property
    def text(self) -> str:
        """Extract the first text block from this turn."""
        for block in self.content:
            if isinstance(block, TextBlock):
                return block.text
        return ""


@dataclass
class DisplayEntry:
    """A non-LLM event for UI display and history replay."""

    role: str
    content: str
    turn_index: int = 0


@dataclass
class Session:
    """In-memory transcript, cost totals, and context-window tracking."""

    model_id: str
    model: ModelEntry
    session_id: str = ""
    turns: list[Turn] = field(default_factory=list)
    display_entries: list[DisplayEntry] = field(default_factory=list)
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
        """Estimated next-request context usage, including cached input."""
        estimated = self._last_input_tokens + (
            self.turns[-1].output_tokens if self.turns else 0
        )
        return (estimated / self.model.context) * 100

    @property
    def context_warning(self) -> bool:
        """Whether the next request approaches the model context limit."""
        estimated = self._last_input_tokens + (
            self.turns[-1].output_tokens if self.turns else 0
        )
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
        """Accumulate billable usage and derived total context input."""
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cache_read_tokens += cache_read_tokens
        self.total_cache_write_tokens += cache_write_tokens
        self._last_input_tokens = input_tokens + cache_read_tokens + cache_write_tokens

    def add_turn(
        self,
        role: str,
        content: str | list[ContentBlock],
        turn_index: int = 0,
        output_tokens: int = 0,
        interrupted: bool = False,
    ) -> Turn:
        """Record a turn in memory without writing to disk."""
        blocks = [TextBlock(text=content)] if isinstance(content, str) else content
        turn = Turn(
            role=role,
            content=blocks,
            turn_index=turn_index,
            output_tokens=output_tokens,
            interrupted=interrupted,
        )
        self.turns.append(turn)
        return turn

"""Session state and persistence.

A Session represents one conversation. It tracks:
- The sequence of turns (for building LLM context)
- Cumulative token usage and cost
- Context window utilisation

Persistence: single JSONL file per session at /archie/sessions/{id}.jsonl
- One line per user exchange (prompt → response)
- Append-only — each turn is flushed when the agent loop completes it

The turn_index is per-exchange: a user message and its assistant response share
the same index. It's incremented once per user message.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from archie_shared.models import ModelEntry, calculate_cost
from archie_shared.types import ContentBlock, TextBlock
from ulid import ULID


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
class TurnLog:
    """Accumulated data for one user exchange, written to the JSONL log.

    Built up by the agent loop during run_turn, then passed to session.flush_turn().
    """

    when: str
    user: str
    assistant_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    model: str = ""
    interrupted: bool = False


@dataclass
class Session:
    """Manages conversation state and persistence.

    In-memory state (turns list) is used for building LLM context.
    Persistence (flush_turn) writes completed turns to a JSONL file.
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
    _log_dir: Path | None = field(default=None, repr=False)

    @property
    def log_path(self) -> Path | None:
        """Path to the JSONL log file, or None if no log dir configured."""
        if self._log_dir is None:
            return None
        return self._log_dir / f"{self.session_id}.jsonl"

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

    def add_turn(
        self,
        role: str,
        content: str | list[ContentBlock],
        turn_index: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
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
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            interrupted=interrupted,
        )
        self.turns.append(turn)
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cache_read_tokens += cache_read_tokens
        self.total_cache_write_tokens += cache_write_tokens

        if input_tokens > 0:
            self._last_input_tokens = input_tokens

        return turn

    def flush_turn(self, turn_log: TurnLog) -> None:
        """Write a completed turn to the JSONL log file. Append-only.

        Called by the agent loop at the end of each user exchange.
        Creates the sessions directory and file on first write.
        """
        log_path = self.log_path
        if log_path is None:
            return

        log_path.parent.mkdir(parents=True, exist_ok=True)

        cost = calculate_cost(
            self.model.cost,
            turn_log.input_tokens,
            turn_log.output_tokens,
            turn_log.cache_read_tokens,
            turn_log.cache_write_tokens,
        )

        entry = {
            "id": str(ULID()),
            "when": turn_log.when,
            "user": turn_log.user,
            "assistant": turn_log.assistant_text or None,
            "metadata": {
                "model": turn_log.model or self.model_id,
                "input_tokens": turn_log.input_tokens,
                "output_tokens": turn_log.output_tokens,
                "cache_read_tokens": turn_log.cache_read_tokens,
                "cache_write_tokens": turn_log.cache_write_tokens,
                "cost": round(cost, 6),
                "interrupted": turn_log.interrupted,
            },
        }

        if entry["assistant"] is None:
            del entry["assistant"]

        with log_path.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

"""Transcript compaction helpers and the one-shot compaction provider call."""

from __future__ import annotations

import html
import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from archie_shared.events import LLMRequest
from archie_shared.models import ModelEntry
from archie_shared.schemas import CompactionConfig
from archie_shared.types import ContentBlock, ImageBlock, TextBlock, ToolResultBlock, ToolUseBlock
from ulid import ULID

from archie_agent.compaction_prompts import CONTINUATION, FRAMING, INSTRUCTIONS, SYSTEM
from archie_agent.event_factory import EventFactory, now_utc
from archie_agent.loop import _RequestResult, stream_once
from archie_agent.prompt import SystemPrompt, flatten_system_prompt
from archie_agent.session import Turn

_TOOL_RESULT_LIMIT = 65_536
_SUMMARY_RESERVE = 16_384
_TRANSCRIPT_OPEN = "<transcript>\n"
_TRANSCRIPT_CLOSE = "\n</transcript>"
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranscriptStats:
    tool_results_kept: int = 0
    tool_results_removed: int = 0
    orphan_results: int = 0
    rounds_dropped: int = 0


@dataclass(frozen=True)
class CompactionOutcome:
    ok: bool
    summary: str
    request_event: LLMRequest | None
    duration_ms: int
    stats: TranscriptStats
    error: str | None = None
    interrupted: bool = False


def is_compact_command(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("/compact") and (len(stripped) == 8 or stripped[8].isspace())


def effective_limit(model: ModelEntry, cfg: CompactionConfig) -> int:
    limit = int(model.context * cfg.percent / 100)
    if cfg.tokens is not None:
        maximum = int(model.context * 0.95)
        if cfg.tokens > maximum:
            log.warning(
                "agent.compaction.tokens exceeds 95%% of model context; clamping to %d", maximum
            )
        limit = min(limit, min(cfg.tokens, maximum))
    return limit


def estimate_tokens(
    system: SystemPrompt | str | None, tool_config: list[dict] | None, turns: list[Turn]
) -> int:
    parts: list[bytes] = []
    if system is not None:
        parts.append(flatten_system_prompt(system).encode())
    if tool_config is not None:
        parts.append(json.dumps(tool_config, sort_keys=True).encode())
    for turn in turns:
        parts.append(turn.role.encode())
        for block in turn.content:
            if isinstance(block, TextBlock):
                parts.append(block.text.encode())
            elif isinstance(block, ToolUseBlock):
                parts.append(
                    json.dumps(
                        {"name": block.name, "id": block.tool_use_id, "input": block.input},
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode()
                )
            elif isinstance(block, ToolResultBlock):
                parts.append(block.content.encode())
                parts.append(_IMAGE_OMITTED.encode() if block.images else b"")
            elif isinstance(block, ImageBlock):
                parts.append(_IMAGE_OMITTED.encode())
    return math.ceil(sum(len(part) for part in parts) / 4)


_DELIMITER = re.compile(r"<(?=/?(?:transcript|message|tool_call|tool_result)\b)", re.IGNORECASE)
_IMAGE_OMITTED = "[image omitted during compaction]"
_RESULT_REMOVED = "[tool result removed to save space]"


def _escape(text: str) -> str:
    """Neutralise transcript delimiter tags inside untrusted content."""
    return _DELIMITER.sub("&lt;", text)


def _attr(value: object) -> str:
    return html.escape(str(value), quote=True)


def _tool_call_text(block: ToolUseBlock) -> str:
    payload = _escape(json.dumps(block.input, ensure_ascii=False, sort_keys=True))
    return f'<tool_call name="{_attr(block.name)}" id="{_attr(block.tool_use_id)}">{payload}</tool_call>'


def _tool_result_text(block: ToolResultBlock, content: str) -> str:
    return (
        f'<tool_result id="{_attr(block.tool_use_id)}" error="{str(block.is_error).lower()}">'
        f"{content}</tool_result>"
    )


def _transcript_lines(turns: list[Turn]) -> tuple[list[list[str]], TranscriptStats]:
    """Render each turn to transcript lines, applying the global result budget.

    Results are kept newest-first, so a result's fate depends only on newer
    results; dropping the oldest rounds never changes the remaining lines.
    """
    calls = {
        block.tool_use_id
        for turn in turns
        for block in turn.content
        if isinstance(block, ToolUseBlock)
    }
    results = [
        block for turn in turns for block in turn.content if isinstance(block, ToolResultBlock)
    ]
    kept: set[int] = set()
    used = 0
    removed = 0
    orphans = 0
    for index in range(len(results) - 1, -1, -1):
        result = results[index]
        if result.tool_use_id not in calls:
            orphans += 1
            continue
        size = len(result.content.encode("utf-8"))
        if used + size <= _TOOL_RESULT_LIMIT:
            kept.add(index)
            used += size
        else:
            removed += 1
    result_index = {id(result): index for index, result in enumerate(results)}
    per_turn: list[list[str]] = []
    for turn in turns:
        lines: list[str] = []
        for block in turn.content:
            if isinstance(block, TextBlock):
                lines.append(f'<message role="{_attr(turn.role)}">{_escape(block.text)}</message>')
            elif isinstance(block, ToolUseBlock):
                lines.append(_tool_call_text(block))
            elif isinstance(block, ToolResultBlock):
                if block.tool_use_id not in calls:
                    continue
                content = (
                    _escape(block.content) if result_index[id(block)] in kept else _RESULT_REMOVED
                )
                lines.append(_tool_result_text(block, content))
            elif isinstance(block, ImageBlock):
                lines.append(_IMAGE_OMITTED)
        per_turn.append(lines)
    return per_turn, TranscriptStats(
        tool_results_kept=len(kept), tool_results_removed=removed, orphan_results=orphans
    )


def build_transcript(turns: list[Turn]) -> tuple[str, TranscriptStats]:
    per_turn, stats = _transcript_lines(turns)
    return "\n".join(line for lines in per_turn for line in lines), stats


def _round_starts(turns: list[Turn]) -> list[int]:
    """Indices where a round (a user text message) begins.

    Tool-result turns carry no text, so a round always contains its calls and
    their results. Any leading turns before the first user text join round 0.
    """
    starts = [
        index
        for index, turn in enumerate(turns)
        if turn.role == "user" and any(isinstance(block, TextBlock) for block in turn.content)
    ]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    return starts


def fit_rounds(turns: list[Turn], budget_tokens: int) -> tuple[list[Turn], int]:
    """Drop the oldest whole rounds until the transcript fits ``budget_tokens``.

    Returns ``([], dropped)`` when even the newest round does not fit.
    """
    if not turns:
        return [], 0
    per_turn, _ = _transcript_lines(turns)
    starts = _round_starts(turns)
    bounds = list(zip(starts, [*starts[1:], len(turns)], strict=True))
    # Bytes per round, counting one "\n" separator per line.
    sizes = [
        sum(len(line.encode()) + 1 for lines in per_turn[start:end] for line in lines)
        for start, end in bounds
    ]
    remaining = sum(sizes)
    for dropped, (start, _end) in enumerate(bounds):
        if math.ceil(max(remaining - 1, 0) / 4) <= budget_tokens:
            return list(turns[start:]), dropped
        remaining -= sizes[dropped]
    return [], len(bounds) - 1


def build_replacement(
    summary: str,
    mode: Literal["start_of_turn", "mid_turn", "manual"],
    pending: list[ContentBlock],
) -> Turn:
    blocks: list[ContentBlock] = [
        TextBlock(FRAMING),
        TextBlock(f"<summary>\n{summary}\n</summary>"),
    ]
    if mode == "start_of_turn":
        blocks.extend(pending)
    elif mode == "mid_turn":
        blocks.append(TextBlock(CONTINUATION))
    return Turn(role="user", content=blocks, compacted=True)


class Compactor:
    def __init__(self, model_key: str, model_entry: ModelEntry, llm_client) -> None:
        self.model_key = model_key
        self.model_entry = model_entry
        self.llm_client = llm_client

    def transcript_budget(self) -> int:
        """Tokens available for the transcript after prompt overhead and output."""
        overhead = math.ceil(
            len((SYSTEM + INSTRUCTIONS + _TRANSCRIPT_OPEN + _TRANSCRIPT_CLOSE).encode()) / 4
        )
        return int(self.model_entry.context * 0.8) - _SUMMARY_RESERVE - overhead

    async def run(
        self,
        turns: list[Turn],
        *,
        reason: Literal["manual", "threshold", "cold_cache"],
        turn: int,
        iteration: int,
        interrupt: threading.Event,
        request_id: str | None = None,
    ) -> CompactionOutcome:
        started = time.monotonic()
        _, stats = build_transcript(turns)
        fitted, dropped = fit_rounds(turns, self.transcript_budget())
        if not fitted:
            return CompactionOutcome(
                False,
                "",
                None,
                0,
                TranscriptStats(**{**stats.__dict__, "rounds_dropped": dropped}),
                "Transcript is too large to compact",
            )
        transcript, stats = build_transcript(fitted)
        stats = TranscriptStats(**{**stats.__dict__, "rounds_dropped": dropped})
        request_id = request_id or str(ULID())
        sent_at = now_utc()
        result = _RequestResult()
        try:
            async for _event in stream_once(
                messages=[
                    Turn(
                        "user",
                        [
                            TextBlock(f"{_TRANSCRIPT_OPEN}{transcript}{_TRANSCRIPT_CLOSE}"),
                            TextBlock(INSTRUCTIONS),
                        ],
                    )
                ],
                system=SYSTEM,
                llm=self.llm_client,
                interrupt=interrupt,
                tool_config=None,
                history_boundary=None,
                result=result,
                request_id=request_id,
            ):
                continue
            summary = "".join(block.text for block in result.text_blocks).strip()
            usage = result.usage
            factory = EventFactory(Path("."), self.model_key, self.model_entry)
            request = factory.request(
                turn=turn,
                iteration=iteration,
                sent_at=sent_at,
                duration_ms=int((time.monotonic() - started) * 1000),
                status="interrupted"
                if result.interrupted
                else "error"
                if result.failed
                else "completed"
                if usage
                else "no_usage",
                usage=usage,
                stop_reason=result.stop_reason,
                error=result.error_msg,
                request_id=request_id,
                purpose="compaction",
            )
            if result.failed or result.interrupted:
                return CompactionOutcome(
                    False,
                    "",
                    request,
                    request.duration_ms,
                    stats,
                    result.error_msg or "Compaction interrupted",
                    interrupted=result.interrupted and not result.failed,
                )
            if not summary:
                return CompactionOutcome(
                    False,
                    "",
                    request,
                    request.duration_ms if request else 0,
                    stats,
                    "Compaction returned an empty summary",
                )
            return CompactionOutcome(
                True, summary, request, request.duration_ms if request else 0, stats
            )
        except Exception as exc:  # provider errors are reported by the harness
            request = EventFactory(Path("."), self.model_key, self.model_entry).request(
                turn=turn,
                iteration=iteration,
                sent_at=sent_at,
                duration_ms=int((time.monotonic() - started) * 1000),
                status="error",
                error=str(exc),
                request_id=request_id,
                purpose="compaction",
            )
            return CompactionOutcome(False, "", request, request.duration_ms, stats, str(exc))

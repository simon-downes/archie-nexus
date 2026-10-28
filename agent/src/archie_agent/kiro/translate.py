"""Translate Kiro ACP ``session/update`` notifications into canonical Archie events (077).

Pure: the caller supplies each update's receive time, and the translator returns the
events to emit in order. Segment rules follow D12, tool mapping D9/D10/D28, subagent
scope D11, images D26 and accounting D2.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from archie_shared.events import Event, LLMRequest
from archie_shared.types import ImageBlock
from ulid import ULID

from archie_agent.event_factory import EventFactory

log = logging.getLogger(__name__)

ARCHIE_TOOL_PREFIX = "archie::"
ARCHIE_TITLE_PREFIX = "@archie/"
SKILL_TOOL_ID = "disclose_context"
NO_COMPLETION_ERROR = "no turn_completion from Kiro"


def _kiro_meta(update: dict) -> dict:
    meta = update.get("_meta")
    if not isinstance(meta, dict):
        return {}
    kiro = meta.get("kiro")
    return kiro if isinstance(kiro, dict) else {}


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).isoformat()


@dataclass
class _Tool:
    """A tool call the translator has seen, with its mapping."""

    kind: str  # "archie" | "skill" | "task" | "other" | "internal"
    scope: str | None
    name: str = ""
    input: dict = field(default_factory=dict)
    started: float = 0.0
    iteration: int = 0
    request_id: str = ""
    done: bool = False


@dataclass
class _Scope:
    factory: EventFactory
    opened: bool = False
    iteration: int = -1
    request_id: str = ""
    text: str = ""  # current segment text not yet in an assistant_message
    flushed: bool = False  # an assistant_message was written in this iteration
    result_written: bool = False  # a tool_result was written in this iteration
    had_text: bool = False  # any text in this iteration
    boundary: bool = False  # the next text or tool call opens a new iteration
    ended: bool = False


class TurnTranslator:
    """Per-turn state machine from ACP updates to canonical events."""

    def __init__(self, log_path: Path, model_key: str, turn: int) -> None:
        self._log_path = log_path
        self._model_key = model_key
        self.turn = turn
        self._scopes: dict[str | None, _Scope] = {}
        self._tools: dict[str, _Tool] = {}
        self._subtasks: dict[str, str] = {}  # agentSubtaskId -> dispatch toolCallId
        self.completion: dict | None = None

    # -- scopes and segments ---------------------------------------------------

    def _scope(self, scope: str | None) -> _Scope:
        state = self._scopes.get(scope)
        if state is None:
            factory = EventFactory(
                self._log_path,
                self._model_key,
                None,
                scope=scope,
                subagent_index=None if scope is None else 0,
            )
            state = self._scopes[scope] = _Scope(factory)
        return state

    def _scope_of(self, update: dict) -> str | None:
        subtask = _kiro_meta(update).get("agentSubtaskId")
        if not subtask:
            return None
        scope = self._subtasks.get(subtask)
        if scope is None:
            log.warning("Kiro update for unknown subagent %s; logging it at root", subtask)
        return scope

    def _flush(self, state: _Scope, out: list[Event], *, interrupted: bool = False) -> None:
        if not state.text:
            return
        out.append(
            state.factory.assistant_message(
                turn=self.turn,
                iteration=state.iteration,
                request_id=state.request_id,
                content=state.text,
                interrupted=interrupted,
            )
        )
        state.text = ""
        state.flushed = True

    def _open(self, state: _Scope, out: list[Event]) -> None:
        """Open iteration 0, or the next iteration once a boundary was reached."""
        if state.opened and not state.boundary:
            return
        self._flush(state, out)
        state.opened = True
        state.iteration += 1
        state.request_id = str(ULID())
        state.flushed = state.result_written = state.had_text = state.boundary = False
        out.append(state.factory.iteration_start(turn=self.turn, iteration=state.iteration))

    def _text(self, scope: str | None, text: str, out: list[Event]) -> None:
        state = self._scope(scope)
        if state.flushed or state.result_written:
            # Text after this iteration's assistant_message or a tool result needs a
            # fresh request key, or the TUI drops its deltas (D12).
            state.boundary = True
        self._open(state, out)
        state.text += text
        state.had_text = True
        out.append(
            state.factory.text_delta(
                turn=self.turn, iteration=state.iteration, request_id=state.request_id, text=text
            )
        )

    # -- feed ------------------------------------------------------------------

    def feed(self, update: dict, at: float) -> list[Event]:
        """Translate one ``session/update`` ``update`` object received at ``at``."""
        out: list[Event] = []
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            text = content.get("text") if isinstance(content, dict) else None
            if text:
                self._text(self._scope_of(update), text, out)
        elif kind in ("tool_call", "tool_call_update"):
            self._tool_update(update, at, out)
        elif kind == "session_info_update":
            meta = _kiro_meta(update)
            if meta.get("kind") == "turn_completion" and not meta.get("agentSubtaskId"):
                self.completion = meta
        return out

    def _classify(self, update: dict, at: float) -> _Tool:
        meta = _kiro_meta(update)
        title = update.get("title") or ""
        raw = update.get("rawInput") if isinstance(update.get("rawInput"), dict) else {}
        tool_id = raw.get("tool_id") if isinstance(raw.get("tool_id"), str) else ""
        if meta.get("kind") == "agent-subtask":
            # archie-worker has no subagent tool, so dispatches always come from the root.
            subtask = meta.get("agentSubtaskId")
            if subtask:
                self._subtasks[subtask] = update["toolCallId"]
            task = {"agent": raw.get("name", ""), "prompt": raw.get("prompt", "")}
            return _Tool("task", None, "task", {"tasks": [task]}, at)
        scope = self._scope_of(update)
        if title in ("Tool Load", "Subagent Response") or "tool_ids" in raw:
            return _Tool("internal", scope)
        if meta.get("toolId") == SKILL_TOOL_ID:
            return _Tool("skill", scope, "skill", {"name": raw.get("name", "")}, at)
        if tool_id.startswith(ARCHIE_TOOL_PREFIX) or title.startswith(ARCHIE_TITLE_PREFIX):
            name = tool_id.removeprefix(ARCHIE_TOOL_PREFIX) or title.removeprefix(
                ARCHIE_TITLE_PREFIX
            )
            args = raw.get("arguments")
            return _Tool("archie", scope, name, args if isinstance(args, dict) else {}, at)
        name = meta.get("toolId") or title or "unknown"
        log.warning("Unmapped Kiro tool call %r (%s); logging it as is", name, update["toolCallId"])
        return _Tool("other", scope, str(name), dict(raw), at)

    def _tool_update(self, update: dict, at: float, out: list[Event]) -> None:
        call_id = update.get("toolCallId")
        if not call_id:
            return
        tool = self._tools.get(call_id)
        if tool is None:
            tool = self._tools[call_id] = self._classify(update, at)
            if tool.kind == "internal":
                state = self._scope(tool.scope)
                if state.had_text:
                    state.boundary = True
            else:
                self._start_tool(call_id, tool, out)
        if tool.kind == "internal" or tool.done:
            return
        status = update.get("status")
        if status in ("completed", "failed"):
            self._end_tool(call_id, tool, update, at, out)

    def _start_tool(self, call_id: str, tool: _Tool, out: list[Event]) -> None:
        state = self._scope(tool.scope)
        if state.result_written:
            state.boundary = True
        self._open(state, out)
        self._flush(state, out)
        tool.iteration = state.iteration
        tool.request_id = state.request_id
        out.append(
            state.factory.tool_call(
                turn=self.turn,
                iteration=tool.iteration,
                request_id=tool.request_id,
                tool_use_id=call_id,
                name=tool.name,
                input=tool.input,
            )
        )

    def _end_tool(
        self, call_id: str, tool: _Tool, update: dict, at: float, out: list[Event]
    ) -> None:
        failed = update.get("status") == "failed"
        raw_output = update.get("rawOutput")
        content = _result_text(update)
        images: tuple[ImageBlock, ...] = ()
        if isinstance(raw_output, dict):
            images = _images(raw_output.get("imageBase64Urls"))
        if tool.kind == "task":
            child = self._scopes.get(call_id)
            if child is not None and not child.ended:
                self._end_scope(child, out, "cancelled" if failed else "end_turn", content)
        self._write_result(call_id, tool, content, failed, at, out, images)

    def _write_result(
        self,
        call_id: str,
        tool: _Tool,
        content: str,
        is_error: bool,
        at: float,
        out: list[Event],
        images: tuple[ImageBlock, ...] = (),
    ) -> None:
        tool.done = True
        state = self._scope(tool.scope)
        out.append(
            state.factory.tool_result(
                turn=self.turn,
                iteration=tool.iteration,
                request_id=tool.request_id,
                tool_use_id=call_id,
                content=content,
                is_error=is_error,
                duration_ms=max(0, round((at - tool.started) * 1000)),
                result_bytes=len(content.encode("utf-8")),
                result_lines=len(content.splitlines()),
                images=images,
            )
        )
        if state.iteration == tool.iteration:
            state.result_written = True

    def _end_scope(
        self,
        state: _Scope,
        out: list[Event],
        stop_reason: str | None,
        error: str | None = None,
    ) -> None:
        """Flush a scope's text and write its terminal event (children only)."""
        state.ended = True
        self._flush(state, out, interrupted=stop_reason == "cancelled")
        if state.factory.scope is None:
            return
        if stop_reason == "cancelled":
            out.append(state.factory.turn_interrupted(turn=self.turn))
        elif stop_reason is None:
            out.append(state.factory.turn_error(turn=self.turn, message=error or "subagent failed"))
        else:
            out.append(state.factory.turn_complete(turn=self.turn, stop_reason=stop_reason))

    # -- finish ----------------------------------------------------------------

    def finish(
        self,
        *,
        stop_reason: str | None,
        error: str | None,
        sent_at: float,
        at: float,
        price: Callable[[float], float],
    ) -> list[Event]:
        """Close the turn: open tools and scopes, the ``llm_request``, the root terminal.

        Exactly one of ``stop_reason`` (the ``session/prompt`` result) and ``error``
        (a JSON-RPC error or Kiro exit) is set.
        """
        out: list[Event] = []
        cancelled = stop_reason == "cancelled"
        for call_id, tool in self._tools.items():
            if tool.kind != "internal" and not tool.done:
                reason = (
                    "Tool call was cancelled" if cancelled else "Tool call ended without a result"
                )
                if tool.kind == "task":
                    child = self._scopes.get(call_id)
                    if child is not None and not child.ended:
                        self._end_scope(child, out, "cancelled" if cancelled else None, error)
                self._write_result(call_id, tool, reason, True, at, out)
        for scope, state in self._scopes.items():
            if scope is not None and not state.ended:
                self._end_scope(state, out, "cancelled" if cancelled else None, error)
        root = self._scope(None)
        self._end_scope(root, out, stop_reason, error)
        out.append(self._llm_request(stop_reason, error, sent_at, at, price))
        if cancelled:
            out.append(root.factory.turn_interrupted(turn=self.turn))
        elif stop_reason is None:
            out.append(root.factory.turn_error(turn=self.turn, message=error or "Kiro turn failed"))
        else:
            out.append(root.factory.turn_complete(turn=self.turn, stop_reason=stop_reason))
        return out

    def credits(self) -> float:
        """Credits Kiro reported for this turn (0 without a ``turn_completion``)."""
        if self.completion is None:
            return 0.0
        total = 0.0
        for summary in self.completion.get("promptTurnSummaries") or ():
            if isinstance(summary, dict) and summary.get("unit") == "credit":
                try:
                    total += float(summary.get("usage") or 0)
                except (TypeError, ValueError):
                    continue
        return total

    def _llm_request(
        self,
        stop_reason: str | None,
        error: str | None,
        sent_at: float,
        at: float,
        price: Callable[[float], float],
    ) -> LLMRequest:
        completion = self.completion
        wall_ms = max(0, round((at - sent_at) * 1000))
        if completion is None:
            credits, cost, status, duration = 0.0, 0.0, "no_usage", wall_ms
            error = error or NO_COMPLETION_ERROR
        else:
            credits = self.credits()
            cost = price(credits)
            kiro_status = completion.get("status")
            status = {"success": "completed", "aborted": "interrupted"}.get(kiro_status, "error")
            elapsed = completion.get("elapsedTime")
            duration = int(elapsed) if isinstance(elapsed, (int, float)) else wall_ms
            if status == "error" and error is None:
                error = f"Kiro turn status: {kiro_status}"
        return LLMRequest(
            id=str(ULID()),
            scope=None,
            turn=self.turn,
            iteration=0,
            model_key=self._model_key,
            sent_at=_iso(sent_at),
            duration_ms=max(0, duration),
            status=status,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            context_tokens=0,
            cost_usd=round(cost, 6),
            stop_reason=stop_reason,
            error=error,
            credits=round(credits, 6),
        )


def _result_text(update: dict) -> str:
    raw = update.get("rawOutput")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        for key in ("response", "message"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                return value
    parts: list[str] = []
    for block in update.get("content") or ():
        inner = block.get("content") if isinstance(block, dict) else None
        if isinstance(inner, dict) and inner.get("type") == "text":
            parts.append(str(inner.get("text", "")))
    return "".join(parts)


def _images(urls: Any) -> tuple[ImageBlock, ...]:
    """Image metadata from ``data:`` URLs (D26); undecodable entries are skipped."""
    if not isinstance(urls, list):
        return ()
    from PIL import Image

    images: list[ImageBlock] = []
    for url in urls:
        if not isinstance(url, str) or not url.startswith("data:") or "," not in url:
            continue
        header, payload = url[5:].split(",", 1)
        mime = header.split(";", 1)[0]
        fmt = mime.removeprefix("image/") or "png"
        try:
            data = base64.b64decode(payload, validate=False)
            with Image.open(io.BytesIO(data)) as img:
                width, height = img.size
        except (binascii.Error, ValueError, OSError):
            log.warning("Cannot decode an image in a Kiro tool result")
            continue
        images.append(ImageBlock(format=fmt, data=data, width=width, height=height))
    return tuple(images)

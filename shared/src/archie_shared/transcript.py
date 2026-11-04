"""Client-neutral transcript model reduced from canonical session events.

``TranscriptBuilder`` turns persisted events (plus live-only ``text_delta``) into a
tree of display nodes. Both the TUI and the orchestrator's web viewer use it, so the
event rules (request reconciliation, iteration grouping, subagent nesting) live in one
place. Nodes hold data only; each client renders them its own way.

:meth:`TranscriptBuilder.apply` returns the :class:`Change` records one event caused,
so a client can update an existing view instead of rebuilding it.

Tree shape::

    preamble items / TurnNode.items
        TextNode | StreamNode | IterationNode | CompactionNode | NoticeNode | SubagentNode
    IterationNode.tools
        ToolNode
    ToolNode.children
        SubagentNode
    SubagentNode.items
        (same kinds as a turn's items)

Child events (non-null ``scope`` and ``subagent_index``) belong to the ``task`` tool
call whose ``tool_use_id`` is their scope. Tool calls are grouped by
``(scope, subagent_index, turn, iteration)``: one group per provider response.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from archie_shared.events import (
    AssistantMessage,
    CompactionComplete,
    CompactionStart,
    ContextCompacted,
    IterationStart,
    LLMRequest,
    TextDelta,
    ToolCall,
    ToolResult,
    TurnComplete,
    TurnError,
    TurnInterrupted,
    UserMessage,
)
from archie_shared.session.accounting import UsageTotals
from archie_shared.tool_summaries import format_tool_activity, format_tool_complete

PREVIEW_CHARS = 8000
"""Characters of one input or result section that a detail view shows."""

ROW_PREVIEW_LINES = 10
"""Lines of exec source or shell command that a transcript row shows."""

# Provider stop reason when a safety filter blocks the response (Bedrock Converse).
CONTENT_FILTERED = "content_filtered"

CONTENT_FILTERED_MESSAGE = (
    "The model provider's content filter blocked this response. Something in the "
    "conversation, such as your message, a tool result or a file, triggered it.\n"
    "That content is still in the session, so later requests will be blocked too.\n"
    "To continue:\n"
    "1. Run /compact to replace the conversation history with a summary.\n"
    "2. Rephrase your request to avoid the content that triggered the filter."
)

CHILD_CONTENT_FILTERED_MESSAGE = "Blocked by the model provider's content filter"

_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def dom_id(*parts: object) -> str:
    """Join parts into a stable id using only ``[A-Za-z0-9_-]``."""
    return "-".join(_ID_UNSAFE.sub("_", str(p)) for p in parts if p is not None)


def preview(text: str, limit: int = PREVIEW_CHARS) -> tuple[str, int]:
    """Return (text capped at ``limit`` characters, characters omitted)."""
    if len(text) <= limit:
        return text, 0
    return text[:limit], len(text) - limit


def source_preview(text: str, limit: int = ROW_PREVIEW_LINES) -> tuple[list[str], int]:
    """Return (first ``limit`` lines, lines omitted) of multi-line source text."""
    lines = text.rstrip("\n").split("\n") if text else []
    return lines[:limit], max(0, len(lines) - limit)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


@dataclass
class TextNode:
    id: str
    role: Literal["user", "assistant"]
    content: str
    interrupted: bool = False
    kind: str = "text"


@dataclass
class StreamNode:
    id: str
    text: str = ""
    kind: str = "stream"


@dataclass
class NoticeNode:
    id: str
    variant: Literal["turn-error", "turn-interrupted"]
    title: str
    detail: str = ""
    kind: str = "notice"


@dataclass
class CompactionNode:
    id: str
    request_id: str
    reason: str
    status: Literal["pending", "success", "failure", "interrupted"]
    tokens_before: int
    tokens_after: int | None = None
    duration_ms: int = 0
    error: str | None = None
    estimated_next_request_cost_usd: float | None = None
    cost_saved_usd: float = 0.0
    summary: str = ""
    marker: bool = False  # legacy ``context_compacted`` record
    kind: str = "compaction"


@dataclass
class SubagentNode:
    id: str
    scope: str
    index: int
    agent: str = "child"
    status: Literal["running", "complete", "error", "interrupted"] = "running"
    cost_usd: float = 0.0
    context_tokens: int = 0
    error: str = ""
    activity: str = "Thinking..."
    items: list = field(default_factory=list)
    kind: str = "subagent"

    @property
    def items_id(self) -> str:
        return f"{self.id}-items"

    @property
    def row_id(self) -> str:
        return f"{self.id}-row"

    @property
    def label(self) -> str:
        return f"{self.agent} #{self.index}"


@dataclass
class ToolNode:
    id: str
    tool_use_id: str
    name: str
    input: dict
    status: Literal["pending", "ok", "error"] = "pending"
    result: str | None = None
    duration_ms: int = 0
    result_lines: int = 0
    result_bytes: int = 0
    children: list[SubagentNode] = field(default_factory=list)
    kind: str = "tool"

    @property
    def children_id(self) -> str:
        return f"{self.id}-children"

    @property
    def head_id(self) -> str:
        return f"{self.id}-head"


@dataclass
class IterationNode:
    id: str
    scope: str | None
    subagent_index: int | None
    turn: int
    iteration: int
    tools: list[ToolNode] = field(default_factory=list)
    kind: str = "iteration"

    @property
    def tools_id(self) -> str:
        return f"{self.id}-tools"


@dataclass
class TurnNode:
    id: str
    number: int
    user: TextNode | None
    items: list = field(default_factory=list)
    usage: UsageTotals = field(default_factory=UsageTotals)  # root-scope requests only
    duration_ms: int = 0  # root-scope request durations
    stop_reason: str | None = None
    kind: str = "turn"

    @property
    def items_id(self) -> str:
        return f"{self.id}-items"

    @property
    def footer_id(self) -> str:
        return f"{self.id}-footer"


@dataclass
class Change:
    """One view update caused by an event.

    - ``append``: add ``node`` at the end of slot ``parent_id`` (``None`` = root).
    - ``replace``: ``node`` changed; ``target_id`` names what to redraw (the node's own
      id, or a slot such as a turn footer or a stream the node replaces).
    - ``append_text``: append ``text`` to stream ``node`` (element ``target_id``).
    - ``remove``: delete ``target_id`` if present.
    """

    kind: Literal["append", "replace", "append_text", "remove"]
    node: object | None = None
    parent_id: str | None = None
    target_id: str | None = None
    text: str = ""


# ---------------------------------------------------------------------------
# Tool presentation shared by every client
# ---------------------------------------------------------------------------


def _esc(text: str) -> str:
    return text.replace("[", r"\[").replace("]", r"\]")


def _first_line(text: str) -> str:
    return text.split("\n", 1)[0] if text else ""


def _exec_metrics(tool: ToolNode) -> str:
    parts = []
    if tool.duration_ms:
        ms = tool.duration_ms
        parts.append(f"{ms}ms" if ms < 1000 else f"{ms / 1000:.1f}s")
    if tool.result_lines:
        parts.append(f"{tool.result_lines} lines")
    if tool.result_bytes:
        parts.append(f"{tool.result_bytes:,} bytes")
    return " · ".join(parts) if parts else "done"


def _completed_summary(tool: ToolNode) -> str:
    assert tool.result is not None
    tool_input = tool.input
    if tool.name == "shell" and isinstance(tool_input.get("command"), str):
        # The row previews the command's lines; the headline names only the first.
        tool_input = {**tool_input, "command": _first_line(tool_input["command"])}
    return format_tool_complete(
        tool.name, tool_input, tool.result, tool.status == "error", tool.duration_ms
    )


def tool_headline(tool: ToolNode) -> str:
    """One-line Rich-markup summary of a tool row (no status glyph)."""
    if tool.name == "exec":
        if tool.result is None:
            return "[bold]Exec[/]"
        if tool.status == "error":
            message = _first_line(tool.result)[:120] or "error"
            return f"[bold]Exec[/] [dim red]{_esc(message)}[/]"
        return f"[bold]Exec[/] [dim]{_exec_metrics(tool)}[/]"
    if tool.result is None:
        tool_input = tool.input
        if tool.name == "shell" and isinstance(tool_input.get("command"), str):
            tool_input = {**tool_input, "command": _first_line(tool_input["command"])}
        return format_tool_activity(tool.name, tool_input)
    return _first_line(_completed_summary(tool))


def tool_extra(tool: ToolNode) -> str:
    """Rich-markup lines shown under the headline: a diff or condensed error output."""
    if tool.result is None or tool.name == "exec":
        return ""
    parts = _completed_summary(tool).split("\n", 1)
    return parts[1] if len(parts) > 1 else ""


def tool_preview(tool: ToolNode) -> tuple[list[str], int]:
    """Plain source lines previewed in the row (exec source, shell command), and how
    many more lines the detail view holds.

    The shell headline already shows the command's first line, so its preview starts
    at the second line and counts the first towards the limit.
    """
    if tool.name == "exec":
        source = tool.input.get("source")
        return source_preview(source) if isinstance(source, str) else ([], 0)
    if tool.name == "shell":
        command = tool.input.get("command")
        if not isinstance(command, str):
            return [], 0
        lines, omitted = source_preview(command)
        return lines[1:], omitted
    return [], 0


@dataclass
class DetailSection:
    """One block of a tool detail view."""

    title: str
    text: str
    language: str | None = None  # lexer name for highlighting
    filename: str | None = None  # lexer hint when no language is given
    omitted: int = 0  # characters cut by :data:`PREVIEW_CHARS`


def _section(title: str, text: str, language=None, filename=None) -> DetailSection:
    shown, omitted = preview(text)
    return DetailSection(title, shown, language, filename, omitted)


def _json_text(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _looks_like_json(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return False
    try:
        json.loads(stripped)
    except ValueError:
        return False
    return True


def tool_detail_sections(tool: ToolNode) -> list[DetailSection]:
    """Input and result blocks for a tool detail view, with highlighting hints."""
    tool_input = tool.input
    path = tool_input.get("path") if isinstance(tool_input.get("path"), str) else None
    sections: list[DetailSection] = []

    def rest(*skip: str) -> dict:
        return {k: v for k, v in tool_input.items() if k not in skip}

    if tool.name == "exec" and isinstance(tool_input.get("source"), str):
        sections.append(_section("Source", tool_input["source"], "python"))
    elif tool.name == "shell" and isinstance(tool_input.get("command"), str):
        sections.append(_section("Command", tool_input["command"], "bash"))
        if rest("command"):
            sections.append(_section("Options", _json_text(rest("command")), "json"))
    elif tool.name == "write" and isinstance(tool_input.get("content"), str):
        sections.append(_section("Content", tool_input["content"], filename=path))
    elif tool.name == "edit" and isinstance(tool_input.get("old"), str):
        sections.append(_section("Old", tool_input["old"], filename=path))
        sections.append(_section("New", str(tool_input.get("new", "")), filename=path))
        if rest("path", "old", "new"):
            sections.append(_section("Options", _json_text(rest("path", "old", "new")), "json"))
    else:
        sections.append(_section("Input", _json_text(tool_input), "json"))

    if tool.result is not None:
        title = "Error" if tool.status == "error" else "Result"
        result = tool.result
        if tool.name == "edit" and tool.status != "error":
            sections.append(_section(title, result, "diff"))
        elif _looks_like_json(result):
            sections.append(_section(title, result, "json"))
        else:
            sections.append(_section(title, result))
    return sections


def subagent_status_text(sub: SubagentNode) -> str:
    """Short status for a subagent row: the error, "Completed", or current activity."""
    if sub.status == "error":
        return sub.error or "error"
    if sub.status == "complete":
        return "Completed"
    if sub.status == "interrupted":
        return "Interrupted"
    return sub.activity


def _one_line(text: str, limit: int = 512) -> str:
    return "; ".join(part.strip() for part in text.splitlines() if part.strip())[:limit]


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _compaction_label(reason: str) -> str:
    return {"threshold": "threshold", "cold_cache": "cold cache", "manual": "manual"}.get(
        reason, reason
    )


class TranscriptBuilder:
    """Reduce canonical events to transcript nodes, reporting view changes per event.

    Indexes, for clients that update views incrementally:

    - ``nodes``: node id → node, for every node with an id.
    - ``container_of``: node id → slot id holding it (``None`` = transcript root).
    - ``owner_of``: slot id → id of the node that owns the slot.
    - ``tools``: tool_use_id → ``ToolNode``; ``subagents``: (scope, index) → ``SubagentNode``.
    """

    def __init__(self) -> None:
        self.turns: list[TurnNode] = []
        self.preamble: list = []  # items before the first user message
        self.usage = UsageTotals()
        self.last_persisted_id: str | None = None
        self.nodes: dict[str, object] = {}
        self.container_of: dict[str, str | None] = {}
        self.owner_of: dict[str, str] = {}
        self.tools: dict[str, ToolNode] = {}
        self.subagents: dict[tuple[str, int], SubagentNode] = {}
        self._iterations: dict[tuple, IterationNode] = {}
        self._compactions: dict[tuple, CompactionNode] = {}
        self._streams: dict[tuple, StreamNode] = {}
        self._finalized: set[tuple] = set()
        self._seen: set[str] = set()
        self._fresh: set[str] = set()  # slots created by the event being applied
        self._created: set[str] = set()  # node ids created by the event being applied

    # -- lookups ---------------------------------------------------------------

    def current_turn(self) -> TurnNode | None:
        return self.turns[-1] if self.turns else None

    def ancestors(self, node_id: str):
        """Yield the ids of the nodes enclosing ``node_id``, innermost first."""
        slot = self.container_of.get(node_id)
        while slot is not None:
            owner = self.owner_of.get(slot)
            if owner is None:
                return
            yield owner
            slot = self.container_of.get(owner)

    # -- registration ----------------------------------------------------------

    def _register(self, node, slot: str | None, *slots: str) -> None:
        self.nodes[node.id] = node
        self._created.add(node.id)
        self.container_of[node.id] = slot
        for owned in slots:
            self.owner_of[owned] = node.id

    def _slots_of(self, node) -> tuple[str, ...]:
        if isinstance(node, TurnNode):
            return (node.items_id,)
        if isinstance(node, IterationNode):
            return (node.tools_id,)
        if isinstance(node, ToolNode):
            return (node.children_id,)
        if isinstance(node, SubagentNode):
            return (node.items_id,)
        return ()

    def _root_container(self) -> tuple[list, str | None]:
        turn = self.current_turn()
        if turn is None:
            return self.preamble, None
        return turn.items, turn.items_id

    def _subagent(self, scope: str, index: int, changes: list[Change]) -> SubagentNode:
        """Return the child group for (scope, index), creating it on first use.

        A new group is sent as one ``append`` rendered after ``apply`` returns, so it
        already holds the items this event adds. It is preceded by a ``remove`` of
        its id, which clears a group left in a page by an earlier live connection.
        """
        key = (scope, index)
        node = self.subagents.get(key)
        if node is not None:
            return node
        node = SubagentNode(id=dom_id("sub", scope, index), scope=scope, index=index)
        parent = self.tools.get(scope)
        if parent is not None:
            tasks = parent.input.get("tasks")
            if isinstance(tasks, list) and index < len(tasks) and isinstance(tasks[index], dict):
                node.agent = str(tasks[index].get("agent") or "child")
            parent.children.append(node)
            parent_slot: str | None = parent.children_id
        else:
            items, parent_slot = self._root_container()
            items.append(node)
        self.subagents[key] = node
        self._register(node, parent_slot, node.items_id)
        changes.append(Change("remove", target_id=node.id))
        self._emit_append(node, parent_slot, changes)
        return node

    def _emit_append(self, node, slot: str | None, changes: list[Change]) -> None:
        if slot is None or slot not in self._fresh:
            changes.append(Change("append", node, parent_id=slot))
        self._fresh.update(self._slots_of(node))

    def _container(self, event, changes: list[Change]) -> tuple[list, str | None]:
        """Return (items, slot id) for the event's scope."""
        scope = getattr(event, "scope", None)
        index = getattr(event, "subagent_index", None)
        if scope is not None and index is not None:
            node = self._subagent(scope, index, changes)
            return node.items, node.items_id
        return self._root_container()

    def _add(self, event, node, changes: list[Change]) -> None:
        items, slot = self._container(event, changes)
        items.append(node)
        self._register(node, slot, *self._slots_of(node))
        self._emit_append(node, slot, changes)

    def _iteration(self, event, changes: list[Change]) -> IterationNode:
        key = (event.scope, event.subagent_index, event.turn, event.iteration)
        node = self._iterations.get(key)
        if node is None:
            node = IterationNode(
                id=dom_id("iter", *key),
                scope=event.scope,
                subagent_index=event.subagent_index,
                turn=event.turn,
                iteration=event.iteration,
            )
            self._iterations[key] = node
            self._add(event, node, changes)
        return node

    def _add_tool(self, event, tool: ToolNode, changes: list[Change]) -> None:
        group = self._iteration(event, changes)
        group.tools.append(tool)
        self.tools[tool.tool_use_id] = tool
        self._register(tool, group.tools_id, tool.children_id)
        self._emit_append(tool, group.tools_id, changes)

    def _changed(self, node, changes: list[Change], target_id: str | None = None) -> None:
        if node.id in self._created:
            return  # its append, rendered after apply returns, already shows this state
        changes.append(Change("replace", node, target_id=target_id or node.id))

    @staticmethod
    def _request_key(event) -> tuple:
        return (event.scope, event.subagent_index, event.turn, event.iteration, event.request_id)

    def _stream_id(self, key: tuple) -> str:
        scope, index, turn, iteration, request_id = key
        return dom_id("stream", scope, index, turn, iteration, request_id)

    def _child_of(self, event) -> SubagentNode | None:
        scope = getattr(event, "scope", None)
        index = getattr(event, "subagent_index", None)
        if scope is None or index is None:
            return None
        return self.subagents.get((scope, index))

    def _set_activity(self, sub: SubagentNode | None, activity: str, changes) -> None:
        if sub is None:
            return
        sub.activity = _one_line(activity) or "Thinking..."
        self._changed(sub, changes, sub.row_id)

    def _close_streams(self, scope, index, changes: list[Change]) -> None:
        """Turn unfinished streams of an ended turn into plain assistant messages."""
        for key in [k for k in self._streams if k[0] == scope and k[1] == index]:
            stream = self._streams.pop(key)
            self._finalized.add(key)
            slot = self.container_of.get(stream.id)
            owner = self.nodes.get(self.owner_of.get(slot, "")) if slot else None
            items = owner.items if owner is not None else self.preamble
            position = next((i for i, item in enumerate(items) if item is stream), None)
            if position is None:
                continue
            if stream.text.strip():
                node = TextNode(dom_id("msg", stream.id), "assistant", stream.text.rstrip("\n"))
                items[position] = node
                self._register(node, slot)
                changes.append(Change("replace", node, target_id=stream.id))
            else:
                items.pop(position)
                changes.append(Change("remove", target_id=stream.id))

    # -- events ------------------------------------------------------------------

    def apply(self, event) -> list[Change]:
        """Apply one event and return the view changes it caused."""
        event_id = getattr(event, "id", None)
        if event_id is not None:
            if event_id in self._seen:
                return []
            self._seen.add(event_id)
            if event.persist:
                self.last_persisted_id = event_id
        changes: list[Change] = []
        self._fresh.clear()
        self._created.clear()
        scope = getattr(event, "scope", None)
        index = getattr(event, "subagent_index", None)
        child = scope is not None and index is not None

        if isinstance(event, UserMessage):
            if child:
                if event.content:
                    self._add(
                        event, TextNode(dom_id("msg", event.id), "user", event.content), changes
                    )
            else:
                user = TextNode(dom_id("msg", event.id), "user", event.content)
                turn = TurnNode(id=dom_id("turn", event.id), number=event.turn, user=user)
                self.turns.append(turn)
                self._register(turn, None, turn.items_id)
                self.nodes[user.id] = user
                self._emit_append(turn, None, changes)
        elif isinstance(event, IterationStart):
            if child:
                sub = self._subagent(scope, index, changes)
                self._set_activity(sub, "Thinking...", changes)
        elif isinstance(event, TextDelta):
            key = self._request_key(event)
            if key in self._finalized:
                return changes
            stream = self._streams.get(key)
            if stream is None:
                stream = StreamNode(id=self._stream_id(key), text=event.text)
                self._streams[key] = stream
                # A reconnect starts a new builder mid-stream; drop the old partial block.
                changes.append(Change("remove", target_id=stream.id))
                self._add(event, stream, changes)
                if child:
                    self._set_activity(self._child_of(event), "Responding...", changes)
            else:
                stream.text += event.text
                changes.append(
                    Change("append_text", stream, target_id=f"{stream.id}-text", text=event.text)
                )
        elif isinstance(event, AssistantMessage):
            key = self._request_key(event)
            self._finalized.add(key)
            stream = self._streams.pop(key, None)
            stream_id = self._stream_id(key)
            node = TextNode(dom_id("msg", event.id), "assistant", event.content, event.interrupted)
            visible = bool(event.content) or event.interrupted
            items, slot = self._container(event, changes)
            if stream is not None:
                position = next((i for i, item in enumerate(items) if item is stream), None)
                if visible and position is not None:
                    items[position] = node
                    self._register(node, slot)
                    changes.append(Change("replace", node, target_id=stream_id))
                else:
                    if position is not None:
                        items.pop(position)
                    changes.append(Change("remove", target_id=stream_id))
            elif visible:
                changes.append(Change("remove", target_id=stream_id))
                self._add(event, node, changes)
            else:
                changes.append(Change("remove", target_id=stream_id))
            if child and event.content:
                self._set_activity(self._child_of(event), "Responding...", changes)
        elif isinstance(event, ToolCall):
            tool = ToolNode(
                id=dom_id("tool", event.tool_use_id),
                tool_use_id=event.tool_use_id,
                name=event.name,
                input=dict(event.input),
            )
            self._add_tool(event, tool, changes)
            if child:
                self._set_activity(
                    self._child_of(event), format_tool_activity(event.name, tool.input), changes
                )
        elif isinstance(event, ToolResult):
            tool = self.tools.get(event.tool_use_id)
            created = tool is None
            if tool is None:
                tool = ToolNode(
                    id=dom_id("tool", event.tool_use_id),
                    tool_use_id=event.tool_use_id,
                    name="tool",
                    input={},
                )
            tool.result = event.content
            tool.status = "error" if event.is_error else "ok"
            tool.duration_ms = event.duration_ms
            tool.result_lines = event.result_lines
            tool.result_bytes = event.result_bytes
            if created:
                self._add_tool(event, tool, changes)
            else:
                self._changed(tool, changes)
            if child:
                sub = self._child_of(event)
                if event.is_error:
                    self._set_activity(sub, _first_line(tool_headline(tool)), changes)
                else:
                    self._set_activity(sub, "Thinking...", changes)
        elif isinstance(event, LLMRequest):
            self.usage.add(event)
            changes.append(Change("replace", self.usage, target_id="session-usage"))
            turn = self.current_turn()
            if child:
                sub = self._subagent(scope, index, changes)
                sub.cost_usd += event.cost_usd
                sub.context_tokens = event.context_tokens
                self._changed(sub, changes, sub.row_id)
            elif turn is not None:
                turn.usage.add(event)
                turn.duration_ms += event.duration_ms
                self._changed(turn, changes, turn.footer_id)
        elif isinstance(event, CompactionStart):
            node = CompactionNode(
                id=dom_id("compact", event.id),
                request_id=event.request_id,
                reason=event.reason,
                status="pending",
                tokens_before=event.tokens_before,
                estimated_next_request_cost_usd=event.estimated_next_request_cost_usd,
            )
            self._compactions[(scope, index, event.request_id)] = node
            self._add(event, node, changes)
        elif isinstance(event, CompactionComplete):
            node = self._compactions.pop((scope, index, event.request_id), None)
            created = node is None
            if node is None:
                node = CompactionNode(
                    id=dom_id("compact", event.id),
                    request_id=event.request_id,
                    reason=event.reason,
                    status="pending",
                    tokens_before=event.tokens_before,
                )
            node.status = event.status
            node.tokens_after = event.tokens_after
            node.duration_ms = event.duration_ms
            node.error = event.error
            node.cost_saved_usd = event.cost_saved_usd
            if event.estimated_next_request_cost_usd is not None:
                node.estimated_next_request_cost_usd = event.estimated_next_request_cost_usd
            node.summary = event.summary or ""
            if created:
                self._add(event, node, changes)
            else:
                self._changed(node, changes)
        elif isinstance(event, ContextCompacted):
            self._add(
                event,
                CompactionNode(
                    id=dom_id("note", event.id),
                    request_id=event.request_id,
                    reason=event.reason,
                    status="success",
                    tokens_before=event.tokens_before,
                    tokens_after=event.tokens_after,
                    duration_ms=event.duration_ms,
                    summary=event.summary,
                    marker=True,
                ),
                changes,
            )
        elif isinstance(event, TurnError):
            self._close_streams(scope, index, changes)
            self._add(
                event,
                NoticeNode(dom_id("note", event.id), "turn-error", "Turn failed", event.message),
                changes,
            )
            if child:
                sub = self._subagent(scope, index, changes)
                sub.status = "error"
                sub.error = _one_line(event.message) or "error"
                self._changed(sub, changes, sub.row_id)
        elif isinstance(event, TurnInterrupted):
            self._close_streams(scope, index, changes)
            self._add(
                event,
                NoticeNode(dom_id("note", event.id), "turn-interrupted", "Turn interrupted"),
                changes,
            )
            if child:
                sub = self._subagent(scope, index, changes)
                sub.status = "interrupted"
                self._changed(sub, changes, sub.row_id)
        elif isinstance(event, TurnComplete):
            self._close_streams(scope, index, changes)
            filtered = event.stop_reason == CONTENT_FILTERED
            if filtered:
                detail = CHILD_CONTENT_FILTERED_MESSAGE if child else CONTENT_FILTERED_MESSAGE
                self._add(
                    event,
                    NoticeNode(dom_id("note", event.id), "turn-error", "Response blocked", detail),
                    changes,
                )
            if child:
                sub = self._subagent(scope, index, changes)
                sub.status = "error" if filtered else "complete"
                sub.error = CHILD_CONTENT_FILTERED_MESSAGE if filtered else ""
                self._changed(sub, changes, sub.row_id)
            else:
                turn = self.current_turn()
                if turn is not None:
                    turn.stop_reason = event.stop_reason
                    self._changed(turn, changes, turn.footer_id)
        # SessionStarted and live-only status frames have no transcript representation.
        return changes


def build(events) -> TranscriptBuilder:
    """Build a transcript from a complete event list."""
    builder = TranscriptBuilder()
    for event in events:
        builder.apply(event)
    return builder

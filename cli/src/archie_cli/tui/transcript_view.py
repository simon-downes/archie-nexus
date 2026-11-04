"""TUI view of the shared transcript model.

``TranscriptView`` keeps an ordered list of top-level rows (messages, iteration
blocks, compaction rows, client-only rows) and applies the ``Change`` records that
``archie_shared.transcript.TranscriptBuilder.apply`` returns. Each row is built from
its model node when it is mounted, so a row can be unmounted and rebuilt later.

The main conversation shows the root transcript. A subagent screen shows one
subagent's items with the same view class.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from archie_shared.session.accounting import UsageTotals
from archie_shared.transcript import (
    CompactionNode,
    IterationNode,
    NoticeNode,
    StreamNode,
    SubagentNode,
    TextNode,
    ToolNode,
    TranscriptBuilder,
    TurnNode,
    plain_text,
    subagent_status_text,
    tool_extra,
    tool_headline,
    tool_preview,
)
from rich.text import Text
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from archie_cli.tui import theme
from archie_cli.tui.conversation import (
    AssistantMessage,
    CancelledMessage,
    ClientErrorMessage,
    CompactionEntry,
    CompactionMarker,
    ErrorMessage,
    ShellOutput,
    StreamingMessage,
    TurnStatus,
    UserMessage,
)

# ---------------------------------------------------------------------------
# Row widgets
# ---------------------------------------------------------------------------


def _markup(text: str) -> Text:
    try:
        return Text.from_markup(text)
    except Exception:  # noqa: BLE001 — summaries may contain stray brackets
        return Text(plain_text(text))


def _status_glyph(status: str) -> Text:
    if status in {"pending", "running"}:
        return Text("○", style=f"bold {theme.PRIMARY}")
    colour = theme.ERROR if status in {"error", "interrupted"} else theme.SUCCESS
    return Text("●", style=f"bold {colour}")


def render_tool_row(tool: ToolNode) -> Text:
    """The tool row: status glyph, summary, 10-line exec/shell preview, extra lines."""
    text = Text()
    text.append_text(_status_glyph(tool.status))
    text.append(" ")
    text.append_text(_markup(tool_headline(tool)))
    lines, more = tool_preview(tool)
    for line in lines:
        text.append("\n  ")
        text.append(line, style="dim")
    if more:
        text.append(f"\n  … +{more} lines", style="dim italic")
    extra = tool_extra(tool)
    if extra:
        text.append("\n")
        text.append_text(_markup(extra))
    return text


_AGENT_COLUMN = 20
_CONTEXT_COLUMN = 8
_COST_COLUMN = 9


def _fmt_tokens(tokens: int) -> str:
    return f"{tokens / 1000:.1f}k" if tokens >= 1000 else str(tokens)


def _fmt_column(value: str, width: int, *, align: str = "<") -> str:
    if len(value) > width:
        value = value[: max(1, width - 1)] + "…"
    return f"{value:{align}{width}}"


def render_subagent_row(sub: SubagentNode) -> Text:
    """One line: glyph, agent #index, context tokens, cost and current status."""
    agent = _fmt_column(sub.label, _AGENT_COLUMN)
    context = _fmt_column(_fmt_tokens(sub.context_tokens), _CONTEXT_COLUMN, align=">")
    cost = _fmt_column(f"${sub.cost_usd:.4f}", _COST_COLUMN, align=">")
    text = Text("  ")
    text.append_text(_status_glyph(sub.status))
    text.append(f" {agent} {context} {cost} - ")
    status = _markup(subagent_status_text(sub))
    if sub.status == "error":
        status.stylize(theme.ERROR)
    text.append_text(status)
    text.no_wrap = True
    text.overflow = "ellipsis"
    return text


class ToolClicked(Message):
    """A tool row was clicked; the app opens its detail screen."""

    def __init__(self, tool: ToolNode) -> None:
        super().__init__()
        self.tool = tool


class SubagentClicked(Message):
    """A subagent row was clicked; the app opens its transcript screen."""

    def __init__(self, sub: SubagentNode) -> None:
        super().__init__()
        self.sub = sub


class ToolRow(Static):
    """One tool call. Clicking it opens the tool detail screen."""

    DEFAULT_CSS = """
    ToolRow {
        height: auto;
    }
    ToolRow:hover {
        background: $boost;
    }
    """

    def __init__(self, tool: ToolNode) -> None:
        self.tool = tool
        super().__init__(render_tool_row(tool))

    def refresh_node(self) -> None:
        self.update(render_tool_row(self.tool))

    def on_click(self, event) -> None:
        event.stop()
        self.post_message(ToolClicked(self.tool))

    def get_copy_text(self) -> str:
        source = self.tool.input.get("source") if self.tool.name == "exec" else None
        if isinstance(source, str):
            return source
        command = self.tool.input.get("command") if self.tool.name == "shell" else None
        if isinstance(command, str):
            return command
        return plain_text(tool_headline(self.tool))


class SubagentRow(Static):
    """One subagent under its task tool. Clicking it opens the subagent transcript."""

    DEFAULT_CSS = """
    SubagentRow {
        height: 1;
    }
    SubagentRow:hover {
        background: $boost;
    }
    """

    def __init__(self, sub: SubagentNode) -> None:
        self.sub = sub
        super().__init__(render_subagent_row(sub))

    def refresh_node(self) -> None:
        self.update(render_subagent_row(self.sub))

    def on_click(self, event) -> None:
        event.stop()
        self.post_message(SubagentClicked(self.sub))


class IterationBlock(Widget):
    """The tool calls of one provider response, under a dim left border.

    Rows are a tool row per call, each followed by its subagent rows. The block
    builds its rows from the ``IterationNode`` when mounted and on each ``sync``.
    """

    can_focus = True

    DEFAULT_CSS = """
    IterationBlock {
        margin: 0 0 1 0;
        padding: 0 2;
        height: auto;
        border-left: thick transparent;
    }
    """

    def __init__(self, node: IterationNode) -> None:
        super().__init__(id=f"view-{node.id}")
        self.node = node
        self._rows: dict[str, ToolRow | SubagentRow] = {}
        self._ready = False

    def on_mount(self) -> None:
        self._ready = True
        self.sync()

    def sync(self) -> None:
        """Add rows for new tools and subagents and redraw the others."""
        if not self._ready:
            return  # on_mount builds every row from the node
        previous: Widget | None = None
        for tool in self.node.tools:
            previous = self._ensure(tool.id, lambda t=tool: ToolRow(t), previous)
            for sub in tool.children:
                previous = self._ensure(sub.id, lambda s=sub: SubagentRow(s), previous)

    def _ensure(self, key: str, factory: Callable[[], Widget], previous: Widget | None):
        row = self._rows.get(key)
        if row is None:
            row = factory()
            self._rows[key] = row
            if previous is None:
                self.mount(row, before=0) if self.children else self.mount(row)
            else:
                self.mount(row, after=previous)
        else:
            row.refresh_node()
        return row

    def get_copy_text(self) -> str:
        return "\n\n".join(
            row.get_copy_text() for row in self._rows.values() if isinstance(row, ToolRow)
        )


class SubagentBlock(Widget):
    """A subagent shown at the top level (its launching task tool is unknown)."""

    DEFAULT_CSS = """
    SubagentBlock {
        margin: 0 0 1 0;
        padding: 0 1;
        height: auto;
    }
    """

    def __init__(self, sub: SubagentNode) -> None:
        super().__init__()
        self.sub = sub
        self.row = SubagentRow(sub)

    def compose(self):
        yield self.row

    def sync(self) -> None:
        self.row.refresh_node()


# ---------------------------------------------------------------------------
# Node → widget
# ---------------------------------------------------------------------------


def _compaction_label(reason: str) -> str:
    return {"threshold": "threshold tokens", "cold_cache": "threshold cold cache"}.get(
        reason, reason
    )


def _complete_compaction(entry: CompactionEntry, node: CompactionNode) -> None:
    error = node.error
    if node.status != "success" and error is None:
        error = f"Compaction {node.status}"
    cost = (
        node.estimated_next_request_cost_usd
        if node.estimated_next_request_cost_usd is not None
        else node.cost_saved_usd
    )
    entry.complete(node.tokens_after, cost, node.duration_ms, error)


def widget_for(node) -> Widget:
    """Build the row widget for one top-level transcript node."""
    if isinstance(node, TextNode):
        if node.role == "user":
            return UserMessage(node.content)
        if not node.content:
            # An interrupted reply with no text; the turn's "Cancelled" row follows.
            hidden = Static()
            hidden.display = False
            return hidden
        return AssistantMessage(node.content.rstrip("\n"))
    if isinstance(node, StreamNode):
        return StreamingMessage(node.text)
    if isinstance(node, IterationNode):
        return IterationBlock(node)
    if isinstance(node, SubagentNode):
        return SubagentBlock(node)
    if isinstance(node, CompactionNode):
        if node.marker:
            return CompactionMarker(
                node.reason, node.tokens_before, node.tokens_after or 0, node.summary
            )
        entry = CompactionEntry(_compaction_label(node.reason), node.tokens_before)
        if node.status != "pending":
            _complete_compaction(entry, node)
        return entry
    if isinstance(node, NoticeNode):
        if node.variant == "turn-interrupted":
            return CancelledMessage()
        return ErrorMessage(node.detail or node.title)
    return Static(type(node).__name__)


def refresh_widget(widget: Widget, node) -> None:
    """Redraw a mounted row after its node changed."""
    if isinstance(widget, (IterationBlock, SubagentBlock)):
        widget.sync()
    elif isinstance(widget, CompactionEntry) and isinstance(node, CompactionNode):
        _complete_compaction(widget, node)


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------


@dataclass
class _Row:
    key: str
    node: object | None  # None for client-only rows
    factory: Callable[[], Widget] | None = None  # client-only rows
    widget: Widget | None = None

    def build(self) -> Widget:
        return self.factory() if self.factory is not None else widget_for(self.node)


# Window parameters (plan 078 D9, from Tau): mount about WINDOW_ROWS rows, move the
# window by WINDOW_PAGE rows at a time, and let live appends overshoot by
# WINDOW_OVERSCAN rows before the oldest mounted rows are dropped.
WINDOW_ROWS = 200
WINDOW_PAGE = 80
WINDOW_OVERSCAN = 40


class WindowBoundary(Static):
    """Marks rows above or below the mounted window; scrolling past it pages them in."""

    DEFAULT_CSS = """
    WindowBoundary {
        height: 1;
        padding: 0 2;
    }
    """

    def __init__(self, direction: str) -> None:
        super().__init__(classes=f"window-boundary window-{direction}")
        self.direction = direction
        self.hidden_count = 0

    def set_count(self, count: int) -> None:
        self.hidden_count = count
        noun = "row" if count == 1 else "rows"
        verb = "up" if self.direction == "earlier" else "down"
        self.update(
            Text(f"··· {count} {self.direction} {noun} · scroll {verb} to show", style="dim")
        )


class TranscriptView(VerticalScroll):
    """Scrollable list of transcript rows driven by model changes.

    Every row stays in ``rows``; only ``rows[start:end]`` (``window``) have mounted
    widgets. Scrolling to the top or bottom edge pages the window, and boundary rows
    show how many rows are outside it.
    """

    DEFAULT_CSS = """
    TranscriptView {
        height: 1fr;
        padding: 0;
    }
    """

    _autoscroll = True

    def __init__(
        self,
        *,
        root_slot: str | None = None,
        window: int = WINDOW_ROWS,
        page: int = WINDOW_PAGE,
        overscan: int = WINDOW_OVERSCAN,
        **kwargs,
    ) -> None:
        """``root_slot`` is the model slot whose items are rows: ``None`` for the
        root transcript (preamble and every turn), or a subagent's ``items_id``."""
        super().__init__(**kwargs)
        self._root_slot = root_slot
        self._rows: list[_Row] = []
        self._index: dict[str, _Row] = {}
        self._local_ids = 0
        self._suspended = False
        self._window = window
        self._page = page
        self._overscan = overscan
        self._start = 0
        self._end = 0
        self._shift_pending = False
        self._window_generation = 0
        self._top: WindowBoundary | None = None
        self._bottom: WindowBoundary | None = None

    # -- scrolling -------------------------------------------------------------

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        self._autoscroll = new_value >= self.max_scroll_y and self.window_is_latest
        if new_value <= 1 and new_value < old_value and self._start > 0:
            self._schedule_shift("earlier")
        elif (
            new_value >= max(0, self.max_scroll_y - 1)
            and new_value > old_value
            and self._end < len(self._rows)
        ):
            self._schedule_shift("later")

    def scroll_if_at_bottom(self) -> None:
        if self._autoscroll and not self._suspended and self.window_is_latest:
            self.scroll_end(animate=False)

    # -- window ----------------------------------------------------------------

    @property
    def window(self) -> tuple[int, int]:
        """The mounted slice of ``rows`` as ``(start, end)``."""
        return self._start, self._end

    @property
    def window_is_latest(self) -> bool:
        return self._end >= len(self._rows)

    def _schedule_shift(self, direction: str) -> None:
        if self._shift_pending or self._suspended:
            return
        self._shift_pending = True
        self.call_later(self.shift_window, direction)

    def shift_window(self, direction: str) -> None:
        """Page the window one step ``"earlier"`` or ``"later"``.

        The row at the edge being paged stays mounted and is scrolled back to where
        it was (top edge for earlier, bottom edge for later) after the next refresh.
        """
        anchor: Widget | None = None
        try:
            start, end, total = self._start, self._end, len(self._rows)
            if direction == "earlier" and start > 0:
                anchor = self._rows[start].widget if start < end else None
                new_start = max(0, start - self._page)
                new_end = min(total, max(new_start + self._window, start + 1))
                self._set_window(new_start, new_end)
            elif direction == "later" and end < total:
                anchor = self._rows[end - 1].widget if end > start else None
                new_end = min(total, end + self._page)
                new_start = max(0, min(new_end - self._window, end - 1))
                self._set_window(new_start, new_end)
        finally:
            if anchor is None:
                self._shift_pending = False
        if not self.window_is_latest:
            self._autoscroll = False  # before the anchor restore lands

        if anchor is None:
            return

        generation = self._window_generation

        def restore() -> None:
            try:
                # Skip if the window moved again first (``jump_to_latest``).
                if anchor.is_attached and generation == self._window_generation:
                    self.scroll_to_widget(
                        anchor,
                        top=direction == "earlier",
                        animate=False,
                        immediate=True,
                        force=True,
                    )
            finally:
                self._shift_pending = False

        self.call_after_refresh(restore)

    def jump_to_latest(self) -> None:
        """Mount the newest rows and follow the end of the transcript."""
        total = len(self._rows)
        self._window_generation += 1  # cancels a pending anchor restore
        self._set_window(max(0, total - self._window), total)
        self._autoscroll = True
        if not self._suspended:
            self.scroll_end(animate=False)

    def _set_window(self, new_start: int, new_end: int) -> None:
        """Mount exactly ``rows[new_start:new_end]``, keeping widgets already mounted."""
        for i in range(self._start, self._end):
            if not new_start <= i < new_end:
                self._unmount(self._rows[i])
        self._start, self._end = new_start, new_end
        self._sync_boundaries()
        self._mount_missing()

    def _mount_missing(self) -> None:
        """Mount the unmounted rows inside the window, in order, one batch per gap."""
        i = self._start
        while i < self._end:
            if self._rows[i].widget is not None:
                i += 1
                continue
            j = i
            while j < self._end and self._rows[j].widget is None:
                j += 1
            widgets = []
            for row in self._rows[i:j]:
                row.widget = row.build()
                widgets.append(row.widget)
            following = self._rows[j].widget if j < self._end else self._bottom
            if following is not None:
                self.mount(*widgets, before=following)
            else:
                self.mount(*widgets)
            i = j

    @staticmethod
    def _unmount(row: _Row) -> None:
        if row.widget is not None:
            row.widget.remove()
            row.widget = None

    def _sync_boundaries(self) -> None:
        """Show, update or drop the boundary rows for the current window."""
        above = self._start
        below = len(self._rows) - self._end
        if above and self._top is None:
            self._top = WindowBoundary("earlier")
            if self.children:
                self.mount(self._top, before=0)
            else:
                self.mount(self._top)
        elif not above and self._top is not None:
            self._top.remove()
            self._top = None
        if self._top is not None:
            self._top.set_count(above)
        if below and self._bottom is None:
            self._bottom = WindowBoundary("later")
            self.mount(self._bottom)
        elif not below and self._bottom is not None:
            self._bottom.remove()
            self._bottom = None
        if self._bottom is not None:
            self._bottom.set_count(below)

    # -- rows ------------------------------------------------------------------

    @property
    def rows(self) -> list[_Row]:
        return self._rows

    def _add_row(self, row: _Row) -> None:
        was_latest = self.window_is_latest
        self._rows.append(row)
        self._index[row.key] = row
        if self._suspended:
            return  # ``resume`` mounts the window
        mounted = self._end - self._start
        if not was_latest:
            self._sync_boundaries()  # reading earlier rows: count the new one below
        elif mounted < self._window + self._overscan:
            self._end += 1
            self._mount_missing()
        elif self._autoscroll:
            # Following the end at the cap: drop the oldest rows beyond the window.
            self._set_window(len(self._rows) - self._window, len(self._rows))
        else:
            self._sync_boundaries()  # scrolled up at the cap: leave the window alone

    def _remove_row(self, row: _Row) -> None:
        i = self._rows.index(row)
        self._unmount(row)
        del self._rows[i]
        self._index.pop(row.key, None)
        if i < self._start:
            self._start -= 1
            self._end -= 1
        elif i < self._end:
            self._end -= 1
        if not self._suspended:
            self._sync_boundaries()

    def _replace_row(self, row: _Row, node) -> None:
        self._index.pop(row.key, None)
        row.key = node.id
        row.node = node
        self._index[row.key] = row
        old = row.widget
        if old is None:
            return  # built from the new node when it enters the window
        if (
            isinstance(old, StreamingMessage)
            and isinstance(node, TextNode)
            and not node.interrupted
            and old.text.rstrip("\n") == node.content.rstrip("\n")
        ):
            old.finish()  # same text: keep the rendered Markdown
            return
        row.widget = row.build()
        self.mount(row.widget, after=old)
        old.remove()

    def add_local(self, factory: Callable[[], Widget]) -> None:
        """Append a client-only row (not part of the session transcript)."""
        self._local_ids += 1
        self._add_row(_Row(key=f"local-{self._local_ids}", node=None, factory=factory))
        self.scroll_if_at_bottom()

    def add_node(self, node) -> None:
        """Append a row for a model node (used to build a view from existing state)."""
        self._append_node(node)

    def _append_node(self, node) -> None:
        if isinstance(node, TurnNode):
            # A new user message: return to the latest rows and follow them.
            if not self._suspended and not self.window_is_latest:
                self.jump_to_latest()
            self._autoscroll = True
            if node.user is not None and node.user.content:
                self._add_row(_Row(node.user.id, node.user))
            for item in node.items:
                self._add_row(_Row(item.id, item))
            if not self._suspended:
                self.scroll_end(animate=False)
            return
        self._add_row(_Row(node.id, node))
        self.scroll_if_at_bottom()

    # -- model changes -----------------------------------------------------------

    def _is_root_slot(self, builder: TranscriptBuilder, slot: str | None) -> bool:
        if self._root_slot is not None:
            return slot == self._root_slot
        if slot is None:
            return True
        return isinstance(builder.nodes.get(builder.owner_of.get(slot, "")), TurnNode)

    def _touch(self, builder: TranscriptBuilder, node_id: str) -> None:
        """Redraw the mounted row that shows ``node_id``, if this view shows it.

        Walks up the model from the node. Reaching a subagent other than the node
        itself means the node is inside that subagent's transcript, which this view
        shows only as the subagent's row. A row outside the window has no widget;
        it is built from current node state when it enters the window.
        """
        current = node_id
        for nid in (node_id, *builder.ancestors(node_id)):
            row = self._index.get(nid)
            if row is not None:
                if row.widget is not None:
                    refresh_widget(row.widget, row.node)
                return
            if nid != current and isinstance(builder.nodes.get(nid), SubagentNode):
                return
            if nid != node_id:
                current = nid

    def apply_changes(self, builder: TranscriptBuilder, changes) -> None:
        for change in changes:
            kind = change.kind
            if kind == "append":
                if self._is_root_slot(builder, change.parent_id):
                    self._append_node(change.node)
                else:
                    self._touch(builder, change.node.id)
            elif kind == "replace":
                node = change.node
                if isinstance(node, (UsageTotals, TurnNode)):
                    continue
                row = self._index.get(change.target_id)
                if row is not None and change.target_id != node.id:
                    self._replace_row(row, node)  # a stream became a message
                elif row is not None:
                    if row.widget is not None:
                        refresh_widget(row.widget, node)
                else:
                    self._touch(builder, node.id)
            elif kind == "append_text":
                row = self._index.get(change.node.id)
                if row is not None and isinstance(row.widget, StreamingMessage):
                    row.widget.append(change.text)
                    self.scroll_if_at_bottom()
            elif kind == "remove":
                row = self._index.get(change.target_id)
                if row is not None:
                    self._remove_row(row)

    # -- replay ----------------------------------------------------------------

    def suspend(self) -> None:
        """Stop mounting rows; ``resume`` mounts the latest window from final state."""
        self._suspended = True

    def resume(self) -> None:
        self._suspended = False
        self.jump_to_latest()

    async def reset(self) -> None:
        """Drop every row (a full history reload follows)."""
        self._rows.clear()
        self._index.clear()
        self._start = self._end = 0
        self._top = self._bottom = None
        self._shift_pending = False
        await self.remove_children()

    def finish_streams(self) -> None:
        """Stop the streaming indicator on any mounted stream rows."""
        for row in self._rows[self._start : self._end]:
            if isinstance(row.widget, StreamingMessage):
                row.widget.finish()


class Conversation(TranscriptView):
    """The main session transcript plus client-only rows."""

    def add_turn_status(
        self,
        duration_s: float,
        input_tokens: int,
        cache_read: int,
        cache_write: int,
        output_tokens: int,
        cost: float,
    ) -> None:
        """Add a client-only summary for a completed turn."""
        self.add_local(
            lambda: TurnStatus(
                duration_s, input_tokens, cache_read, cache_write, output_tokens, cost
            )
        )

    def add_client_error(self, content: str) -> None:
        """Add a client/transport error, marked as local (not in session log)."""
        self.add_local(lambda: ClientErrorMessage(content))

    def add_shell_output(self, command: str, output: str, exit_code: int = 0) -> None:
        """Add direct shell command output (! prefix, no LLM involvement)."""
        self.add_local(lambda: ShellOutput(command, output, exit_code))

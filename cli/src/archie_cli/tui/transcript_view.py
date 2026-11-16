"""TUI view of the shared transcript model.

``TranscriptView`` keeps an ordered list of top-level rows (messages, iteration
blocks, compaction rows, client-only rows) and applies the ``Change`` records that
``archie_shared.transcript.TranscriptBuilder.apply`` returns. Each row is built from
its model node when it is mounted, so a row can be unmounted and rebuilt later.

The main conversation shows the root transcript. A subagent view (``detail.py``) shows
one subagent's items with the same view class.

A view built for a session that already has history is filled with ``load``: the rows
of the builder's current root nodes, with each client-only entry put back after the
row it followed (084 D6).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from archie_shared.kiro import kiro_model_id
from archie_shared.session.accounting import UsageTotals
from archie_shared.tool_rows import row_lines, tool_row
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
    tool_headline,
)
from rich.style import Style
from rich.text import Text
from textual import events
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from archie_cli.tui import theme
from archie_cli.tui.animation import Animated, spinner
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
from archie_cli.tui.session import ClientErrorEntry, LocalEntry, TurnStatusEntry

# ---------------------------------------------------------------------------
# Row widgets
# ---------------------------------------------------------------------------


def safe_markup(text: str) -> Text:
    """Rich markup to ``Text``, or the plain text when the markup can't render.

    ``Text.from_markup`` accepts any tag name and only fails when Textual renders the
    span, which crashes the app. Every span style is parsed here instead, so text that
    isn't really markup (a raw error, a stray ``[name]``) falls back to plain text.
    """
    try:
        rendered = Text.from_markup(text)
        for span in rendered.spans:
            if isinstance(span.style, str):
                Style.parse(span.style)
    except Exception:  # noqa: BLE001 — summaries may contain stray brackets
        return Text(plain_text(text))
    return rendered


_GLYPH_STYLES = {
    "running": f"bold {theme.PRIMARY}",
    "ok": f"bold {theme.SUCCESS}",
    "error": f"bold {theme.ERROR}",
    "waiting": f"bold {theme.WARNING}",
}
_ROLE_STYLES = {
    "label": f"bold {theme.TOOL_NAME}",
    "muted": theme.MUTED,
    "added": theme.SUCCESS,
    "removed": theme.ERROR,
    "error": theme.ERROR,
    "target": theme.TARGET,
}


def status_glyph(status: str, frame: int = 0) -> Text:
    """A node's status glyph: the spinner while it runs, else a green or red dot.

    Args:
        status: A tool's or subagent's status (``pending``/``running``, ``ok``/
            ``complete``, ``error``/``interrupted``).
        frame: Animation frame, which picks the spinner glyph.
    """
    if status in {"pending", "running"}:
        return Text(spinner(frame), style=_GLYPH_STYLES["running"])
    colour = theme.ERROR if status in {"error", "interrupted"} else theme.SUCCESS
    return Text("●", style=f"bold {colour}")


def render_tool_row(tool: ToolNode, width: int = 120, frame: int = 0) -> Text:
    """The tool's two-line row at ``width`` columns (plans/tui-tool-rows.md).

    Args:
        tool: The call.
        width: Columns available; both lines are cut to fit.
        frame: Animation frame, which picks a running row's spinner glyph.
    """
    parts = tool_row(tool)
    lines = []
    for segments in row_lines(parts, width, spinner(frame)):
        line = Text(no_wrap=True)
        for text, role in segments:
            line.append(text, _GLYPH_STYLES[parts.glyph] if role == "glyph" else _ROLE_STYLES[role])
        lines.append(line)
    text = Text("\n").join(lines)
    text.no_wrap = True
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


def render_subagent_row(sub: SubagentNode, frame: int = 0) -> Text:
    """One line: glyph, agent #index, context tokens, cost and current status."""
    agent = _fmt_column(sub.label, _AGENT_COLUMN)
    context = _fmt_column(_fmt_tokens(sub.context_tokens), _CONTEXT_COLUMN, align=">")
    cost = _fmt_column(f"${sub.cost_usd:.4f}", _COST_COLUMN, align=">")
    text = Text("  ")
    text.append_text(status_glyph(sub.status, frame))
    text.append(f" {agent} {context} {cost} - ")
    status = safe_markup(subagent_status_text(sub))
    if sub.status == "error":
        status.stylize(theme.ERROR)
    text.append_text(status)
    text.no_wrap = True
    text.overflow = "ellipsis"
    return text


class ToolClicked(Message):
    """A tool row was clicked; the session pane opens its detail view."""

    def __init__(self, tool: ToolNode) -> None:
        super().__init__()
        self.tool = tool


class SubagentClicked(Message):
    """A subagent row was clicked; the session pane opens its transcript view."""

    def __init__(self, sub: SubagentNode) -> None:
        super().__init__()
        self.sub = sub


class ToolRow(Animated):
    """One tool call as two lines, cut to the row's width. A click opens its details.

    A running call's spinner follows the app's animation clock (084 D16).
    """

    DEFAULT_CSS = """
    ToolRow {
        height: 2;
    }
    ToolRow:hover {
        background: $boost;
    }
    """

    def __init__(self, tool: ToolNode) -> None:
        super().__init__()
        self.tool = tool

    @property
    def animating(self) -> bool:
        """True while the call runs (a waiting question shows ``?`` instead)."""
        return self.tool.result is None and self.tool.name != "question"

    def render(self) -> Text:
        return render_tool_row(self.tool, self.content_size.width or 120, self.frame)

    def refresh_node(self) -> None:
        self.refresh()
        self.sync_animation()

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


class SubagentRow(Animated):
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
        super().__init__()
        self.sub = sub

    @property
    def animating(self) -> bool:
        """True while the subagent runs."""
        return self.sub.status == "running"

    def render(self) -> Text:
        return render_subagent_row(self.sub, self.frame)

    def refresh_node(self) -> None:
        self.refresh()
        self.sync_animation()

    def on_click(self, event) -> None:
        event.stop()
        self.post_message(SubagentClicked(self.sub))


class IterationBlock(Widget):
    """The tool calls of one provider response.

    Blocks have no margin, so the tool rows of consecutive iterations stack with no
    gap; other rows keep a blank line from them with their own margins.

    Rows are a tool row per call, each followed by its subagent rows. The block
    builds its rows from the ``IterationNode`` when mounted and on each ``sync``.
    """

    can_focus = False

    DEFAULT_CSS = """
    IterationBlock {
        margin: 0;
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
        margin: 0;
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
        if kiro_model_id(node.model_key) is not None:
            # Kiro rows carry no token figures, so the plain reason is the label (091 D3).
            entry = CompactionEntry(node.reason, node.tokens_before, kiro=True)
        else:
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


# Window parameters: mount about WINDOW_ROWS rows, move the window by WINDOW_PAGE rows
# at a time, and let live appends overshoot by WINDOW_OVERSCAN rows before the oldest
# mounted rows are dropped. Plan 084 replaced 078 D9's 200/80/40 (from Tau) because
# every session switch remounts the window: on archie-01m32ky6rn.jsonl (11.5 MB), a
# window of 200 rows meant about 5,100 widgets and 5.2 s to switch back to the
# session; a window of 40 means about 490 widgets and about 0.44 s.
WINDOW_ROWS = 40
WINDOW_PAGE = 20
WINDOW_OVERSCAN = 20
WINDOW_SCROLL_ROWS = 200
"""Most rows paging keeps mounted before it drops rows at the far end.

Scrolling pages rows in without dropping the ones already mounted, up to this many, so
scrolling back over them mounts nothing; returning to the end of the latest rows
(scrolling there, End, a jump, or the first live row while following) trims back to
:data:`WINDOW_ROWS`. Measured on
archie-01m32ky6rn.jsonl (162 rows; a 20-row page costs 0.4-1.6 s of mounting): wheeling
to the top and back took 7 + 7 pages at 40 rows and 7 + 0 at 200, while a 200-row window
made a height change 0.17 -> 0.84 s and leaving the pane 0.1 -> 0.27 s. The 200 is 078
D9's window; switching still mounts only :data:`WINDOW_ROWS`.
"""
FILL_PAGES = 10
"""Most pages added at once to fill a viewport the window's rows don't fill."""
LAYOUT_WAITS = 5
"""Refreshes to wait for mounted rows to be laid out before deciding to fill."""


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
    show how many rows are outside it. A window whose rows are shorter than the
    viewport can't scroll, so after the window moves or the view is resized, earlier
    rows are added a page at a time until it can (at most :data:`FILL_PAGES` pages),
    and the window keeps that larger size.
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
        scroll_span: int = WINDOW_SCROLL_ROWS,
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
        self._span = window  # rows to keep mounted; grows to fill the viewport
        self._scroll_span = scroll_span  # rows paging keeps mounted (at least ``_span``)
        self._page = page
        self._filling = False
        self._overscan = overscan
        self._start = 0
        self._end = 0
        self._shift_pending = False
        self._wanted_page: str | None = None  # a scroll at an edge while a page was pending
        self._pending_direction: str | None = None  # the pending (or last) page's direction
        self._shifts = 0  # pages so far; only the latest page's restore runs
        self._window_generation = 0
        self._jumping = False  # a jump to the latest rows hasn't reached the end yet
        self._top: WindowBoundary | None = None
        self._bottom: WindowBoundary | None = None

    # -- scrolling -------------------------------------------------------------

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        self._autoscroll = new_value >= self.max_scroll_y and self.window_is_latest
        direction: str | None = None
        if new_value <= 1 and new_value < old_value and self._start > 0 and not self._jumping:
            # (Not while a jump lands: rows unmounted above can clamp the offset to 0.)
            direction = "earlier"
        elif (
            new_value >= max(0, self.max_scroll_y - 1)
            and new_value > old_value
            and self._end < len(self._rows)
        ):
            direction = "later"
        if direction is not None:
            if not self._shift_pending:
                self._schedule_shift(direction)
            elif direction == self._pending_direction:
                # Checked again once the pending page lands (``_retry_page``): a
                # scrollbar drag held at the edge doesn't change ``scroll_y`` again.
                # (Not the other way: the pending page's layout clamps the offset there.)
                self._wanted_page = direction
        elif (
            self._autoscroll
            and new_value > old_value
            and not self._shift_pending
            and not self._jumping
        ):
            self._schedule_trim()  # scrolled back to the end

    # A scroll toward an edge the view is already at doesn't change ``scroll_y``, so
    # ``watch_scroll_y`` can't page. That happens when the view reached the edge while a
    # page was pending (its trigger was dropped) or when a page added no lines (rows
    # that aren't displayed). The wheel and scroll keys page there instead.

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if not (event.ctrl or event.shift) and self._page_at_edge("later"):
            event.prevent_default()
            event.stop()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if not (event.ctrl or event.shift) and self._page_at_edge("earlier"):
            event.prevent_default()
            event.stop()

    def action_scroll_down(self) -> None:
        if not self._page_at_edge("later"):
            super().action_scroll_down()

    def action_page_down(self) -> None:
        if not self._page_at_edge("later"):
            super().action_page_down()

    def action_scroll_end(self) -> None:
        if self.window_is_latest or self._suspended:
            super().action_scroll_end()
        else:
            self.jump_to_latest()

    def action_scroll_up(self) -> None:
        if not self._page_at_edge("earlier"):
            super().action_scroll_up()

    def action_page_up(self) -> None:
        if not self._page_at_edge("earlier"):
            super().action_page_up()

    def action_scroll_home(self) -> None:
        if self._start == 0 or self._suspended:
            super().action_scroll_home()
        else:
            self.jump_to_first()

    def _page_at_edge(self, direction: str) -> bool:
        """Page ``direction`` if the view is at that edge and rows lie beyond the window.

        A scroll that arrives while a page is pending is remembered and checked again
        once that page has landed (``_retry_page``).

        Returns:
            True if the scroll is handled here (paged or remembered).
        """
        if self._suspended:
            return False
        if direction == "later":
            at_edge = self._end < len(self._rows) and self.scroll_y >= self.max_scroll_y
        else:
            at_edge = self._start > 0 and self.scroll_y <= 0 and not self._jumping
        if not at_edge:
            return False
        if self._shift_pending:
            self._wanted_page = direction
        else:
            self._schedule_shift(direction)
        return True

    def _retry_page(self) -> None:
        """Apply a scroll that arrived at an edge while the last page was pending."""
        direction, self._wanted_page = self._wanted_page, None
        if direction is not None:
            self._page_at_edge(direction)

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

    @property
    def following(self) -> bool:
        """True while the view follows the end of the transcript."""
        return self._autoscroll and self.window_is_latest

    def _schedule_shift(self, direction: str) -> None:
        if self._shift_pending or self._suspended:
            return
        self._shift_pending = True
        self._pending_direction = direction
        self.call_later(self.shift_window, direction)

    def shift_window(self, direction: str) -> None:
        """Page the window one step ``"earlier"`` or ``"later"``.

        Rows already mounted stay mounted up to ``scroll_span`` rows; beyond that the
        rows at the far end are dropped. The first row in view that stays mounted is the
        anchor: after the next refresh the view scrolls so that row is back at the same
        offset from the view's top, so the content in view doesn't move.
        """
        anchor: tuple[Widget, int] | None = None
        self._pending_direction = direction
        start, end, total = self._start, self._end, len(self._rows)
        if direction == "earlier" and start > 0:
            new_start = max(0, start - self._page)
            new_end = min(total, max(new_start + self._span, start + 1))
            new_end = max(new_end, min(end, new_start + self._scroll_span))
        elif direction == "later" and end < total:
            new_end = min(total, end + self._page)
            new_start = max(0, min(new_end - self._span, end - 1))
            new_start = min(new_start, max(start, new_end - self._scroll_span))
        else:
            self._shift_pending = False
            self._wanted_page = None
            return
        try:
            anchor = self._anchor(new_start, new_end)
            self._set_window(new_start, new_end)
        except BaseException:
            self._shift_pending = False  # no restore will clear it
            raise
        finally:
            self._shifts += 1
        # Pending until the restore lands: scrolls that reach an edge meanwhile only ask
        # for another page, checked once the restore has landed (``_retry_page``).
        self._shift_pending = anchor is not None
        if not self.window_is_latest:
            self._autoscroll = False  # before the anchor restore lands

        if anchor is None:
            self._retry_page()
            return

        widget, offset = anchor
        current = self._shifts, self._window_generation

        def restore() -> None:
            if current != (self._shifts, self._window_generation):
                return  # the window moved again; that move restores (or follows the end)
            wanted = self._wanted_page
            try:
                if widget.is_attached:
                    self.scroll_to(
                        y=widget.virtual_region.y - offset,
                        animate=False,
                        immediate=True,
                        force=True,
                    )
            finally:
                # The restore's own scroll isn't a request to page: when rows were
                # dropped beyond the edge it lands there, and paging back would undo this.
                self._wanted_page = wanted
                self._shift_pending = False
            self._retry_page()

        self.call_after_refresh(restore)

    def _anchor(self, new_start: int, new_end: int) -> tuple[Widget, int] | None:
        """The first laid-out row in view that stays in ``rows[new_start:new_end]``.

        Returns:
            The row's widget and its top's offset from the view's top, or None if no
            row that stays is laid out (nothing to keep in place).
        """
        top = round(self.scroll_y)
        fallback: tuple[Widget, int] | None = None
        for i in range(max(self._start, new_start), min(self._end, new_end)):
            widget = self._rows[i].widget
            if widget is None or not widget.is_attached or not widget.display:
                continue
            region = widget.virtual_region
            if not region.height:
                continue  # not laid out yet
            if region.bottom > top:
                return widget, region.y - top
            fallback = widget, region.y - top
        return fallback

    def jump_to_latest(self) -> None:
        """Mount the newest rows and follow the end of the transcript."""
        total = len(self._rows)
        self._jump(max(0, total - self._span), total, to_end=True)

    def jump_to_first(self) -> None:
        """Mount the first rows and scroll to the top."""
        self._jump(0, min(len(self._rows), self._span), to_end=False)

    def _jump(self, start: int, end: int, *, to_end: bool) -> None:
        """Mount ``rows[start:end]`` and scroll to the end or the top."""
        self._window_generation += 1  # cancels a pending anchor restore
        self._shift_pending = False
        self._wanted_page = None
        self._set_window(start, end)
        self._autoscroll = to_end
        if not self._suspended:
            self._jumping = True
            if to_end:
                self.scroll_end(animate=False)
            else:
                self.scroll_home(animate=False)

            def landed() -> None:
                self._jumping = False

            self.call_after_refresh(landed)

    def _schedule_trim(self) -> None:
        """After the next refresh, trim a grown window if the view still follows the end."""
        if self._grown and not self._suspended:
            self.call_after_refresh(self._trim_to_span)

    @property
    def _grown(self) -> bool:
        """True if paging left more rows mounted than live appends would."""
        return self._end - self._start > self._span + self._overscan

    def _trim_to_span(self) -> None:
        """Mount only the latest ``_span`` rows again while the view follows the end.

        Rows paged in by scrolling up stay mounted (up to ``scroll_span``) so scrolling
        back over them is free; once the view is back at the end they only make every
        layout slower (a 200-row window turns a height change from 0.17 s into 0.84 s).
        Up to ``overscan`` extra rows stay, as for live appends.
        """
        if (
            self._suspended
            or self._shift_pending
            or self._jumping
            or not self.following
            or not self._grown
        ):
            return
        self.jump_to_latest()

    def _set_window(self, new_start: int, new_end: int, *, fill: bool = True) -> None:
        """Mount exactly ``rows[new_start:new_end]``, keeping widgets already mounted.

        With ``fill``, earlier rows are paged in afterwards if the viewport has no
        scroll room (``_fill_viewport``).
        """
        for i in range(self._start, self._end):
            if not new_start <= i < new_end:
                self._unmount(self._rows[i])
        self._start, self._end = new_start, new_end
        self._sync_boundaries()
        self._mount_missing()
        if fill:
            self._schedule_fill()

    def on_resize(self) -> None:
        self._schedule_fill()
        if self.following:
            self._schedule_trim()  # after the fill check, so a trimmed window refills

    def _schedule_fill(self, pages: int | None = None) -> None:
        """Check after the next refresh (once the rows are laid out) whether to fill."""
        if self._filling or self._suspended or not self.is_attached:
            return
        self._filling = True
        self.call_after_refresh(self._fill_viewport, FILL_PAGES if pages is None else pages)

    def _fill_viewport(self, pages: int, waits: int = LAYOUT_WAITS) -> None:
        """Page earlier rows in while the mounted rows don't fill the viewport.

        Without scroll room, scrolling can't reach the top edge, so earlier rows could
        never page in. Each step adds :attr:`_page` rows above (keeping the rows below)
        and checks again after the next refresh, up to ``pages`` steps. The window then
        keeps its larger size, so appends don't shrink it back. A view following the
        end stays at the end; otherwise the previous first row stays in view.

        Rows mounted but not yet laid out (fewer lines than rows, as every row takes
        at least one) are waited for, up to ``waits`` refreshes.
        """
        self._filling = False
        if self._suspended or not self.is_attached or pages <= 0:
            return
        if self._start == 0 or self.max_scroll_y > 0:
            return
        window = [row.widget for row in self._rows[self._start : self._end]]
        if self.virtual_size.height < len(window) or not all(
            widget is not None and widget.is_mounted for widget in window
        ):
            if waits > 0:
                self._filling = True
                self.call_after_refresh(self._fill_viewport, pages, waits - 1)
            return
        anchor = window[0]
        following = self._autoscroll and self.window_is_latest
        new_start = max(0, self._start - self._page)
        self._span = max(self._span, self._end - new_start)
        self._set_window(new_start, self._end, fill=False)
        generation = self._window_generation

        def settle() -> None:
            self._filling = False
            if generation != self._window_generation:
                return  # the window moved meanwhile (``jump_to_latest``)
            if following:
                self.scroll_end(animate=False, immediate=True)
            elif anchor is not None and anchor.is_attached:
                self.scroll_to_widget(anchor, top=True, animate=False, immediate=True)
            self._schedule_fill(pages - 1)

        self._filling = True
        self.call_after_refresh(settle)

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
        elif mounted < self._span + self._overscan:
            self._end += 1
            self._mount_missing()
        elif self._autoscroll:
            # Following the end at the cap: drop the oldest rows beyond the window.
            self._set_window(len(self._rows) - self._span, len(self._rows))
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
        self._add_local_row(factory)
        self.scroll_if_at_bottom()

    def _add_local_row(self, factory: Callable[[], Widget]) -> None:
        self._local_ids += 1
        self._add_row(_Row(key=f"local-{self._local_ids}", node=None, factory=factory))

    def add_node(self, node) -> None:
        """Append a row for a model node (used to build a view from existing state)."""
        self._append_node(node)

    def load(self, builder: TranscriptBuilder, locals: Sequence[LocalEntry] = ()) -> None:
        """Fill an empty root view from the builder's current state (084 D6).

        Rows come from the root nodes in order (the preamble, then each turn's user
        message and items), so replaced streams and removed rows are already resolved.
        Each local entry goes after the node it names as its anchor; entries whose
        anchor is gone go at the end, in the order they were added. The latest window
        is mounted once.

        Args:
            builder: The session's transcript model.
            locals: Client-only entries, oldest first.
        """
        waiting: dict[str | None, list[LocalEntry]] = {}
        for entry in locals:
            waiting.setdefault(entry.anchor, []).append(entry)
        placed: set[int] = set()

        def place(anchor: str | None) -> None:
            for entry in waiting.pop(anchor, ()):
                placed.add(id(entry))
                self._add_local_row(local_row(entry))

        was_suspended = self._suspended
        self._suspended = True
        place(None)
        for node in builder.preamble:
            self._add_row(_Row(node.id, node))
            place(node.id)
        for turn in builder.turns:
            user = turn.user
            if user is not None and user.content:
                self._add_row(_Row(user.id, user))
            place(turn.id)
            if user is not None:
                place(user.id)
            for item in turn.items:
                self._add_row(_Row(item.id, item))
                place(item.id)
        for entry in locals:
            if id(entry) not in placed:
                self._add_local_row(local_row(entry))
        if not was_suspended:
            self.resume()

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
        self._wanted_page = None
        await self.remove_children()

    def finish_streams(self) -> None:
        """Stop the streaming indicator on any mounted stream rows."""
        for row in self._rows[self._start : self._end]:
            if isinstance(row.widget, StreamingMessage):
                row.widget.finish()


def local_row(entry: LocalEntry) -> Callable[[], Widget]:
    """The row factory for one client-only timeline entry."""
    if isinstance(entry, TurnStatusEntry):
        return lambda: TurnStatus(
            entry.duration_s,
            entry.input_tokens,
            entry.cache_read,
            entry.cache_write,
            entry.output_tokens,
            entry.cost,
            entry.credits,
        )
    if isinstance(entry, ClientErrorEntry):
        return lambda: ClientErrorMessage(entry.message)
    return lambda: ShellOutput(entry.command, entry.output, entry.exit_code)


class Conversation(TranscriptView):
    """The main session transcript plus client-only rows."""

    def add_entry(self, entry: LocalEntry) -> None:
        """Append the row for a client-only timeline entry."""
        self.add_local(local_row(entry))

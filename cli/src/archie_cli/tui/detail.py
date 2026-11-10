"""Popup screens for inspecting the transcript: tool calls and subagents.

Each popup is a ``ModalScreen`` on the app's screen stack, so Escape closes only the
top one and the screen below is still there. A breadcrumb header names every level
open, from the session down to the current popup.

- ``ToolDetailScreen`` shows one tool call's input and result, syntax-highlighted.
- ``SubagentScreen`` shows one subagent's transcript in a ``TranscriptView``; its tool
  rows and nested subagent rows open further popups.

Both read live model nodes, so they update when the app applies new events.
"""

from __future__ import annotations

from archie_shared.transcript import (
    DetailSection,
    SubagentNode,
    ToolNode,
    TranscriptBuilder,
    subagent_status_text,
    tool_crumb,
    tool_detail_sections,
    tool_headline,
)
from rich.syntax import Syntax
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from archie_cli.tui import theme
from archie_cli.tui.conversation import PromptMessage
from archie_cli.tui.transcript_view import TranscriptView, _markup, _status_glyph

SYNTAX_THEME = "monokai"
_SEPARATOR = " › "


SESSION_STYLE = f"bold {theme.ERROR}"
HEADING_STYLE = f"bold {theme.SECONDARY}"  # the theme's bright magenta


def render_crumbs(crumbs: list[str]) -> Text:
    """Breadcrumb line for the levels above a popup: the session, then any parents.

    The popup's own level is left out because the status line under it names it.
    """
    text = Text()
    for n, crumb in enumerate(crumbs[:-1] or crumbs):
        if n:
            text.append(_SEPARATOR, style=theme.MUTED)
        text.append(crumb, style=SESSION_STYLE if n == 0 else "bold")
    text.no_wrap = True
    text.overflow = "ellipsis"
    return text


def _lexer_for(section: DetailSection) -> str | None:
    if section.language:
        return section.language
    if section.filename:
        lexer = Syntax.guess_lexer(section.filename, code=section.text)
        return None if lexer in {"default", "text"} else lexer
    return None


def render_section(section: DetailSection) -> Text:
    """One input/result block: a title, then highlighted (or plain) text.

    The block is a single ``Text`` so Textual can select and copy it; a ``Syntax``
    or ``Group`` renderable gives the screen no text offsets to select by.
    """
    text = Text()
    text.append(section.title.upper(), style=HEADING_STYLE)
    text.append("\n")
    lexer = _lexer_for(section)
    body = section.text.rstrip("\n")
    if lexer:
        syntax = Syntax(body, lexer, theme=SYNTAX_THEME, background_color=theme.SURFACE)
        highlighted = syntax.highlight(body)
        highlighted.rstrip()  # ``highlight`` adds a trailing newline
        text.append_text(highlighted)
    else:
        text.append(body)
    if section.omitted:
        text.append(f"\n… {section.omitted:,} more characters", style="dim italic")
    text.append("\n")
    return text


class DetailScreen(ModalScreen[None]):
    """A popup with a breadcrumb header. Escape closes it and nothing else."""

    BINDINGS = [Binding("escape", "dismiss", "Back", show=True)]

    DEFAULT_CSS = """
    DetailScreen {
        align: center middle;
        background: $background 70%;
    }
    DetailScreen > Vertical {
        width: 92%;
        height: 88%;
        background: $surface;
        border: round $primary;
    }
    DetailScreen .detail-crumbs {
        height: 1;
        padding: 0 1;
        margin-bottom: 1;
    }
    DetailScreen .detail-status {
        height: auto;
        padding: 0 1;
    }
    DetailScreen .detail-body {
        height: 1fr;
        padding: 0 1;
    }
    """

    def __init__(self, crumbs: list[str]) -> None:
        super().__init__()
        self.crumbs = crumbs

    def compose_body(self) -> ComposeResult:
        yield from ()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(render_crumbs(self.crumbs), classes="detail-crumbs")
            yield Static("", classes="detail-status")
            yield from self.compose_body()

    def on_click(self, event: events.Click) -> None:
        # A click on the dimmed backdrop lands on the screen itself, not the frame.
        if event.widget is self:
            event.stop()
            self.dismiss()

    _ready = False  # set in ``on_mount``; changes before that are drawn from state

    def on_mount(self) -> None:
        self._ready = True
        self.update_state()

    def update_state(self) -> None:
        """Redraw from the live node."""

    def node_changed(self, builder: TranscriptBuilder, changes) -> None:
        """Called for every batch of model changes while this screen is open."""
        self.update_state()


class ToolDetailScreen(DetailScreen):
    """Input and result of one tool call."""

    def __init__(self, tool: ToolNode, crumbs: list[str]) -> None:
        super().__init__([*crumbs, tool_crumb(tool)])
        self.tool = tool
        self._shown: tuple | None = None

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(classes="detail-body"):
            yield Static("", id="tool-sections")

    def _status_line(self) -> Text:
        tool = self.tool
        text = Text()
        text.append_text(_status_glyph(tool.status))
        text.append(" ")
        # The headline names the tool and its duration; the glyph shows the status.
        text.append_text(_markup(tool_headline(tool)))
        return text

    def update_state(self) -> None:
        tool = self.tool
        key = (tool.status, tool.result is None, tool.duration_ms)
        if key == self._shown or not self._ready:
            return  # unchanged, or not mounted yet (``on_mount`` draws it)
        self._shown = key
        self.query_one(".detail-status", Static).update(self._status_line())
        sections = [render_section(s) for s in tool_detail_sections(tool)]
        if tool.result is None:
            sections.append(Text("Running…", style="dim italic"))
        self.query_one("#tool-sections", Static).update(Text("\n").join(sections))


class SubagentScreen(DetailScreen):
    """The transcript of one subagent, live."""

    def __init__(self, sub: SubagentNode, crumbs: list[str]) -> None:
        super().__init__([*crumbs, sub.label])
        self.sub = sub
        self.view = TranscriptView(root_slot=sub.items_id, classes="detail-body")

    def compose_body(self) -> ComposeResult:
        yield self.view

    def on_mount(self) -> None:
        # Build the rows from the node's current items, then follow live changes.
        self.view.suspend()
        if self.sub.prompt.strip():
            prompt = self.sub.prompt
            self.view.add_local(lambda: PromptMessage(prompt))
        for item in self.sub.items:
            self.view.add_node(item)
        self.view.resume()
        self._ready = True
        self.view.scroll_end(animate=False)
        self.update_state()

    def update_state(self) -> None:
        sub = self.sub
        text = Text()
        text.append_text(_status_glyph(sub.status))
        text.append(f" {sub.label}", style="bold")
        text.append(f"  {sub.status} · ${sub.cost_usd:.4f}")
        if sub.context_tokens:
            text.append(f" · {sub.context_tokens:,} context tokens")
        text.append("  ")
        text.append_text(_markup(subagent_status_text(sub)))
        self.query_one(".detail-status", Static).update(text)

    def node_changed(self, builder: TranscriptBuilder, changes) -> None:
        if not self._ready:
            return  # ``on_mount`` builds the rows from the node's latest state
        self.view.apply_changes(builder, changes)
        self.update_state()

"""Detail views that open inside the session pane, in place of the transcript.

The attach TUI opens tool calls and subagents as modal popups. Here they stack in
the session pane instead: the sidebar, the session header and the input stay on
screen, each session keeps its own stack, and Esc goes back one level.

The tool view is laid out as a summary card and one titled panel per section
(command, input, result, ...) instead of a single block of text.
"""

from __future__ import annotations

from archie_shared.transcript import (
    DetailSection,
    SubagentNode,
    ToolNode,
    subagent_status_text,
    tool_detail_sections,
    tool_headline,
)
from rich.syntax import Syntax
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from archie_cli.tui import theme
from archie_cli.tui.conversation import PromptMessage
from archie_cli.tui.detail import SYNTAX_THEME, _lexer_for
from archie_cli.tui.transcript_view import TranscriptView, _markup, _status_glyph

_SEPARATOR = " › "

# Background of the transcript and detail views: a step darker than the surface
# colour around them instead of black, so the pane's edges don't glare. The app adds
# it to the theme as $pane; highlighted code is drawn on it too.
PANE_BG = "#171717"


def crumb_line(crumbs: list[Text]) -> Text:
    """``← esc  Parent › Current``; the last level is the one on screen.

    The session isn't a level: its name is in the header just above.
    """
    text = Text("← esc  ", style=theme.MUTED)
    for n, crumb in enumerate(crumbs):
        if n:
            text.append(_SEPARATOR, style=theme.MUTED)
        if n == len(crumbs) - 1:
            crumb = crumb.copy()
            # The name in the theme colour; any id after it stays muted.
            crumb.stylize(theme.PRIMARY, 0, len(crumb.plain.split("  ")[0]))
        else:
            crumb = Text(crumb.plain, style=theme.MUTED)
        text.append_text(crumb)
    text.no_wrap = True
    text.overflow = "ellipsis"
    return text


def tool_crumb(tool: ToolNode) -> Text:
    """The tool's name and its call id, which is what the logs and the agent use."""
    return Text.assemble((tool.name, "bold"), (f"  {tool.tool_use_id}", theme.MUTED))


def subagent_crumb(sub: SubagentNode) -> Text:
    return Text(sub.label, style="bold")


class PaneView(Vertical):
    """Base for views pushed onto a session's pane stack."""

    def __init__(self, crumbs: list[Text], **kwargs) -> None:
        super().__init__(**kwargs)
        self.crumbs = crumbs

    def compose(self) -> ComposeResult:
        yield Static(crumb_line(self.crumbs), classes="pane-crumbs")
        yield from self.compose_body()

    def compose_body(self) -> ComposeResult:
        yield from ()


def _section_body(section: DetailSection) -> Text:
    body = section.text.rstrip("\n")
    lexer = _lexer_for(section)
    if not lexer:
        return Text(body)
    syntax = Syntax(body, lexer, theme=SYNTAX_THEME, background_color=PANE_BG)
    highlighted = syntax.highlight(body)
    highlighted.rstrip()
    return highlighted


class SectionPanel(Vertical):
    """One input or result block under a full-width header band.

    The band names the section on the left and its language and size on the right;
    it also divides one section from the next.
    """

    def __init__(self, section: DetailSection, *, error: bool = False) -> None:
        super().__init__(classes="section" + (" -error" if error else ""))
        self.section = section

    def compose(self) -> ComposeResult:
        section = self.section
        facts = []
        if section.language:
            facts.append(section.language)
        lines = section.text.count("\n") + 1 if section.text else 0
        facts.append(f"{lines:,} {'line' if lines == 1 else 'lines'}")
        if section.omitted:
            facts.append(f"{section.omitted:,} more characters not shown")
        with Horizontal(classes="section-header"):
            yield Static(Text(section.title.upper()), classes="section-title")
            yield Static(Text(" · ".join(facts)), classes="section-facts")
        yield Static(_section_body(section), classes="section-body")


class ToolDetailView(PaneView):
    """One tool call: a one-line status block, then a section per input and result."""

    def __init__(self, tool: ToolNode, crumbs: list[Text], **kwargs) -> None:
        super().__init__(crumbs, **kwargs)
        self.tool = tool

    def compose_body(self) -> ComposeResult:
        tool = self.tool
        with VerticalScroll(classes="pane-body"):
            yield Static(self._headline(), classes="tool-card")
            for section in tool_detail_sections(tool):
                yield SectionPanel(section, error=section.title == "Error")
            if tool.result is None:
                yield Static(Text("Running…", style="dim italic"), classes="section-body")

    def _headline(self) -> Text:
        """Glyph and the row's summary (name, target, duration, size) on one line."""
        tool = self.tool
        text = Text()
        text.append_text(_status_glyph(tool.status))
        text.append(" ")
        headline = _markup(tool_headline(tool))
        text.append_text(headline.split("\n")[0] if headline.plain else headline)
        return text


class SubagentView(PaneView):
    """One subagent's transcript; its tool and subagent rows open further views."""

    def __init__(self, sub: SubagentNode, crumbs: list[Text], **kwargs) -> None:
        super().__init__(crumbs, **kwargs)
        self.sub = sub
        self.view = TranscriptView(root_slot=sub.items_id, classes="pane-body")

    def compose_body(self) -> ComposeResult:
        yield Static(self._card(), classes="tool-card")
        yield self.view

    def on_mount(self) -> None:
        self.view.suspend()
        if self.sub.prompt.strip():
            prompt = self.sub.prompt
            self.view.add_local(lambda: PromptMessage(prompt))
        for item in self.sub.items:
            self.view.add_node(item)
        self.view.resume()
        self.view.scroll_end(animate=False)

    def _card(self) -> Text:
        sub = self.sub
        text = Text()
        text.append_text(_status_glyph(sub.status))
        text.append(f" {sub.label}", style="bold")
        text.append("\n")
        text.append("subagent", style=f"bold {theme.ANSWER}")
        text.append(f"  {sub.status}", style=theme.SUCCESS if sub.status == "complete" else "")
        text.append(f" · ${sub.cost_usd:.4f}", style=theme.MUTED)
        if sub.context_tokens:
            text.append(f" · {sub.context_tokens:,} context tokens", style=theme.MUTED)
        status = subagent_status_text(sub)
        if status and status != "Completed":
            text.append(" · ", style=theme.MUTED)
            text.append_text(_markup(status))
        return text

"""Detail views that open inside the session pane, in place of the transcript (084 D13).

The pane keeps a stack of these views (``SessionPane.open_tool``/``open_subagent``):
the top one is shown where the transcript was, and Esc pops it. Both views read live
model nodes and get every batch of model changes through ``node_changed``, so a
running tool's view fills in its result and a subagent's transcript keeps growing.

- ``ToolDetailView`` shows one tool call: a headline card, then one section per input
  and result under a full-width band (title left; language, lines and anything cut
  right), syntax-highlighted.
- ``SubagentView`` shows one subagent: a status card and its transcript in a
  ``TranscriptView``; its tool and subagent rows open deeper views.

A crumb line over each view names the levels open, ``← esc  general #0 › Shell ls``,
with the current level's name in the theme colour and its call ID muted. The session
isn't a level: its name is in the pane's header.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from archie_shared.transcript import (
    Change,
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
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from archie_cli.tui import theme
from archie_cli.tui.animation import Animated
from archie_cli.tui.conversation import PromptMessage
from archie_cli.tui.transcript_view import TranscriptView, safe_markup, status_glyph

SYNTAX_THEME = "monokai"
_SEPARATOR = " › "


@dataclass(frozen=True)
class Crumb:
    """One level of the crumb line.

    Attributes:
        name: What the level shows (``Shell ls -la``, ``general #0``).
        ident: The tool call's ID, shown muted after the name (``""`` for none).
    """

    name: str
    ident: str = ""


def crumb_line(crumbs: Sequence[Crumb]) -> Text:
    """``← esc  Parent › Current  id``: every level open, the last one on screen.

    Levels above the current one are muted; the current level's name is in the
    primary colour and its ID muted.
    """
    text = Text("← esc  ", style=theme.MUTED)
    last = len(crumbs) - 1
    for n, crumb in enumerate(crumbs):
        if n:
            text.append(_SEPARATOR, style=theme.MUTED)
        if n == last:
            text.append(crumb.name, style=f"bold {theme.PRIMARY}")
            if crumb.ident:
                text.append(f"  {crumb.ident}", style=theme.MUTED)
        else:
            text.append(crumb.name, style=theme.MUTED)
    text.no_wrap = True
    text.overflow = "ellipsis"
    return text


def lexer_for(section: DetailSection) -> str | None:
    """The lexer that highlights ``section``: its language, else one guessed from the
    file name, else None (plain text)."""
    if section.language:
        return section.language
    if section.filename:
        lexer = Syntax.guess_lexer(section.filename, code=section.text)
        return None if lexer in {"default", "text"} else lexer
    return None


def section_body(section: DetailSection) -> Text:
    """A section's text, highlighted on the pane's colour when it has a lexer.

    The body is a single ``Text`` so Textual can select and copy it; a ``Syntax``
    renderable gives the screen no text offsets to select by. Text cut by the
    preview limit ends with a dim count of what was left out.
    """
    body = section.text.rstrip("\n")
    lexer = lexer_for(section)
    if lexer:
        syntax = Syntax(body, lexer, theme=SYNTAX_THEME, background_color=theme.PANE)
        text = syntax.highlight(body)
        text.rstrip()  # ``highlight`` adds a trailing newline
    else:
        text = Text(body)
    if section.omitted:
        text.append(f"\n… {section.omitted:,} more characters", style="dim italic")
    return text


def section_facts(section: DetailSection) -> str:
    """The right side of a section's band: ``python · 12 lines · 1,000 more chars``."""
    facts = []
    lexer = lexer_for(section)
    if lexer:
        facts.append(lexer)
    body = section.text.rstrip("\n")
    lines = body.count("\n") + 1 if body else 0
    facts.append(f"{lines:,} {'line' if lines == 1 else 'lines'}")
    if section.omitted:
        facts.append(f"{section.omitted:,} more chars")
    return " · ".join(facts)


def is_error_section(section: DetailSection) -> bool:
    """True for a failed call's result, which gets a red band."""
    return section.title == "Error"


class SectionPanel(Vertical):
    """One input or result block under a full-width band naming it."""

    DEFAULT_CSS = """
    SectionPanel {
        height: auto;
    }
    SectionPanel > .section-band {
        height: 1;
        padding: 0 2;
    }
    SectionPanel .section-title {
        width: 1fr;
    }
    SectionPanel .section-facts {
        width: auto;
    }
    SectionPanel > .section-body {
        height: auto;
        padding: 1 2;
    }
    """

    def __init__(self, section: DetailSection) -> None:
        super().__init__()
        self.section: DetailSection | None = None
        self.title = Static("", classes="section-title")
        self.facts = Static("", classes="section-facts")
        self.body = Static("", classes="section-body")
        self.show(section)

    def compose(self) -> ComposeResult:
        with Horizontal(classes="section-band"):
            yield self.title
            yield self.facts
        yield self.body

    def show(self, section: DetailSection) -> None:
        """Draw ``section`` (nothing to do when it is the one shown)."""
        if section == self.section:
            return
        self.section = section
        self.set_class(is_error_section(section), "-error")
        self.title.update(Text(section.title.upper()))
        self.facts.update(Text(section_facts(section)))
        self.body.update(section_body(section))

    def get_copy_text(self) -> str:
        """The section's text, for copying."""
        return self.section.text if self.section is not None else ""


class ToolCard(Animated):
    """The tool view's headline: status glyph and the call's one-line summary."""

    DEFAULT_CSS = """
    ToolCard {
        height: auto;
        padding: 1 2;
        margin: 0 0 1 0;
    }
    """

    def __init__(self, tool: ToolNode) -> None:
        super().__init__(classes="detail-card")
        self.tool = tool

    @property
    def animating(self) -> bool:
        """True while the call runs (a waiting question shows no spinner)."""
        return self.tool.result is None and self.tool.name != "question"

    def render(self) -> Text:
        tool = self.tool
        text = Text.assemble(status_glyph(tool.status, self.frame), " ")
        headline = safe_markup(tool_headline(tool))
        text.append_text(headline.split("\n")[0])
        text.no_wrap = True
        text.overflow = "ellipsis"
        return text

    def refresh_node(self) -> None:
        """Redraw from the node and start or stop the spinner."""
        self.refresh(layout=True)
        self.sync_animation()


class SubagentCard(Animated):
    """The subagent view's headline: glyph and label, then status, cost and activity."""

    DEFAULT_CSS = """
    SubagentCard {
        height: auto;
        padding: 1 2;
    }
    """

    def __init__(self, sub: SubagentNode) -> None:
        super().__init__(classes="detail-card")
        self.sub = sub

    @property
    def animating(self) -> bool:
        """True while the subagent runs."""
        return self.sub.status == "running"

    def render(self) -> Text:
        sub = self.sub
        text = Text.assemble(status_glyph(sub.status, self.frame), (f" {sub.label}", "bold"))
        text.append("\n")
        text.append("subagent", style=f"bold {theme.ANSWER}")
        text.append(f"  {sub.status}", style=theme.SUCCESS if sub.status == "complete" else "")
        text.append(f" · ${sub.cost_usd:.4f}", style=theme.MUTED)
        if sub.context_tokens:
            text.append(f" · {sub.context_tokens:,} context tokens", style=theme.MUTED)
        status = subagent_status_text(sub)
        if status and status != "Completed":
            text.append(" · ", style=theme.MUTED)
            # An error is the agent's raw text; activities are markup.
            if sub.status == "error":
                text.append(status, style=theme.ERROR)
            else:
                text.append_text(safe_markup(status))
        text.no_wrap = True
        text.overflow = "ellipsis"
        return text

    def refresh_node(self) -> None:
        """Redraw from the node and start or stop the spinner."""
        self.refresh()
        self.sync_animation()


class DetailView(Vertical):
    """Base of the views on a session pane's stack: the crumb line, then a body.

    Attributes:
        crumbs: Every level open, this view's last.
    """

    DEFAULT_CSS = """
    DetailView {
        width: 1fr;
        height: 1fr;
    }
    DetailView > .detail-crumbs {
        height: 1;
        padding: 0 2;
        margin: 1 0;
    }
    DetailView .detail-body {
        height: 1fr;
        padding: 0;
    }
    """

    def __init__(self, crumbs: Sequence[Crumb]) -> None:
        super().__init__()
        self.crumbs = list(crumbs)
        self._ready = False  # set in ``on_mount``; changes before that are drawn from state

    def compose(self) -> ComposeResult:
        yield Static(crumb_line(self.crumbs), classes="detail-crumbs")
        yield from self.compose_body()

    def compose_body(self) -> ComposeResult:
        """The widgets under the crumb line."""
        yield from ()

    @property
    def body(self) -> Widget:
        """The scrolling part, which takes focus."""
        raise NotImplementedError

    def on_mount(self) -> None:
        self._ready = True
        self.draw()

    def draw(self) -> None:
        """Draw from the live node."""

    def node_changed(self, builder: TranscriptBuilder, changes: list[Change]) -> None:
        """Follow one batch of model changes (every batch, while the view is open)."""
        if self._ready:
            self.draw()


class ToolDetailView(DetailView):
    """Input and result of one tool call, redrawn when the call changes.

    Attributes:
        tool: The live node.
    """

    DEFAULT_CSS = """
    ToolDetailView .detail-sections {
        height: auto;
    }
    ToolDetailView .detail-running {
        height: auto;
        padding: 1 2;
    }
    """

    def __init__(self, tool: ToolNode, crumbs: Sequence[Crumb] = ()) -> None:
        """Create the view.

        Args:
            tool: The call to show.
            crumbs: The levels above this one.
        """
        super().__init__([*crumbs, Crumb(tool_crumb(tool), tool.tool_use_id)])
        self.tool = tool
        self.card = ToolCard(tool)
        self.sections = Vertical(classes="detail-sections")
        self.running = Static(Text("Running…", style="dim italic"), classes="detail-running")
        self._scroll = VerticalScroll(classes="detail-body")
        self._shown: tuple | None = None

    def compose_body(self) -> ComposeResult:
        with self._scroll:
            yield self.card
            yield self.sections
            yield self.running

    @property
    def body(self) -> Widget:
        """The scrolling card and sections."""
        return self._scroll

    @property
    def panels(self) -> list[SectionPanel]:
        """The section panels shown, in order."""
        return [child for child in self.sections.children if isinstance(child, SectionPanel)]

    def draw(self) -> None:
        """Redraw the card and sections when the call's status or result changed."""
        tool = self.tool
        key = (tool.status, tool.result, tool.duration_ms, tool.result_lines, tool.result_bytes)
        if key == self._shown:
            return
        self._shown = key
        self.card.refresh_node()
        sections = tool_detail_sections(tool)
        panels = self.panels
        for panel, section in zip(panels, sections, strict=False):
            panel.show(section)
        for panel in panels[len(sections) :]:
            panel.remove()
        new = [SectionPanel(section) for section in sections[len(panels) :]]
        if new:
            self.sections.mount(*new)
        self.running.display = tool.result is None

    def get_copy_text(self) -> str:
        """Every section as ``TITLE`` and text, for copying the whole call."""
        return "\n\n".join(
            f"{section.title.upper()}\n{section.text.rstrip()}"
            for section in tool_detail_sections(self.tool)
        )


class SubagentView(DetailView):
    """One subagent's live transcript under its status card.

    Attributes:
        sub: The live node.
        view: The subagent's transcript.
    """

    def __init__(self, sub: SubagentNode, crumbs: Sequence[Crumb] = ()) -> None:
        """Create the view.

        Args:
            sub: The subagent to show.
            crumbs: The levels above this one.
        """
        super().__init__([*crumbs, Crumb(sub.label)])
        self.sub = sub
        self.card = SubagentCard(sub)
        self.view = TranscriptView(root_slot=sub.items_id, classes="detail-body")

    def compose_body(self) -> ComposeResult:
        yield self.card
        yield self.view

    @property
    def body(self) -> Widget:
        """The subagent's transcript."""
        return self.view

    @property
    def child_key(self) -> tuple[str, int]:
        """``(scope, index)`` of the subagent, the target of ``ctrl+x``."""
        return (self.sub.scope, self.sub.index)

    def on_mount(self) -> None:
        # Build the rows from the node's current items, then follow live changes.
        view = self.view
        view.suspend()
        if self.sub.prompt.strip():
            prompt = self.sub.prompt
            view.add_local(lambda: PromptMessage(prompt))
        for item in self.sub.items:
            view.add_node(item)
        view.resume()
        super().on_mount()

    def draw(self) -> None:
        """Redraw the status card."""
        self.card.refresh_node()

    def node_changed(self, builder: TranscriptBuilder, changes: list[Change]) -> None:
        """Apply the changes to the transcript and redraw the card."""
        if not self._ready:
            return  # ``on_mount`` builds the rows from the node's latest state
        self.view.apply_changes(builder, changes)
        self.draw()

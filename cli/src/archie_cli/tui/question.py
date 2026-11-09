"""Pending-question panel, docked above the status bar while the agent waits for an answer.

The panel only displays the question and the current selection. Keys reach it through
:class:`~archie_cli.tui.input.MessageInput`, which keeps focus so typing a free-text answer
works without switching focus.
"""

from __future__ import annotations

from archie_shared.events import PendingQuestion
from rich.markup import escape
from textual.widget import Widget

from archie_cli.tui import theme

HINT = "↑↓ select · Enter choose · 1-4 pick · type to answer · Esc dismiss"


class QuestionPanel(Widget):
    """Shows one :class:`PendingQuestion` with numbered options."""

    DEFAULT_CSS = """
    QuestionPanel {
        height: auto;
        margin: 0;
        padding: 0 1;
        display: none;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.question: PendingQuestion | None = None
        self.selected = 0

    def show(self, question: PendingQuestion) -> None:
        """Display ``question``; keep the selection when it is the same question."""
        if self.question is None or self.question.id != question.id:
            self.selected = question.recommended if question.recommended is not None else 0
        self.question = question
        self.display = True
        self.refresh(layout=True)

    def hide(self) -> None:
        """Remove the question from view."""
        self.question = None
        self.display = False

    def move(self, delta: int) -> None:
        """Move the selection by ``delta``, wrapping around the options."""
        if self.question is None:
            return
        self.selected = (self.selected + delta) % len(self.question.options)
        self.refresh()

    def render(self) -> str:
        question = self.question
        if question is None:
            return ""
        lines = [f"[bold {theme.PRIMARY}]?[/] [bold]{escape(question.question)}[/]"]
        for index, option in enumerate(question.options):
            marker = f"[{theme.PRIMARY}]›[/]" if index == self.selected else " "
            label = escape(option.label)
            label = f"[bold]{label}[/]" if index == self.selected else label
            badge = f" [{theme.SUCCESS}](recommended)[/]" if index == question.recommended else ""
            description = (
                f" [{theme.MUTED}]— {escape(option.description)}[/]" if option.description else ""
            )
            lines.append(f"{marker} {index + 1}. {label}{badge}{description}")
        lines.append(f"[{theme.MUTED}]{HINT}[/]")
        return "\n".join(lines)

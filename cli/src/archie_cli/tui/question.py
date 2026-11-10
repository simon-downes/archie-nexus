"""Pending-question panel, docked above the status bar while the agent waits for an answer.

The panel only displays the question and the current selection. Keys reach it through
:class:`~archie_cli.tui.input.MessageInput`, which keeps focus so typing a free-text answer
works without switching focus.
"""

from __future__ import annotations

from archie_shared.events import PendingQuestion
from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from archie_cli.tui import theme

HINT = "↑↓ select · Enter choose · 1-4 pick · type to answer · Esc dismiss"
MARKER = "❯"


class QuestionPanel(Vertical):
    """Shows one :class:`PendingQuestion` with numbered options.

    Text wraps at the panel width, capped at 120 columns. Descriptions are indented under
    their option's label.
    """

    DEFAULT_CSS = """
    QuestionPanel {
        height: auto;
        margin: 0 0 1 0;
        padding: 1 2 0 2;
        display: none;
    }
    QuestionPanel > Static {
        height: auto;
        max-width: 120;
    }
    QuestionPanel > .question-text,
    QuestionPanel > .option-description,
    QuestionPanel > .option-last {
        margin: 0 0 1 0;
    }
    QuestionPanel > .option-description {
        padding: 0 0 0 5;
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
        self.refresh(recompose=True, layout=True)

    def hide(self) -> None:
        """Remove the question from view."""
        self.question = None
        self.display = False
        self.refresh(recompose=True)

    def move(self, delta: int) -> None:
        """Move the selection by ``delta``, wrapping around the options."""
        if self.question is None:
            return
        self.selected = (self.selected + delta) % len(self.question.options)
        self.refresh(recompose=True)

    def compose(self) -> ComposeResult:
        question = self.question
        if question is None:
            return
        header = Text.assemble(
            ("? Question", Style(color=theme.PRIMARY, bold=True)),
            (" - ", theme.MUTED),
            question.question,
        )
        yield Static(header, classes="question-text")
        for index, option in enumerate(question.options):
            if option.description:
                yield Static(self.option_line(index), classes="option-label")
                yield Static(Text(option.description), classes="option-description")
            else:
                # Without a description, the label carries the gap before the next option.
                yield Static(self.option_line(index), classes="option-label option-last")
        yield Static(Text(HINT, style=theme.MUTED), classes="hint")

    def option_line(self, index: int) -> Text:
        """The numbered label row for option ``index``, with marker and badge."""
        assert self.question is not None
        selected = index == self.selected
        line = Text(f"{MARKER} " if selected else "  ", style=theme.SUCCESS)
        label = f"{index + 1}. {self.question.options[index].label}"
        line.append(label, style=Style(color=theme.ANSWER, bold=selected))
        if index == self.question.recommended:
            line.append(" (recommended)", style=theme.SUCCESS)
        return line

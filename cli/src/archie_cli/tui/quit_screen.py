"""Confirmation screen shown before leaving an attached session."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label


class QuitScreen(ModalScreen[bool]):
    """Ask whether quitting should also terminate the attached session."""

    DEFAULT_CSS = """
    QuitScreen {
        align: center middle;
        background: $background 0%;
    }
    QuitScreen > Vertical {
        width: 56;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: round $primary;
    }
    QuitScreen .quit-question {
        margin-bottom: 1;
    }
    QuitScreen Horizontal {
        height: auto;
        align: right middle;
    }
    QuitScreen Button {
        margin-left: 1;
    }
    QuitScreen Button:focus {
        color: $button-color-foreground;
        background: $primary;
        border-top: tall $primary-lighten-3;
        border-bottom: tall $primary-darken-3;
    }
    """

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(
                "Detach from Archie or terminate the session?\n\n"
                "Choose Detach to leave the session running. Press Esc to go back.",
                classes="quit-question",
            ),
            Horizontal(
                Button("Detach", id="keep-session"),
                Button("Terminate", id="terminate-session"),
            ),
        )

    def on_mount(self) -> None:
        """Make the safe, non-destructive choice the default."""
        self.query_one("#keep-session", Button).focus()

    def on_key(self, event) -> None:
        """Close the prompt without quitting when Esc is pressed."""
        if event.key == "escape":
            event.stop()
            self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "terminate-session")

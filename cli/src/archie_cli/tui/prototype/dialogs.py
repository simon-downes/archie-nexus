"""Dialogs for the TUI prototype, on one shared frame.

Every dialog is a ``Dialog``: a frame with its title in the top border, the key
hint in the bottom border, and right-aligned buttons. Escape or a click outside
the frame cancels.

A dialog has a scope. ``window`` dialogs (new session, quit, keys) dim and centre
over the whole window. ``pane`` dialogs act on one session (stop, rename): they
dim and centre over the session pane only, so the sidebar stays in view and keeps
showing the other sessions' states.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static, TextArea

from archie_cli.tui import theme
from archie_cli.tui.models_provider import model_entries

# Matches #sidebar's width in prototype.tcss: pane dialogs leave this much uncovered.
SIDEBAR_WIDTH = 48


class Dialog[T](ModalScreen[T | None]):
    """Base frame for prototype dialogs."""

    DEFAULT_CSS = """
    Dialog {
        align: center middle;
        background: $background 60%;
    }
    Dialog #dialog-frame {
        height: auto;
        max-height: 90%;
        padding: 1 2 0 2;
        background: $surface;
        border: round $primary;
        border-title-color: $text;
        border-title-style: bold;
        border-subtitle-color: $muted;
    }
    Dialog .dialog-body {
        height: auto;
        margin-bottom: 1;
    }
    Dialog .dialog-field {
        color: $muted;
        margin-top: 1;
    }
    Dialog .dialog-buttons {
        height: auto;
        align: right middle;
        margin-bottom: 1;
    }
    Dialog Button {
        margin-left: 2;
        min-width: 12;
    }
    Dialog Input, Dialog TextArea {
        border: round $surface-lighten-2;
        background: $surface;
    }
    Dialog Input:focus, Dialog TextArea:focus {
        border: round $primary;
    }
    Dialog TextArea {
        height: 6;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    dialog_title: str = ""
    hint: str = "esc cancel"
    width: int = 64
    scope: Literal["window", "pane"] = "window"

    def compose(self) -> ComposeResult:
        frame = Vertical(id="dialog-frame")
        frame.border_title = Text(f" {self.dialog_title} ")
        frame.border_subtitle = Text(f" {self.hint} ")
        frame.styles.width = self.width
        if self.scope == "pane":
            # Centred over the session pane: the left margin shifts the centre right by
            # half the sidebar. Nothing else may cover the screen, or Textual stops
            # drawing the (live) screen below.
            self.add_class("-pane")
            frame.styles.margin = (0, 0, 0, SIDEBAR_WIDTH)
        with frame:
            yield from self.compose_body()

    def compose_body(self) -> ComposeResult:
        yield from ()

    def _session_pane(self):
        try:
            return self.app.screen_stack[0].query_one("#main")
        except NoMatches:
            return None

    def on_mount(self) -> None:
        pane = self._session_pane() if self.scope == "pane" else None
        if pane is not None:
            pane.add_class("-dimmed")

    def on_unmount(self) -> None:
        pane = self._session_pane() if self.scope == "pane" else None
        if pane is not None:
            pane.remove_class("-dimmed")

    def on_click(self, event: events.Click) -> None:
        if event.widget is self:  # outside the frame
            event.stop()
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


def _buttons(*buttons: Button) -> Horizontal:
    return Horizontal(*buttons, classes="dialog-buttons")


class ConfirmDialog(Dialog[bool]):
    """A yes/no question. ``destructive`` colours the confirm button and focuses Cancel."""

    def __init__(
        self,
        title: str,
        message: Text | str,
        confirm: str,
        *,
        destructive: bool = False,
        scope: Literal["window", "pane"] = "window",
    ) -> None:
        super().__init__()
        self.dialog_title = title
        self.hint = "enter choose · ←→ move · esc cancel"
        self.scope = scope
        self._message = message
        self._confirm = confirm
        self._destructive = destructive

    def compose_body(self) -> ComposeResult:
        yield Static(self._message, classes="dialog-body")
        yield _buttons(
            Button("Cancel", id="cancel", flat=True),
            Button(
                self._confirm,
                id="confirm",
                variant="error" if self._destructive else "primary",
                flat=True,
            ),
        )

    def on_mount(self) -> None:
        # The safe choice is the default for destructive actions.
        self.query_one("#cancel" if self._destructive else "#confirm", Button).focus()

    def on_key(self, event: events.Key) -> None:
        if event.key in ("left", "right"):
            event.stop()
            self.focus_next() if event.key == "right" else self.focus_previous()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class RenameDialog(Dialog[str]):
    """Name a session; an empty name goes back to showing the session id."""

    dialog_title = "Rename session"
    hint = "enter save · esc cancel"
    scope = "pane"

    def __init__(self, name: str, session_id: str) -> None:
        super().__init__()
        self._name = name
        self._session_id = session_id

    def compose_body(self) -> ComposeResult:
        yield Input(value=self._name, placeholder=self._session_id, id="name")
        yield Static(
            Text("Leave empty to show the session id instead.", style=theme.MUTED),
            classes="dialog-body",
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value.strip())


@dataclass
class NewSession:
    workspace: str
    model_key: str
    name: str
    prompt: str
    background: bool


class NewSessionDialog(Dialog[NewSession]):
    """Workspace, model, name and an optional first message (roadmap item 5)."""

    dialog_title = "New session"
    hint = "tab next field · ctrl+enter start · esc cancel"
    width = 72

    BINDINGS = [Binding("ctrl+enter,ctrl+j", "start", "Start", show=False)]

    def __init__(self, workspaces: list[str], workspace: str, model_key: str) -> None:
        super().__init__()
        self._workspaces = workspaces
        self._workspace = workspace
        self._model_key = model_key

    def compose_body(self) -> ComposeResult:
        models = [
            (label.removeprefix("Change Model → "), key)
            for key, label, _help in model_entries(self._model_key)
        ]
        keys = {key for _label, key in models}
        yield Label("Workspace", classes="dialog-field")
        yield Select(
            [(w, w) for w in self._workspaces],
            value=self._workspace,
            allow_blank=False,
            id="workspace",
        )
        yield Label("Model", classes="dialog-field")
        yield Select(
            models,
            value=self._model_key if self._model_key in keys else models[0][1],
            allow_blank=False,
            id="model",
        )
        yield Label("Name (optional)", classes="dialog-field")
        yield Input(placeholder="Defaults to the session id", id="name")
        yield Label("First message (optional)", classes="dialog-field")
        yield TextArea(
            id="prompt",
            placeholder="What should this session work on?",
            show_line_numbers=False,
        )
        yield Checkbox("Start in the background (don't switch to it)", id="background")
        yield Static(
            Text("Prototype: no container is started.", style=f"italic {theme.MUTED}"),
            classes="dialog-body",
        )
        yield _buttons(
            Button("Cancel", id="cancel", flat=True),
            Button("Start session", id="start", variant="primary", flat=True),
        )

    def on_mount(self) -> None:
        self.query_one("#prompt", TextArea).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.query_one("#prompt", TextArea).focus()

    def action_start(self) -> None:
        self.dismiss(
            NewSession(
                workspace=str(self.query_one("#workspace", Select).value),
                model_key=str(self.query_one("#model", Select).value),
                name=self.query_one("#name", Input).value.strip(),
                prompt=self.query_one("#prompt", TextArea).text.strip(),
                background=self.query_one("#background", Checkbox).value,
            )
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start":
            self.action_start()
        else:
            self.dismiss(None)


KEYS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Sessions",
        (
            ("alt+↑ / alt+↓", "previous / next session"),
            ("alt+1 … alt+9", "jump to a session in sidebar order"),
            ("tab / shift+tab", "move between sidebar, transcript and input"),
            ("ctrl+n", "new session"),
        ),
    ),
    (
        "Current session",
        (
            ("enter / shift+enter", "send / new line"),
            ("esc", "back from a tool view, dismiss a question, or interrupt"),
            ("alt+m", "change model"),
            ("ctrl+c", "copy the selection"),
        ),
    ),
    (
        "App",
        (
            ("ctrl+p", "command palette: rename, compact, stop, …"),
            ("f1", "this list"),
            ("ctrl+q", "quit (sessions keep running)"),
        ),
    ),
)


class KeysDialog(Dialog[None]):
    dialog_title = "Keyboard shortcuts"
    hint = "esc close"
    width = 72

    def compose_body(self) -> ComposeResult:
        text = Text()
        for n, (section, rows) in enumerate(KEYS):
            if n:
                text.append("\n")
            text.append(f"{section}\n", style=f"bold {theme.PRIMARY}")
            for key, what in rows:
                text.append(f"  {key:<22}", style=theme.ANSWER)
                text.append(f"{what}\n")
        yield Static(text, classes="dialog-body")

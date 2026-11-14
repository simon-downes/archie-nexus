"""Dialogs on one shared frame (084 D14).

Every dialog is a ``Dialog``: a modal screen with one frame, its title in the top border,
the key hint in the bottom border and right-aligned buttons. Escape or a click outside
the frame cancels (the dialog's result is None).

App dialogs (new session, keys, quit) centre over the whole window. Session dialogs
(rename, stop) are given the session pane's region and centre over it, so the sidebar
stays in view; the app dims the pane while they are open.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from archie_shared.session import SessionBackend, SessionDescriptor
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.geometry import Region
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from archie_cli.tui import theme

NAME_MAX_CHARS = 80
"""Longest session name the orchestrator accepts (its ``_valid_name``)."""

BACKENDS: tuple[tuple[str, SessionBackend], ...] = (("Archie", "native"), ("Kiro", "kiro"))
"""The new-session dialog's backend choices: ``(label, backend)``."""


class Dialog[T](ModalScreen[T | None]):
    """The shared frame: a title, a body, buttons and a hint, over the window or a pane.

    Subclasses set ``dialog_title``, ``hint`` and ``frame_width`` and yield their body
    from ``compose_body``.
    """

    DEFAULT_CSS = """
    Dialog {
        align: center middle;
    }
    Dialog > .dialog-frame {
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
    }
    Dialog .dialog-error {
        height: auto;
        color: $error;
        margin-bottom: 1;
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
    Dialog Input, Dialog Select > SelectCurrent {
        border: round $surface-lighten-2;
        background: $surface;
    }
    Dialog Input:focus, Dialog Select:focus > SelectCurrent {
        border: round $primary;
    }
    Dialog Select {
        margin-bottom: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    dialog_title = ""
    hint = "esc cancel"
    frame_width = 64

    def __init__(self, *, over: Region | None = None) -> None:
        """Create the dialog.

        Args:
            over: The region to centre over (a session pane's); None for the window.
        """
        super().__init__()
        self.over = over

    def compose(self) -> ComposeResult:
        """The frame around the body."""
        frame = Vertical(classes="dialog-frame")
        frame.border_title = Text(f" {self.dialog_title} ")
        frame.border_subtitle = Text(f" {self.hint} ")
        width = self.frame_width
        if self.over is not None:
            # A left margin of the region's offset centres the frame over the region.
            self.add_class("-pane")
            width = max(20, min(width, self.over.width - 4))
            frame.styles.margin = (0, 0, 0, self.over.x)
        frame.styles.width = width
        with frame:
            yield from self.compose_body()

    def compose_body(self) -> ComposeResult:
        """The widgets inside the frame."""
        yield from ()

    def on_click(self, event: events.Click) -> None:
        """A click outside the frame cancels."""
        if event.widget is self:
            event.stop()
            self.dismiss(None)

    def action_cancel(self) -> None:
        """Esc: close with no result."""
        self.dismiss(None)


def _buttons(*buttons: Button) -> Horizontal:
    return Horizontal(*buttons, classes="dialog-buttons")


class ConfirmDialog(Dialog[bool]):
    """A yes/no question; the result is True when confirmed.

    A ``destructive`` dialog colours its confirm button red and focuses Cancel, so
    Enter alone does not destroy anything. ←/→ move between the buttons.
    """

    hint = "enter choose · ←→ move · esc cancel"

    def __init__(
        self,
        title: str,
        message: Text | str,
        confirm: str,
        *,
        destructive: bool = False,
        over: Region | None = None,
    ) -> None:
        """Create the dialog.

        Args:
            title: The frame's title.
            message: What confirming does.
            confirm: The confirm button's label.
            destructive: The action can't be undone.
            over: The region to centre over; None for the window.
        """
        super().__init__(over=over)
        self.dialog_title = title
        self.message = message
        self.confirm_label = confirm
        self.destructive = destructive

    def compose_body(self) -> ComposeResult:
        """The message and Cancel / confirm buttons."""
        yield Static(self.message, classes="dialog-body")
        yield _buttons(
            Button("Cancel", id="cancel", flat=True),
            Button(
                self.confirm_label,
                id="confirm",
                variant="error" if self.destructive else "primary",
                flat=True,
            ),
        )

    def on_mount(self) -> None:
        """Focus the safe choice for destructive actions, else the confirm button."""
        self.query_one("#cancel" if self.destructive else "#confirm", Button).focus()

    def on_key(self, event: events.Key) -> None:
        """←/→ move between the buttons."""
        if event.key in ("left", "right"):
            event.stop()
            if event.key == "right":
                self.focus_next(Button)
            else:
                self.focus_previous(Button)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Close with the choice."""
        event.stop()
        self.dismiss(event.button.id == "confirm")


def stop_dialog(record: SessionDescriptor, *, over: Region | None = None) -> ConfirmDialog:
    """Confirm stopping a session: what it does, with Cancel focused."""
    message = Text.assemble(
        "Stop ",
        (record.name, "bold"),
        "?\n\n",
        (
            "Archie interrupts any running turn and removes the session's container. "
            "The session log is kept; the session leaves the sidebar once you switch away.",
            theme.MUTED,
        ),
    )
    return ConfirmDialog("Stop session", message, "Stop session", destructive=True, over=over)


class QuitDialog(ConfirmDialog):
    """Confirm closing the window; the sessions keep running."""

    def __init__(self, live: int, busy: int = 0) -> None:
        """Create the dialog.

        Args:
            live: Sessions that have not ended.
            busy: Of those, sessions mid-turn or waiting for an answer.
        """
        detail = f"{live} session{'' if live == 1 else 's'} keep{'s' if live == 1 else ''} running"
        detail += " in their containers"
        if busy:
            detail += f", {busy} of them mid-turn or waiting for you"
        detail += ". archie attach opens them again; Stop session ends one for good."
        message = Text.assemble("Close this window?\n\n", (detail, theme.MUTED))
        super().__init__("Quit Archie", message, "Quit")


# --- Rename -------------------------------------------------------------------------------


def name_error(value: str) -> str | None:
    """Why the orchestrator would refuse ``value`` as a name, or None.

    The orchestrator's rules (``api._valid_name``): no control characters anywhere and
    at most 80 characters once trimmed. An empty name is fine here: it resets the name.
    """
    if any(unicodedata.category(char) == "Cc" for char in value):
        return "Names can't contain control characters such as tabs or line breaks."
    length = len(value.strip())
    if length > NAME_MAX_CHARS:
        return f"Names are at most {NAME_MAX_CHARS} characters; this one has {length}."
    return None


@dataclass(frozen=True)
class Rename:
    """The rename dialog's result.

    Attributes:
        name: The new name, trimmed; None to reset it to the session ID (the title
            model may name the session again).
    """

    name: str | None


class RenameDialog(Dialog[Rename]):
    """Name a session; an empty name resets it. Invalid names are shown, not sent."""

    dialog_title = "Rename session"
    hint = "enter save · esc cancel"

    def __init__(self, record: SessionDescriptor, *, over: Region | None = None) -> None:
        """Create the dialog, prefilled with the session's own or model-written name.

        Args:
            record: The session to rename.
            over: The region to centre over; None for the window.
        """
        super().__init__(over=over)
        self.record = record
        named = record.name_source in ("user", "llm")
        self.input = Input(
            value=record.name if named else "", placeholder=record.session_id, id="name"
        )
        self.error = Static("", classes="dialog-error")

    def compose_body(self) -> ComposeResult:
        """The name field, its error line, a note and the buttons."""
        yield self.input
        yield self.error
        yield Static(
            Text("Leave it empty to go back to the session ID.", style=theme.MUTED),
            classes="dialog-body",
        )
        yield _buttons(
            Button("Cancel", id="cancel", flat=True),
            Button("Save", id="save", variant="primary", flat=True),
        )

    def on_mount(self) -> None:
        """Focus the field; hide the error line."""
        self.error.display = False
        self.input.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Editing clears a shown error."""
        event.stop()
        self.error.display = False

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter saves."""
        event.stop()
        self.save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Save or cancel."""
        event.stop()
        if event.button.id == "save":
            self.save()
        else:
            self.dismiss(None)

    def save(self) -> None:
        """Close with the name, or show why it can't be used."""
        value = self.input.value
        error = name_error(value)
        if error is not None:
            self.error.update(error)
            self.error.display = True
            return
        self.dismiss(Rename(value.strip() or None))


# --- New session ----------------------------------------------------------------------


@dataclass(frozen=True)
class NewSession:
    """The new-session dialog's result."""

    workspace: str
    backend: SessionBackend


class NewSessionDialog(Dialog[NewSession]):
    """Pick a workspace and a backend (084 D3).

    Enter (or ctrl+enter) starts the session, except while a list is open or Cancel is
    focused; space or ↓ opens a list.
    """

    dialog_title = "New session"
    hint = "enter start · space choose · tab next · esc cancel"

    BINDINGS = [Binding("enter,ctrl+enter,ctrl+j", "start", "Start", show=False, priority=True)]

    def __init__(self, workspaces: list[str], workspace: str, backend: SessionBackend) -> None:
        """Create the dialog.

        Args:
            workspaces: Workspace names to choose from (not empty).
            workspace: The workspace selected at first.
            backend: The backend selected at first.
        """
        super().__init__()
        self.workspace = Select(
            [(name, name) for name in workspaces],
            value=workspace if workspace in workspaces else workspaces[0],
            allow_blank=False,
            id="workspace",
        )
        self.backend = Select(list(BACKENDS), value=backend, allow_blank=False, id="backend")

    def compose_body(self) -> ComposeResult:
        """The two fields and the buttons."""
        yield Label("Workspace", classes="dialog-field")
        yield self.workspace
        yield Label("Backend", classes="dialog-field")
        yield self.backend
        yield _buttons(
            Button("Cancel", id="cancel", flat=True),
            Button("Start session", id="start", variant="primary", flat=True),
        )

    def on_mount(self) -> None:
        """Focus the workspace field."""
        self.workspace.focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Enter starts unless a list is open (it picks) or Cancel is focused (it cancels)."""
        if action == "start":
            if self.workspace.expanded or self.backend.expanded:
                return False
            focused = self.focused
            if isinstance(focused, Button) and focused.id == "cancel":
                return False
        return True

    def action_start(self) -> None:
        """Close with the chosen workspace and backend."""
        backend = self.backend.value
        self.dismiss(
            NewSession(
                workspace=str(self.workspace.value),
                backend="kiro" if backend == "kiro" else "native",
            )
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Start or cancel."""
        event.stop()
        if event.button.id == "start":
            self.action_start()
        else:
            self.dismiss(None)


# --- Keys -----------------------------------------------------------------------------

KEYS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Sessions",
        (
            ("alt+↑ / alt+↓", "previous / next session"),
            ("alt+1 … alt+9", "show a session by its place in the sidebar"),
            ("ctrl+n", "new session"),
            ("ctrl+f", "filter the sessions (esc clears, enter goes to the list)"),
            ("tab / shift+tab", "move between filter, sessions, transcript and input"),
        ),
    ),
    (
        "Session on screen",
        (
            ("enter / shift+enter", "send / new line"),
            ("esc", "stop a ! command, close a view, dismiss a question, interrupt"),
            ("esc esc", "clear the input"),
            ("alt+m", "change model"),
            ("ctrl+c", "copy the selection or the focused block"),
            ("ctrl+g", "write the message in $EDITOR (saving sends it)"),
            ("ctrl+x", "stop the subagent shown in a subagent view"),
        ),
    ),
    (
        "App",
        (
            ("ctrl+p", "command palette: rename, compact, stop, web interface, …"),
            ("f1", "this list"),
            ("ctrl+q", "quit (sessions keep running)"),
        ),
    ),
)
"""The keyboard shortcuts (084 D23), by section: ``(section, ((keys, what), ...))``."""


class KeysDialog(Dialog[None]):
    """The keyboard shortcuts."""

    dialog_title = "Keyboard shortcuts"
    hint = "esc close"
    frame_width = 82

    def compose_body(self) -> ComposeResult:
        """One section per group of keys."""
        text = Text()
        for n, (section, rows) in enumerate(KEYS):
            if n:
                text.append("\n")
            text.append(f"{section}\n", style=f"bold {theme.PRIMARY}")
            for keys, what in rows:
                text.append(f"  {keys:<22}", style=theme.ANSWER)
                text.append(f"{what}\n")
        text.rstrip()
        yield Static(text, classes="dialog-body")

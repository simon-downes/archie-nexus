"""Sidebar, session header and pane views for the TUI prototype."""

from __future__ import annotations

from archie_shared.kiro import kiro_model_id
from archie_shared.models import load_models
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from archie_cli.tui import theme
from archie_cli.tui.prototype.data import MockSession, age
from archie_cli.tui.status import StatusBar

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# The workspace header band: the theme primary at about 25% over the surface colour.
HEADER_BAND = "#313356"

STATE_LABELS = {
    "working": "working",
    "blocked": "needs an answer",
    "error": "turn failed",
    "idle": "ready",
    "ended": "ended",
}


def state_label(session: MockSession) -> str:
    if session.state == "idle" and session.unseen:
        return "new reply"
    return STATE_LABELS[session.state]


def state_colour(session: MockSession) -> str:
    return {
        "working": theme.PRIMARY,
        "blocked": theme.WARNING,
        "error": theme.ERROR,
        "idle": theme.SUCCESS,
    }.get(session.state, theme.MUTED)


def state_glyph(session: MockSession, frame: int = 0) -> Text:
    """One-cell state marker; working sessions spin, ready ones show a green dot."""
    char = {
        "working": SPINNER[frame % len(SPINNER)],
        "blocked": "?",
        "error": "✕",
        "idle": "●",
    }.get(session.state, "·")
    return Text(char, style=f"bold {state_colour(session)}")


def model_label(model_key: str) -> str:
    kiro = kiro_model_id(model_key)
    if kiro is not None:
        return f"{kiro} (Kiro)"
    model = load_models().get(model_key)
    return model.name if model is not None else (model_key or "—")


class SessionList(OptionList):
    """The sessions sidebar: one line per session, grouped by workspace.

    Workspaces are in name order and sessions oldest first within each, so a session
    keeps its place while its state changes. Each row is the state glyph, the name
    (or session id) and the time since the session last did anything, with a blank
    line between rows. Ended sessions aren't listed (finding them is a session
    browser's job), except the one on screen, so it doesn't vanish under you.

    The highlighted option is the keyboard cursor; the bar on the left marks the
    session the main view shows. Enter or a click posts ``Chosen``.
    """

    class Chosen(Message):
        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._sessions: list[MockSession] = []
        self._current: str | None = None
        self._filter = ""
        self._frame = 0
        self._order: list[str] = []

    @property
    def order(self) -> list[str]:
        """Session ids in display order (for next/previous session)."""
        return list(self._order)

    def show(
        self, sessions: list[MockSession], current: str | None, filter_text: str | None = None
    ) -> None:
        self._sessions = sessions
        self._current = current
        if filter_text is not None:
            self._filter = filter_text.strip().lower()
        self._rebuild()

    def _visible(self) -> list[MockSession]:
        listed = [s for s in self._sessions if s.state != "ended" or s.session_id == self._current]
        if not self._filter:
            return listed
        return [
            s
            for s in listed
            if self._filter in s.display_name.lower()
            or self._filter in s.title.lower()
            or self._filter in s.workspace.lower()
            or self._filter in s.session_id
        ]

    def _width(self) -> int:
        return max(12, (self.scrollable_content_region.width or 35) - 1)

    def _heading(self, label: str, count: int) -> Text:
        """A full-width band: the workspace on the left, its session count on the right."""
        band = Text(f" {label}", style=f"bold {theme.BRIGHT}")
        tail = f"{count} "
        band.pad_right(self._width() + 1 - band.cell_len - len(tail))
        band.append(tail, style=theme.MUTED)
        band.stylize(f"on {HEADER_BAND}")
        return band

    def _rebuild(self) -> None:
        highlighted = self.highlighted_option.id if self.highlighted_option is not None else None
        options: list[Option] = []
        self._order = []
        visible = self._visible()
        for workspace in sorted({s.workspace for s in visible}):
            members = sorted(
                (s for s in visible if s.workspace == workspace), key=lambda s: s.started_key
            )
            if options:
                options.append(_gap())
            options.append(Option(self._heading(workspace, len(members)), disabled=True))
            for session in members:
                options.append(_gap())
                options.append(Option(self._prompt(session), id=session.session_id))
                self._order.append(session.session_id)
        if not options:
            options.append(Option(Text(" No matching sessions", style=theme.MUTED), disabled=True))
        self.set_options(options)
        # Unfocused, the cursor follows the session on screen; focused, it stays put.
        target = highlighted if self.has_focus and highlighted in self._order else self._current
        if target in self._order:
            self.highlighted = self.get_option_index(target)

    def _prompt(self, session: MockSession) -> Text:
        """``▌● name-or-id … 32m``: the age is time since the session last did anything."""
        width = self._width()
        current = session.session_id == self._current
        when = age(session.last_activity)
        row = Text.assemble(
            ("▌", theme.PRIMARY) if current else " ",
            state_glyph(session, self._frame),
            " ",
        )
        name = Text(
            session.name or _short_id(session),
            style="bold" if current or session.unseen else "",
        )
        name.truncate(width - row.cell_len - len(when) - 2, overflow="ellipsis")
        row.append_text(name)
        row.pad_right(width - row.cell_len - len(when))
        row.append(when, style=theme.MUTED)
        return row

    def tick(self) -> None:
        """Advance the working spinners without rebuilding the list."""
        self._frame += 1
        for session in self._visible():
            if session.session_id in self._order and session.state == "working":
                self.replace_option_prompt(session.session_id, self._prompt(session))

    def on_resize(self) -> None:
        if self._sessions:
            self._rebuild()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id:
            self.post_message(self.Chosen(event.option.id))


def _short_id(session: MockSession) -> str:
    """The session id without its workspace prefix, which the group header shows."""
    prefix = f"{session.workspace}-"
    sid = session.session_id
    return sid[len(prefix) :] if sid.startswith(prefix) else sid


def _gap() -> Option:
    """A blank, unselectable row: the spacing between sessions and after headers."""
    return Option(Text(""), disabled=True)


def key_hints(*pairs: tuple[str, str]) -> Text:
    """``f1 Keys │ ^q Quit``, styled like Textual's footer keys."""
    text = Text()
    for n, (key, label) in enumerate(pairs):
        if n:
            text.append(" │ ", style=theme.MUTED)
        text.append(key, style=f"bold {theme.PRIMARY}")
        text.append(f" {label}")
    return text


class SessionStatusBar(StatusBar):
    """The attach TUI's status bar without the version, which the sidebar shows."""

    def _refresh_display(self) -> None:
        super()._refresh_display()
        try:
            right = self.query_one("#status-right", Static)
        except NoMatches:
            return  # not mounted yet
        right.update(Text(f"{self.session_id} " if self.session_id else ""))


class SessionHeader(Horizontal):
    """Above the transcript: state glyph, name and badge."""

    def compose(self) -> ComposeResult:
        yield Static("", id="header-left")
        yield Static("", id="header-right")

    def show(self, session: MockSession | None, frame: int = 0) -> None:
        left = self.query_one("#header-left", Static)
        right = self.query_one("#header-right", Static)
        if session is None:
            left.update(Text("No session selected", style=theme.MUTED))
            right.update("")
            return
        title = Text.assemble(state_glyph(session, frame), " ", (session.display_name, "bold"))
        title.truncate(max(10, (left.size.width or 80) - 2), overflow="ellipsis")
        left.update(title)
        colour = state_colour(session)
        right.update(
            Text(f" {state_label(session).upper()} ", style=f"bold {theme.SURFACE} on {colour}")
        )

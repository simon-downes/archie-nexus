"""The sidebar: banner, profile line, the session list and key hints (084 D15, D30).

The session list is fed only by the sessions feed (D8). Sessions are grouped under a
band per workspace (bands in name order, each with a count) and listed oldest first by
``started_at``, so a session keeps its place while its state changes. Ended sessions
are hidden, except the one on screen. Rows and bands are widgets keyed by session ID
and workspace and updated in place; each lays itself out in ``render()`` from its own
width.

The state glyph and badge helpers here are shared with the session pane's header.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

from archie_shared import __version__
from archie_shared.session import SessionDescriptor
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.events import Click
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from archie_cli.tui import theme
from archie_cli.tui.client import ProfileInfo

# archie-og's start-up banner (archie-og/src/archie/output.py) and its blue gradient.
BANNER = (
    " █████╗ ██████╗  ██████╗██╗  ██╗██╗███████╗",
    "██╔══██╗██╔══██╗██╔════╝██║  ██║██║██╔════╝",
    "███████║██████╔╝██║     ███████║██║█████╗  ",
    "██╔══██║██╔══██╗██║     ██╔══██║██║██╔══╝  ",
    "██║  ██║██║  ██║╚██████╗██║  ██║██║███████╗",
    "╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝╚═╝╚══════╝",
)
BANNER_COLOURS = ("#a8d8ff", "#7bb8f0", "#4e98e0", "#2878d0", "#1060b8", "#0a4a9a")

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
"""Frames of the working spinner, one per animation tick."""

PENDING_FRAMES = "◌◌◌◌○○○○"
"""Frames of the starting/stopping glyph: a slow pulse between two circles."""

ANIMATED_STATUSES = frozenset({"starting", "working", "stopping"})
"""Statuses whose glyph animates (084 D16)."""

_BADGES: dict[str, tuple[str, str]] = {
    "starting": ("STARTING", theme.MUTED),
    "idle": ("READY", theme.SUCCESS),
    "working": ("WORKING", theme.PRIMARY),
    "blocked": ("NEEDS AN ANSWER", theme.WARNING),
    "error": ("TURN FAILED", theme.ERROR),
    "stopping": ("STOPPING", theme.MUTED),
    "stopped": ("ENDED", theme.MUTED),
    "exited": ("ENDED", theme.MUTED),
    "failed": ("FAILED", theme.MUTED),
}


def age(when: str | None, now: datetime | None = None) -> str:
    """Compact time since an ISO 8601 timestamp: ``now``, ``45s``, ``12m``, ``3h``, ``5d``."""
    if not when:
        return "—"
    try:
        moment = datetime.fromisoformat(when)
    except ValueError:
        return "—"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    seconds = int(((now or datetime.now(UTC)) - moment).total_seconds())
    if seconds < 10:
        return "now"
    for size, unit in ((86_400, "d"), (3_600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def state_glyph(record: SessionDescriptor, frame: int = 0) -> Text:
    """The one-cell state marker of a session (084 D30)."""
    status = record.status
    if status == "working":
        return Text(SPINNER[frame % len(SPINNER)], style=f"bold {theme.PRIMARY}")
    if status in ("starting", "stopping"):
        return Text(PENDING_FRAMES[frame % len(PENDING_FRAMES)], style=theme.MUTED)
    if status == "blocked":
        return Text("?", style=f"bold {theme.WARNING}")
    if status == "error":
        return Text("✕", style=f"bold {theme.ERROR}")
    if status == "idle":
        return Text("●", style=f"bold {theme.SUCCESS}")
    if status == "failed":
        return Text("✕", style=theme.MUTED)
    return Text("·", style=theme.MUTED)  # stopped, exited


def state_badge(record: SessionDescriptor) -> tuple[str, str]:
    """``(label, colour)`` of the header badge, e.g. ``("NEEDS AN ANSWER", warning)``."""
    if record.status == "idle" and record.unseen:
        return "NEW REPLY", theme.SUCCESS
    return _BADGES.get(record.status, (record.status.upper(), theme.MUTED))


def where(record: SessionDescriptor) -> Text:
    """Line 2 of a row: the session's repo and branch (``no repo`` until M3's location)."""
    return Text("  no repo", style=theme.MUTED)  # two cells: lines up with a repo name


def key_hints(*pairs: tuple[str, str]) -> Text:
    """``f1 Keys │ ^q Quit``, styled like Textual's footer keys."""
    text = Text()
    for n, (key, label) in enumerate(pairs):
        if n:
            text.append(" │ ", style=theme.MUTED)
        text.append(key, style=f"bold {theme.PRIMARY}")
        text.append(f" {label}")
    return text


def banner() -> Text:
    """archie-og's banner, one blue shade per row."""
    text = Text(no_wrap=True)
    for n, (line, colour) in enumerate(zip(BANNER, BANNER_COLOURS, strict=True)):
        text.append(line + ("\n" if n < len(BANNER) - 1 else ""), style=colour)
    return text


# --- Session list -------------------------------------------------------------------


class SessionRow(Widget):
    """One session: ``▌ ● name … 12m`` over ``▌ ⎇ repo · branch``.

    The ``▌`` bar marks the session on screen. A click posts ``SessionList.Chosen``.
    """

    DEFAULT_CSS = """
    SessionRow {
        width: 1fr;
        height: 2;
        margin: 1 0 0 0;
    }
    SessionRow:hover {
        background: $surface-lighten-1;
    }
    """

    def __init__(self, record: SessionDescriptor) -> None:
        super().__init__()
        self.record = record
        self.current = False
        self.frame = 0

    @property
    def animated(self) -> bool:
        """True while the state glyph animates."""
        return self.record.status in ANIMATED_STATUSES

    def render(self) -> Text:
        width = max(12, self.size.width)
        record = self.record
        bar = ("\u258c", theme.PRIMARY) if self.current else " "
        when = age(record.last_activity_at or record.started_at)
        line = Text.assemble(bar, " ", state_glyph(record, self.frame), " ")
        name = Text(record.name, style="bold" if self.current or record.unseen else "")
        name.truncate(max(1, width - line.cell_len - len(when) - 1), overflow="ellipsis")
        line.append_text(name)
        line.pad_right(max(0, width - line.cell_len - len(when)))
        line.append(when, style=theme.MUTED)
        second = Text.assemble(bar, " ")
        second.append_text(where(record))
        second.truncate(width, overflow="ellipsis")
        text = Text("\n").join([line, second])
        text.no_wrap = True
        return text

    def on_click(self, event: Click) -> None:
        event.stop()
        self.post_message(SessionList.Chosen(self.record.session_id))


class WorkspaceBand(Widget):
    """A full-width band: the workspace on the left, its session count on the right."""

    DEFAULT_CSS = """
    WorkspaceBand {
        width: 1fr;
        height: 1;
        margin: 1 0 0 0;
        background: $band;
    }
    """

    def __init__(self, workspace: str) -> None:
        super().__init__()
        self.workspace = workspace
        self.count = 0

    def render(self) -> Text:
        width = max(4, self.size.width)
        tail = f"{self.count} "
        text = Text(f" {self.workspace}", style=f"bold {theme.BRIGHT}")
        text.truncate(max(1, width - len(tail) - 1), overflow="ellipsis")
        text.pad_right(max(0, width - text.cell_len - len(tail)))
        text.append(tail, style=theme.MUTED)
        text.no_wrap = True
        return text


class SessionList(VerticalScroll):
    """Workspace bands and session rows, updated in place from the feed's records."""

    DEFAULT_CSS = """
    SessionList {
        height: 1fr;
        padding: 0;
    }
    SessionList > .session-list-empty {
        height: auto;
        padding: 1 1;
        color: $muted;
    }
    """

    class Chosen(Message):
        """A session row was clicked."""

        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.rows: dict[str, SessionRow] = {}
        self.bands: dict[str, WorkspaceBand] = {}
        self._order: list[str] = []
        self._shown: list[Widget] = []
        self._empty = Static("No sessions", classes="session-list-empty")

    def compose(self) -> ComposeResult:
        yield self._empty

    @property
    def order(self) -> list[str]:
        """Session IDs in display order (alt+↑/↓, alt+1…9)."""
        return list(self._order)

    def sync(self, records: Mapping[str, SessionDescriptor], current: str | None) -> None:
        """Show ``records`` grouped and ordered (084 D15), marking ``current``.

        Rows and bands that stay are updated in place; new ones are mounted at their
        place and ones no longer listed are removed.
        """
        visible = [r for r in records.values() if not r.ended or r.session_id == current]
        groups: dict[str, list[SessionDescriptor]] = {}
        for record in visible:
            groups.setdefault(record.workspace, []).append(record)

        desired: list[Widget] = []
        order: list[str] = []
        for workspace in sorted(groups):
            members = sorted(groups[workspace], key=lambda r: (r.started_at, r.session_id))
            band = self.bands.get(workspace) or WorkspaceBand(workspace)
            self.bands[workspace] = band
            if band.count != len(members):
                band.count = len(members)
                band.refresh()
            desired.append(band)
            for record in members:
                row = self.rows.get(record.session_id) or SessionRow(record)
                self.rows[record.session_id] = row
                row.record = record
                row.current = record.session_id == current
                row.refresh()
                desired.append(row)
                order.append(record.session_id)

        keep = {id(widget) for widget in desired}
        for widget in self._shown:
            if id(widget) not in keep:
                widget.remove()
        self.rows = {sid: row for sid, row in self.rows.items() if id(row) in keep}
        self.bands = {ws: band for ws, band in self.bands.items() if id(band) in keep}
        self._place(desired)
        self._shown = desired
        self._order = order
        self._empty.display = not desired

    def _place(self, desired: list[Widget]) -> None:
        """Mount new widgets after their predecessor; widgets that stay keep their order."""
        shown = {id(widget) for widget in self._shown}
        previous: Widget | None = self._empty
        for widget in desired:
            if id(widget) not in shown:
                self.mount(widget, after=previous)
            previous = widget

    def tick(self, frame: int) -> bool:
        """Advance the animated glyphs; True if any row animates."""
        animated = False
        for row in self.rows.values():
            if row.animated:
                animated = True
                row.frame = frame
                row.refresh()
        return animated

    @property
    def animated(self) -> bool:
        """True if any listed row animates."""
        return any(row.animated for row in self.rows.values())

    def refresh_rows(self) -> None:
        """Redraw every row (ages)."""
        for row in self.rows.values():
            row.refresh()


# --- Sidebar ------------------------------------------------------------------------


class Sidebar(Vertical):
    """Banner, ``v0.1.0 · N running · local ↗``, the session list and key hints.

    The profile label in the info line is a link to the app's ``open_web`` action.
    """

    DEFAULT_CSS = """
    Sidebar {
        width: 48;
        height: 1fr;
    }
    Sidebar > .sidebar-brand {
        height: auto;
        padding: 1 1 0 2;
    }
    Sidebar > .sidebar-info {
        height: auto;
        padding: 0 1 0 2;
    }
    Sidebar > .sidebar-keys {
        height: 1;
        padding: 0 2;
        margin: 1 0;
    }
    """

    def __init__(self, profile: ProfileInfo, **kwargs) -> None:
        super().__init__(**kwargs)
        self.profile = profile
        self.info = Static(self._info(0), classes="sidebar-info")
        self.sessions = SessionList()

    def compose(self) -> ComposeResult:
        yield Static(banner(), classes="sidebar-brand")
        yield self.info
        yield self.sessions
        yield Static(
            key_hints(("f1", "Keys"), ("^q", "Quit"), ("^p", "Palette")), classes="sidebar-keys"
        )

    def _info(self, running: int) -> Content:
        facts = Content.styled(f"v{__version__} · {running} running · ", theme.MUTED)
        link = Content.from_markup("[@click=app.open_web]$label ↗[/]", label=self.profile.label)
        return facts + link

    def show(self, records: Mapping[str, SessionDescriptor], current: str | None) -> None:
        """Redraw the info line and the session list from the feed's records."""
        self.info.update(self._info(_running(records.values())))
        self.sessions.sync(records, current)


def _running(records: Iterable[SessionDescriptor]) -> int:
    return sum(1 for record in records if not record.ended)

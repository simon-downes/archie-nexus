"""Status bar widget with session metrics (084 D31).

Shows: ⎇ branch │ model │ In: fresh / cache read / cache write  Out: output │ Ctx: N (P%)
│ $cost, and on the right the session ID when the session has a name (the header shows
the ID otherwise). No version: the sidebar shows it.

Token counts are session totals (lifetime, only climb). Updated from
Usage wire events which carry per-request values (accumulated client-side).
"""

from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from archie_cli.tui import theme


class StatusBar(Widget):
    """Two-section status bar: left (metrics) and right (session info)."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        margin: 0 0 1 0;
        padding: 0 1;
    }
    StatusBar #status-left {
        width: 1fr;
        padding: 0;
    }
    StatusBar #status-right {
        width: auto;
        padding: 0;
        text-align: right;
    }
    """

    # Left section — session lifetime totals
    model_name: reactive[str] = reactive("—")
    git_branch: reactive[str] = reactive("—")  # from the session's location (084 D31)
    session_input: reactive[int] = reactive(0)
    session_output: reactive[int] = reactive(0)
    cache_read: reactive[int] = reactive(0)
    cache_write: reactive[int] = reactive(0)
    context_tokens: reactive[int] = reactive(0)
    context_pct: reactive[float] = reactive(0.0)

    # Right section: the session ID, or "" when the header already shows it
    session_id: reactive[str] = reactive("")

    # Model capability flags
    supports_cache: reactive[bool] = reactive(True)
    pricing_label: reactive[str] = reactive("$0.0000")

    def compose(self) -> ComposeResult:
        """Build the status bar with left and right sections."""
        with Horizontal():
            yield Static("", id="status-left")
            yield Static("", id="status-right")

    # --- Watchers: any reactive change triggers a display refresh ---

    def watch_model_name(self) -> None:
        self._refresh_display()

    def watch_git_branch(self) -> None:
        self._refresh_display()

    def watch_session_input(self) -> None:
        self._refresh_display()

    def watch_session_output(self) -> None:
        self._refresh_display()

    def watch_cache_read(self) -> None:
        self._refresh_display()

    def watch_cache_write(self) -> None:
        self._refresh_display()

    def watch_context_tokens(self) -> None:
        self._refresh_display()

    def watch_context_pct(self) -> None:
        self._refresh_display()

    def watch_session_id(self) -> None:
        self._refresh_display()

    def watch_supports_cache(self) -> None:
        self._refresh_display()

    def watch_pricing_label(self) -> None:
        self._refresh_display()

    def _refresh_display(self) -> None:
        """Render the status bar content."""
        try:
            left = self.query_one("#status-left", Static)
            right = self.query_one("#status-right", Static)
        except Exception:  # noqa: BLE001 — widget may not be mounted yet
            return

        # Input format: include cache columns only when caching is supported
        if self.supports_cache:
            in_val = (
                f"{_fmt(self.session_input)} / {_fmt(self.cache_read)} / {_fmt(self.cache_write)}"
            )
        else:
            in_val = f"{_fmt(self.session_input)}"

        # Context usage with color progression on the percentage.
        if self.context_pct > 85:
            pct_val = f"[bold {theme.ERROR}]{self.context_pct:.0f}%[/]"
        elif self.context_pct >= 60:
            pct_val = f"[bold {theme.WARNING}]{self.context_pct:.0f}%[/]"
        else:
            pct_val = f"[{theme.BRIGHT}]{self.context_pct:.0f}%[/]"
        ctx_val = f"{_fmt_context(self.context_tokens)} ({pct_val})"

        left.update(
            Text.from_markup(
                f" [{theme.MUTED}]⎇ {escape(self.git_branch)}[/]"
                f" │ [{theme.PRIMARY}]{self.model_name}[/]"
                f" │ In: [{theme.POSITIVE}]{in_val}[/]"
                f"  Out: [{theme.POSITIVE_BRIGHT}]{self.session_output}[/]"
                f" │ Ctx: {ctx_val}"
                f" │ [{theme.COST}]{self.pricing_label}[/]"
            )
        )
        right.update(Text(f"{self.session_id} " if self.session_id else "", style=theme.MUTED))


def _fmt(n: int) -> str:
    """Format token count with K suffix. e.g. 1500 → "1.5K", 800 → "800"."""
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)


def _fmt_context(n: int) -> str:
    """Format context tokens with the compact lowercase-k display style."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)

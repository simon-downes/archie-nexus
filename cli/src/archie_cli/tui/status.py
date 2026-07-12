"""Status bar widget with session metrics.

Shows: model │ in:fresh/cache_read/cache_write out:output │ ctx:N% │ $cost │ session_id

Token counts are session totals (lifetime, only climb). Updated from
UsageUpdated wire events which carry cumulative totals.
"""

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
        height: 3;
        padding: 1;
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
    git_branch: reactive[str] = reactive("—")
    session_input: reactive[int] = reactive(0)
    session_output: reactive[int] = reactive(0)
    cache_read: reactive[int] = reactive(0)
    cache_write: reactive[int] = reactive(0)
    context_pct: reactive[float] = reactive(0.0)

    # Right section
    session_id: reactive[str] = reactive("—")

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

        # Context percentage with color progression
        if self.context_pct > 85:
            ctx_val = f"[bold {theme.ERROR}]{self.context_pct:.0f}%[/]"
        elif self.context_pct >= 60:
            ctx_val = f"[bold {theme.WARNING}]{self.context_pct:.0f}%[/]"
        else:
            ctx_val = f"[{theme.BRIGHT}]{self.context_pct:.0f}%[/]"

        left.update(
            Text.from_markup(
                f" [{theme.MUTED}]⎇ {self.git_branch}[/]"
                f" │ [{theme.PRIMARY}]{self.model_name}[/]"
                f" │ In: [{theme.POSITIVE}]{in_val}[/]"
                f"  Out: [{theme.POSITIVE_BRIGHT}]{self.session_output}[/]"
                f" │ Ctx: {ctx_val}"
                f" │ [{theme.COST}]{self.pricing_label}[/]"
            )
        )
        right.update(Text.from_markup(f"{self.session_id} "))


def _fmt(n: int) -> str:
    """Format token count with K suffix. e.g. 1500 → "1.5K", 800 → "800"."""
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)

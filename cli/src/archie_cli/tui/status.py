"""Status bar widget with session metrics (084 D31).

Shows: ⎇ branch │ model │ In: fresh / cache read / cache write  Out: output │ Ctx: N (P%)
│ $cost, and on the right the session ID when the session has a name (the header shows
the ID otherwise). No version: the sidebar shows it.

The bar lays itself out from its own width: when everything does not fit, fields are
dropped in order of ``DROP_ORDER`` (the session ID first, then the cache numbers, the
token counts, the branch and the context), and then the model name is shortened. The
cost always stays.

Token counts are session totals (lifetime, only climb). Updated from
Usage wire events which carry per-request values (accumulated client-side).
"""

from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget

from archie_cli.tui import theme

DROP_ORDER = ("session_id", "cache", "tokens", "branch", "context")
"""Fields dropped, first to last, until the bar fits."""

_SEPARATOR = " │ "


class StatusBar(Widget):
    """One line of session metrics, laid out to fit its width."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        margin: 0 0 1 0;
        padding: 0 1;
    }
    """

    # Session lifetime totals; every change redraws the bar.
    model_name: reactive[str] = reactive("—")
    git_branch: reactive[str] = reactive("—")  # from the session's location (084 D31)
    session_input: reactive[int] = reactive(0)
    session_output: reactive[int] = reactive(0)
    cache_read: reactive[int] = reactive(0)
    cache_write: reactive[int] = reactive(0)
    context_tokens: reactive[int] = reactive(0)
    context_pct: reactive[float] = reactive(0.0)

    # Right-hand side: the session ID, or "" when the header already shows it
    session_id: reactive[str] = reactive("")

    # Model capability flags
    supports_cache: reactive[bool] = reactive(True)
    pricing_label: reactive[str] = reactive("$0.0000")

    def render(self) -> Text:
        """The bar for the current width (see the module docstring)."""
        return self.fit(self.size.width)

    def fit(self, width: int) -> Text:
        """The bar fitted to ``width`` cells."""
        dropped: set[str] = set()
        line = self._line(width, dropped, None)
        for field in DROP_ORDER:
            if line.cell_len <= width:
                return line
            dropped.add(field)
            line = self._line(width, dropped, None)
        if line.cell_len > width:
            # Shorten the model name, keeping at least one character and the cost.
            excess = line.cell_len - width
            line = self._line(width, dropped, max(1, len(self.model_name) - excess))
        line.truncate(max(0, width), overflow="ellipsis")
        return line

    def _line(self, width: int, dropped: set[str], model_width: int | None) -> Text:
        parts: list[Text] = []
        if "branch" not in dropped:
            parts.append(Text(f"⎇ {self.git_branch}", style=theme.MUTED))
        model = Text(self.model_name, style=theme.PRIMARY)
        if model_width is not None:
            model.truncate(model_width, overflow="ellipsis")
        parts.append(model)
        if "tokens" not in dropped:
            parts.append(self._tokens_text(with_cache="cache" not in dropped))
        if "context" not in dropped:
            parts.append(self._context_text())
        parts.append(Text(self.pricing_label, style=theme.COST))

        line = Text(" ", no_wrap=True)
        line.append_text(Text(_SEPARATOR).join(parts))
        if self.session_id and "session_id" not in dropped:
            right = Text(f"{self.session_id} ", style=theme.MUTED)
            line.pad_right(max(1, width - line.cell_len - right.cell_len))
            line.append_text(right)
        return line

    def _tokens_text(self, *, with_cache: bool) -> Text:
        if self.supports_cache and with_cache:
            fresh = (
                f"{_fmt(self.session_input)} / {_fmt(self.cache_read)} / {_fmt(self.cache_write)}"
            )
        else:
            fresh = _fmt(self.session_input)
        return Text.assemble(
            "In: ",
            (fresh, theme.POSITIVE),
            "  Out: ",
            (str(self.session_output), theme.POSITIVE_BRIGHT),
        )

    def _context_text(self) -> Text:
        """``Ctx: 40.0k (20%)``, the percentage coloured as it climbs."""
        pct = self.context_pct
        if pct > 85:
            style = f"bold {theme.ERROR}"
        elif pct >= 60:
            style = f"bold {theme.WARNING}"
        else:
            style = theme.BRIGHT
        return Text.assemble(
            f"Ctx: {_fmt_context(self.context_tokens)} (", (f"{pct:.0f}%", style), ")"
        )


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

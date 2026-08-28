"""Textual widgets for native subagent activity."""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from archie_cli.tui import theme


@dataclass
class ChildActivityState:
    """Mutable state shared by inline and detail child views."""

    scope: str
    index: int
    agent: str = "child"
    status: str = "running"
    cost: float = 0.0
    context_tokens: int = 0
    activity: str = "Thinking..."
    error: str = ""
    lines: list[str] = field(default_factory=list)

    def add_line(self, line: str, limit: int = 3) -> None:
        line = "; ".join(part.strip() for part in line.splitlines() if part.strip())
        if line:
            self.lines.append(line[:512])
            del self.lines[:-limit]

    def set_activity(self, activity: str) -> None:
        """Set compact single-line activity and retain it for detail views."""
        activity = "; ".join(part.strip() for part in activity.splitlines() if part.strip())
        self.activity = activity[:512].strip() or "Thinking..."
        self.add_line(self.activity)

    @property
    def icon(self) -> str:
        """Return the root-tool-style status glyph."""
        return "●" if self.status in {"complete", "error", "interrupted"} else "○"


class SubagentActivity(Static):
    """Collapsed rolling activity display for one child."""

    def __init__(self, state: ChildActivityState, *, key: str = "") -> None:
        self.state = state
        super().__init__(id=key or f"child-{state.scope}-{state.index}")

    def compose(self) -> ComposeResult:
        yield Static(self.render_text())

    def render_text(self) -> Text:
        state = self.state
        activity = state.error if state.status == "error" else (
            "Completed" if state.status == "complete" else state.activity
        )
        agent = _fmt_column(f"{state.agent} #{state.index}", _AGENT_COLUMN)
        context = _fmt_column(_fmt_tokens(state.context_tokens), _CONTEXT_COLUMN, align=">")
        cost = _fmt_column(f"${state.cost:.4f}", _COST_COLUMN, align=">")
        prefix = f"  {state.icon} {agent} {context} {cost} - "
        try:
            activity_text = Text.from_markup(activity)
        except Exception:
            activity_text = Text(activity)
        width = max(1, self.size.width or 120)
        available = max(1, width - len(prefix))
        activity_text.truncate(available, overflow="ellipsis")

        icon_colour = (
            theme.SUCCESS
            if state.status == "complete"
            else theme.ERROR
            if state.status in {"error", "interrupted"}
            else theme.PRIMARY
        )
        return Text.assemble(
            ("  ", ""),
            (state.icon, f"bold {icon_colour}"),
            (f" {agent} {context} {cost} - ", ""),
            activity_text,
        )

    def update_state(self) -> None:
        try:
            self.query_one(Static).update(self.render_text())
        except Exception:
            pass


_AGENT_COLUMN = 20
_CONTEXT_COLUMN = 8
_COST_COLUMN = 9


def _fmt_tokens(tokens: int) -> str:
    return f"{tokens / 1000:.1f}k" if tokens >= 1000 else str(tokens)


def _fmt_column(value: str, width: int, *, align: str = "<") -> str:
    """Fit a value into a fixed-width display column."""
    if len(value) > width:
        value = value[: max(1, width - 1)] + "…"
    return f"{value:{align}{width}}"


class SubagentScreen(ModalScreen[None]):
    """Full-screen live detail view for one mutable child state."""

    DEFAULT_CSS = """
    SubagentScreen {
        align: center middle;
        background: $background 90%;
    }
    SubagentScreen > VerticalScroll {
        width: 90%;
        height: 85%;
        padding: 1 2;
        background: $surface;
        border: round $primary;
    }
    """

    def __init__(self, state: ChildActivityState) -> None:
        self.state = state
        super().__init__()

    def compose(self) -> ComposeResult:
        yield VerticalScroll(
            Static("", id="subagent-header"),
            Static("", id="subagent-body"),
        )

    def on_mount(self) -> None:
        self.update_state()

    def update_state(self) -> None:
        try:
            self.query_one("#subagent-header", Static).update(
                f"Subagent {self.state.agent} #{self.state.index} "
                f"[{self.state.status}] ${self.state.cost:.4f}"
            )
            self.query_one("#subagent-body", Static).update(
                "\n".join(self.state.lines) or "starting"
            )
        except Exception:
            pass

    def on_key(self, event) -> None:
        if event.key in {"escape", "q"}:
            self.dismiss(None)

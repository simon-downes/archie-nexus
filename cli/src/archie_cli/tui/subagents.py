"""Textual widgets for native subagent activity."""

from __future__ import annotations

from dataclasses import dataclass, field

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static


@dataclass
class ChildActivityState:
    """Mutable state shared by inline and detail child views."""

    scope: str
    index: int
    agent: str = "child"
    status: str = "running"
    cost: float = 0.0
    lines: list[str] = field(default_factory=list)

    def add_line(self, line: str, limit: int = 3) -> None:
        line = line.strip()
        if line:
            self.lines.append(line)
            del self.lines[:-limit]


class SubagentActivity(Static):
    """Collapsed rolling activity display for one child."""

    def __init__(self, state: ChildActivityState, *, key: str = "") -> None:
        self.state = state
        super().__init__(id=key or f"child-{state.scope}-{state.index}")

    def compose(self) -> ComposeResult:
        yield Static(self.render_text())

    def render_text(self) -> str:
        state = self.state
        body = "\n".join(state.lines[-3:]) or "starting"
        return f"  [{state.status}] {state.agent} #{state.index} ${state.cost:.4f}\n    {body}"

    def update_state(self) -> None:
        try:
            self.query_one(Static).update(self.render_text())
        except Exception:
            pass


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

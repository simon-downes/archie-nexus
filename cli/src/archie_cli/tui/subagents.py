"""Textual widgets for native subagent activity."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static


class SubagentActivity(Static):
    """Collapsed rolling activity display for one child."""

    def __init__(self, agent: str, index: int, lines: list[str], status: str, cost: float, *, key: str = "") -> None:
        self.agent = agent
        self.index = index
        self.lines = list(lines)
        self.status = status
        self.cost = cost
        super().__init__(id=key or f"child-{agent}-{index}")

    def compose(self) -> ComposeResult:
        yield Static(self.render_text())

    def render_text(self) -> str:
        body = "\n".join(self.lines[-3:]) or "starting"
        return f"  [{self.status}] {self.agent} #{self.index} ${self.cost:.4f}\n    {body}"

    def update_state(self, lines: list[str], status: str, cost: float) -> None:
        self.lines = list(lines)
        self.status = status
        self.cost = cost
        try:
            self.query_one(Static).update(self.render_text())
        except Exception:
            pass


class SubagentScreen(ModalScreen[None]):
    """Full-screen live detail view for one selected child."""

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

    def __init__(self, title: str, lines: list[str], status: str, cost: float) -> None:
        self.title = title
        self.lines = list(lines)
        self.status = status
        self.cost = cost
        super().__init__()

    def compose(self) -> ComposeResult:
        yield VerticalScroll(
            Static(f"{self.title} [{self.status}] ${self.cost:.4f}\n"),
            Static("\n".join(self.lines) or "starting"),
        )

    def on_key(self, event) -> None:
        if event.key in {"escape", "q"}:
            self.dismiss(None)

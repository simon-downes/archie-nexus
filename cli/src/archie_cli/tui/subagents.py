"""Subagent detail screen (status only; the transcript view arrives with detail.py)."""

from __future__ import annotations

from archie_shared.transcript import SubagentNode, subagent_status_text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static


class SubagentScreen(ModalScreen[None]):
    """Live status view for one subagent node."""

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

    def __init__(self, sub: SubagentNode) -> None:
        self.sub = sub
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
                f"Subagent {self.sub.label} [{self.sub.status}] ${self.sub.cost_usd:.4f}"
            )
            self.query_one("#subagent-body", Static).update(subagent_status_text(self.sub))
        except Exception:
            pass

    def on_key(self, event) -> None:
        if event.key in {"escape", "q"}:
            self.dismiss(None)

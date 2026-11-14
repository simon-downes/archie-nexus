"""Notices: short cards at the foot of the sidebar, above the key hints (084 D17).

``ArchieApp.notice`` adds one to the sidebar's ``NoticeStack``: a bold title over a
line of text, with a left bar coloured by severity. At most ``NoticeStack.LIMIT`` show
(the oldest goes first); each removes itself after its timeout or when clicked. A
notice about a session carries its ID, and clicking it also posts ``Notice.Chosen`` so
the app switches to that session.

Textual's own toasts (``App.notify``) are left alone for Textual's internals.
"""

from __future__ import annotations

from typing import Literal

from rich.text import Text
from textual.containers import Vertical
from textual.events import Click
from textual.message import Message
from textual.widgets import Static

type Severity = Literal["information", "warning", "error"]
"""How a notice is marked: the colour of its left bar."""

NOTICE_TIMEOUT = 8.0
"""Seconds a notice shows unless clicked."""


class Notice(Static):
    """One card: an optional bold title over the text. A click dismisses it.

    Attributes:
        title: The bold first line ("" for none).
        text: The text under it.
        severity: The bar's colour.
        session_id: The session the notice is about; a click switches to it.
        dismissed: The notice is going (its removal may still be pending).
    """

    DEFAULT_CSS = """
    Notice {
        width: 1fr;
        height: auto;
        margin: 1 0 0 0;
        padding: 0 1;
    }
    """

    class Chosen(Message):
        """A notice about a session was clicked."""

        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    def __init__(
        self, title: str, text: str, severity: Severity, session_id: str | None = None
    ) -> None:
        body = Text()
        if title:
            body.append(title, style="bold")
            if text:
                body.append("\n")
        body.append(text)
        super().__init__(body, classes=f"-{severity}")
        self.title = title
        self.text = text
        self.severity = severity
        self.session_id = session_id
        self.dismissed = False

    def dismiss(self) -> None:
        """Remove the notice (once)."""
        if not self.dismissed:
            self.dismissed = True
            self.remove()

    def on_click(self, event: Click) -> None:
        event.stop()
        if self.session_id is not None:
            # To the app directly: the notice is about to go, with its message queue.
            self.app.post_message(self.Chosen(self.session_id))
        self.dismiss()


class NoticeStack(Vertical):
    """The sidebar's notices, newest last; at most ``LIMIT`` at a time."""

    DEFAULT_CSS = """
    NoticeStack {
        height: auto;
        padding: 0 1 0 2;
    }
    """

    LIMIT = 3

    @property
    def notices(self) -> list[Notice]:
        """The notices shown (not being removed), oldest first."""
        return [c for c in self.children if isinstance(c, Notice) and not c.dismissed]

    def add(
        self,
        title: str,
        text: str = "",
        severity: Severity = "information",
        *,
        timeout: float = NOTICE_TIMEOUT,
        session_id: str | None = None,
    ) -> Notice:
        """Show a notice, removing the oldest ones beyond ``LIMIT``.

        Returns:
            The mounted notice.
        """
        shown = self.notices
        for old in shown[: max(0, len(shown) - (self.LIMIT - 1))]:
            old.dismiss()
        notice = Notice(title, text, severity, session_id)
        self.mount(notice)
        # The timer belongs to the notice, so it goes with the notice if that goes first.
        notice.set_timer(timeout, notice.dismiss)
        return notice

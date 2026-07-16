"""Message input widget.

A TextArea configured for chat input:
- Enter sends the message
- Shift+Enter inserts a newline (for multiline messages)
- Tab moves focus (doesn't insert a tab character)
- Up/Down at boundary positions cycle through sent message history

The widget posts a Submitted message (Textual's event system) when the
user presses Enter with non-empty content. The parent app handles this
message via on_message_input_submitted().
"""

from textual.message import Message
from textual.widgets import TextArea


class MessageInput(TextArea):
    """Multiline input area with chat-style key bindings and history."""

    DEFAULT_CSS = """
    MessageInput {
        height: auto;
        max-height: 8;
        min-height: 1;
        margin: 0;
        padding: 0 1;
    }
    """

    class Submitted(Message):
        """Posted when user presses Enter with non-empty content."""

        def __init__(self, content: str) -> None:
            super().__init__()
            self.content = content

    def __init__(self, **kwargs) -> None:
        super().__init__(language=None, show_line_numbers=False, **kwargs)
        self.tab_behavior = "focus"
        self._history: list[str] = []
        self._history_idx: int = 0
        self._draft: str = ""

    def _at_start(self) -> bool:
        """Return True if cursor is at row 0, col 0 (or input is empty)."""
        row, col = self.cursor_location
        return row == 0 and col == 0

    def _at_end(self) -> bool:
        """Return True if cursor is at end of the last line."""
        row, col = self.cursor_location
        last_row = self.document.line_count - 1
        if row < last_row:
            return False
        last_line = self.document.get_line(last_row)
        return col >= len(last_line)

    # Numpad keys that some terminals send as distinct key events
    _NUMPAD_MAP: dict[str, str] = {
        "divide": "/",
        "multiply": "*",
        "subtract": "-",
        "add": "+",
        "decimal": ".",
        "separator": ".",
    }

    async def _on_key(self, event) -> None:
        """Override key handling for chat-style Enter behaviour and history.

        - Enter: send the message (if non-empty), clear the input
        - Shift+Enter: insert a newline (the "escape hatch" for multiline)
        - Up (at start): cycle to previous history entry
        - Down (at end): cycle to next history entry / restore draft
        - Numpad arithmetic keys: insert their character
        """
        # Numpad key mapping
        if event.key in self._NUMPAD_MAP:
            event.prevent_default()
            event.stop()
            self.insert(self._NUMPAD_MAP[event.key])
            return
        if event.key in ("kp_enter",):
            # Treat numpad Enter as regular Enter (submit)
            event.prevent_default()
            event.stop()
            content = self.text.strip()
            if content:
                self._history.append(content)
                self._history_idx = len(self._history)
                self._draft = ""
                self.post_message(self.Submitted(content))
                self.clear()
            return

        if event.key == "enter":
            event.prevent_default()
            event.stop()
            content = self.text.strip()
            if content:
                self._history.append(content)
                self._history_idx = len(self._history)
                self._draft = ""
                self.post_message(self.Submitted(content))
                self.clear()
        elif event.key == "shift+enter":
            event.prevent_default()
            event.stop()
            self.insert("\n")
        elif event.key == "up":
            if self._at_start() or not self.text:
                event.prevent_default()
                event.stop()
                self._history_up()
        elif event.key == "down":
            if self._at_end():
                event.prevent_default()
                event.stop()
                self._history_down()

    def _history_up(self) -> None:
        """Navigate to the previous history entry."""
        if not self._history:
            return
        # Save current text as draft if we're at the end (not yet navigating)
        if self._history_idx == len(self._history):
            self._draft = self.text
        if self._history_idx > 0:
            self._history_idx -= 1
            self._load_text(self._history[self._history_idx])

    def _history_down(self) -> None:
        """Navigate to the next history entry or restore draft."""
        if not self._history:
            return
        if self._history_idx < len(self._history):
            self._history_idx += 1
            if self._history_idx == len(self._history):
                self._load_text(self._draft)
            else:
                self._load_text(self._history[self._history_idx])

    def _load_text(self, text: str) -> None:
        """Replace input content with the given text."""
        self.clear()
        if text:
            self.insert(text)

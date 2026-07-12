"""Tests for MessageInput history cycling."""


# ---------------------------------------------------------------------------
# Helpers — minimal mock for TextArea behaviour
# ---------------------------------------------------------------------------


class FakeDocument:
    """Minimal document mock for testing cursor boundary detection."""

    def __init__(self, text: str = ""):
        self._text = text

    @property
    def line_count(self) -> int:
        lines = self._text.split("\n")
        return len(lines)

    def get_line(self, idx: int) -> str:
        lines = self._text.split("\n")
        if 0 <= idx < len(lines):
            return lines[idx]
        return ""


class MockMessageInput:
    """Test harness that simulates MessageInput without a running Textual app.

    Mirrors the history logic from MessageInput so we can test it in isolation
    without needing async Textual infrastructure.
    """

    def __init__(self):
        self._history: list[str] = []
        self._history_idx: int = 0
        self._draft: str = ""
        self._text: str = ""
        self._cursor_row: int = 0
        self._cursor_col: int = 0
        self._messages: list = []

    @property
    def text(self) -> str:
        return self._text

    @property
    def cursor_location(self) -> tuple[int, int]:
        return (self._cursor_row, self._cursor_col)

    @property
    def document(self):
        return FakeDocument(self._text)

    def _at_start(self) -> bool:
        row, col = self.cursor_location
        return row == 0 and col == 0

    def _at_end(self) -> bool:
        row, col = self.cursor_location
        last_row = self.document.line_count - 1
        if row < last_row:
            return False
        last_line = self.document.get_line(last_row)
        return col >= len(last_line)

    def clear(self):
        self._text = ""
        self._cursor_row = 0
        self._cursor_col = 0

    def insert(self, text: str):
        self._text += text
        lines = self._text.split("\n")
        self._cursor_row = len(lines) - 1
        self._cursor_col = len(lines[-1])

    def _load_text(self, text: str) -> None:
        self.clear()
        if text:
            self.insert(text)

    def _history_up(self) -> None:
        if not self._history:
            return
        if self._history_idx == len(self._history):
            self._draft = self._text
        if self._history_idx > 0:
            self._history_idx -= 1
            self._load_text(self._history[self._history_idx])

    def _history_down(self) -> None:
        if not self._history:
            return
        if self._history_idx < len(self._history):
            self._history_idx += 1
            if self._history_idx == len(self._history):
                self._load_text(self._draft)
            else:
                self._load_text(self._history[self._history_idx])

    def submit(self, text: str) -> None:
        """Simulate submitting text (Enter key with content)."""
        self._text = text
        content = self._text.strip()
        if content:
            self._history.append(content)
            self._history_idx = len(self._history)
            self._draft = ""
            self.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHistoryCycling:
    """Tests for Up/Down history navigation."""

    def test_up_cycles_through_history(self):
        inp = MockMessageInput()
        inp.submit("first")
        inp.submit("second")
        inp.submit("third")

        # Up → third
        inp._history_up()
        assert inp.text == "third"

        # Up → second
        inp._history_up()
        assert inp.text == "second"

        # Up → first
        inp._history_up()
        assert inp.text == "first"

        # Up again → stays at first (clamped)
        inp._history_up()
        assert inp.text == "first"

    def test_down_returns_to_draft(self):
        inp = MockMessageInput()
        inp.submit("one")
        inp.submit("two")

        # Type a draft
        inp.insert("my draft")

        # Up → saves draft, shows "two"
        inp._history_up()
        assert inp.text == "two"

        # Down → restores draft
        inp._history_down()
        assert inp.text == "my draft"

    def test_draft_preserved_across_cycling(self):
        inp = MockMessageInput()
        inp.submit("msg1")
        inp.submit("msg2")

        inp.insert("hello")
        inp._history_up()  # saves "hello" as draft, shows msg2
        assert inp.text == "msg2"

        inp._history_up()  # shows msg1
        assert inp.text == "msg1"

        inp._history_down()  # shows msg2
        assert inp.text == "msg2"

        inp._history_down()  # restores draft "hello"
        assert inp.text == "hello"

    def test_empty_history_up_is_noop(self):
        inp = MockMessageInput()
        inp.insert("text")
        inp._history_up()
        assert inp.text == "text"

    def test_empty_history_down_is_noop(self):
        inp = MockMessageInput()
        inp.insert("text")
        inp._history_down()
        assert inp.text == "text"

    def test_single_entry_cycling(self):
        inp = MockMessageInput()
        inp.submit("only")

        inp._history_up()
        assert inp.text == "only"

        inp._history_down()
        assert inp.text == ""  # draft was empty

    def test_submit_resets_index(self):
        inp = MockMessageInput()
        inp.submit("first")
        inp.submit("second")

        # Navigate up
        inp._history_up()
        assert inp.text == "second"

        # Submit something new
        inp._text = "third"
        inp.submit("third")

        # History now has 3 entries, index at end
        assert inp._history == ["first", "second", "third"]
        assert inp._history_idx == 3

    def test_up_from_middle_of_multiline_does_not_trigger(self):
        """Cursor on row > 0 should not trigger history (tested via _at_start)."""
        inp = MockMessageInput()
        inp.submit("prev")

        inp.insert("line1\nline2\nline3")
        inp._cursor_row = 1  # middle line
        inp._cursor_col = 2

        # _at_start returns False → history should not be invoked
        assert not inp._at_start()

    def test_down_from_middle_of_multiline_does_not_trigger(self):
        """Cursor not at end should not trigger history (tested via _at_end)."""
        inp = MockMessageInput()
        inp.submit("prev")

        inp.insert("line1\nline2\nline3")
        inp._cursor_row = 1  # middle line
        inp._cursor_col = 2

        # _at_end returns False → history should not be invoked
        assert not inp._at_end()

    def test_at_start_empty_text(self):
        """Empty text is always 'at start'."""
        inp = MockMessageInput()
        assert inp._at_start()

    def test_at_end_empty_text(self):
        """Empty text is always 'at end'."""
        inp = MockMessageInput()
        assert inp._at_end()

    def test_at_end_single_line(self):
        """Cursor at end of single line text."""
        inp = MockMessageInput()
        inp.insert("hello")
        assert inp._at_end()

        # Move cursor back
        inp._cursor_col = 2
        assert not inp._at_end()

    def test_at_end_multiline(self):
        """Cursor at end of last line in multiline text."""
        inp = MockMessageInput()
        inp.insert("line1\nline2")
        # After insert, cursor is at end of last line
        assert inp._at_end()

        # Move to first row
        inp._cursor_row = 0
        inp._cursor_col = 5
        assert not inp._at_end()

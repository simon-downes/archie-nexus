"""Conversation display widget with styled message blocks.

The conversation is a vertical scroll container that holds individual
message widgets. Each message type has its own styling:
- UserMessage: highlighted background so your messages stand out
- AssistantMessage: rendered Markdown for rich formatting
- StreamingMessage: plain text that updates live during generation
- ErrorMessage: red styling for errors

The streaming → finalised flow:
1. When the model starts generating, we mount a StreamingMessage
2. Text chunks are appended to it as they arrive (plain text, fast updates)
3. When generation completes, we REPLACE it with an AssistantMessage
   which renders the full response as proper Markdown (slower but prettier)
"""

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Markdown, Static

from archie_cli.tui import theme


class UserMessage(Static):
    """A user message block with highlighted background."""

    can_focus = True

    DEFAULT_CSS = """
    UserMessage {
        margin: 0 0 1 0;
        padding: 1 2;
    }
    """

    def __init__(self, content: str) -> None:
        self._content = content
        super().__init__()

    def compose(self) -> ComposeResult:
        """Build the user message widget."""
        header_text = Text.assemble(
            ("▶ You\n", Style(color=theme.SECONDARY, bold=True)),
            self._content,
        )
        yield Static(header_text)

    def get_copy_text(self) -> str:
        """Return the plain text content for clipboard copy."""
        return self._content


class AssistantMessage(Widget):
    """A finalised assistant message with full Markdown rendering."""

    can_focus = True

    DEFAULT_CSS = """
    AssistantMessage {
        margin: 0 0 1 0;
        padding: 0 2;
        height: auto;
    }
    AssistantMessage > .header {
        height: auto;
    }
    AssistantMessage > Markdown {
        margin: 0;
        padding: 0;
        height: auto;
    }
    """

    def __init__(self, content: str = "") -> None:
        super().__init__()
        self._content = content

    def compose(self) -> ComposeResult:
        """Build the assistant message widget with header and markdown."""
        yield Static(
            Text.assemble(("● Archie", Style(color=theme.PRIMARY, bold=True))),
            classes="header",
        )
        yield Markdown(self._content)

    def get_copy_text(self) -> str:
        """Return the markdown source text for clipboard copy."""
        return self._content

    def update_content(self, content: str) -> None:
        """Update the markdown content after initial render."""
        self._content = content
        try:
            md = self.query_one(Markdown)
            md.update(content)
        except Exception:  # noqa: BLE001 — widget may not be mounted yet
            pass


class StreamingMessage(Widget):
    """A message that's actively being streamed from the model.

    Uses a reactive `text` property — when text changes, the content
    Static widget automatically updates via watch_text().
    """

    DEFAULT_CSS = """
    StreamingMessage {
        margin: 0 0 1 0;
        padding: 0 2;
        height: auto;
    }
    StreamingMessage > .header {
        height: auto;
    }
    StreamingMessage > .content {
        margin: 0;
        padding: 0;
        height: auto;
    }
    """

    text: reactive[str] = reactive("")

    def compose(self) -> ComposeResult:
        """Build the streaming message widget with spinner."""
        yield Static(
            Text.assemble(("● Archie", Style(color=theme.PRIMARY, bold=True)), (" ⟳")),
            classes="header",
        )
        yield Static("", classes="content")

    def watch_text(self, value: str) -> None:
        """Called automatically when self.text changes. Updates the display."""
        try:
            self.query_one(".content", Static).update(value)
        except Exception:  # noqa: BLE001 — widget may not be mounted yet
            pass

    def append(self, chunk: str) -> None:
        """Append a text chunk. Triggers reactive update."""
        self.text += chunk


class ErrorMessage(Static):
    """An error message block with red styling."""

    DEFAULT_CSS = """
    ErrorMessage {
        padding: 1 2;
        margin: 0 0 1 0;
    }
    """

    can_focus = True

    def __init__(self, content: str) -> None:
        super().__init__(f"[bold red]✗ Error[/]\n{content}")
        self._content = content

    def get_copy_text(self) -> str:
        """Return the error text for clipboard copy."""
        return self._content


class Conversation(VerticalScroll):
    """Scrollable container for message blocks."""

    DEFAULT_CSS = """
    Conversation {
        height: 1fr;
        padding: 0;
    }
    """

    def add_user_message(self, content: str) -> None:
        """Add a user message and scroll to show it."""
        self.mount(UserMessage(content))
        self.scroll_end(animate=False)

    def add_error(self, content: str) -> None:
        """Add an error message and scroll to show it."""
        self.mount(ErrorMessage(content))
        self.scroll_end(animate=False)

    def add_assistant_message(self, content: str) -> None:
        """Add a complete assistant message (used for history replay)."""
        self.mount(AssistantMessage(content.rstrip("\n")))
        self.scroll_end(animate=False)

    def begin_streaming(self) -> StreamingMessage:
        """Start a streaming response. Returns the widget to append chunks to."""
        msg = StreamingMessage()
        self.mount(msg)
        self.scroll_end(animate=False)
        return msg

    def finalise_streaming(self, streaming: StreamingMessage) -> None:
        """Replace a streaming widget with a finalised Markdown version."""
        final = AssistantMessage(streaming.text.rstrip("\n"))
        self.mount(final, before=streaming)
        streaming.remove()
        self.scroll_end(animate=False)

    def end_iteration(self) -> None:
        """Mark the current iteration as done (no-op in v1, kept for interface compat)."""

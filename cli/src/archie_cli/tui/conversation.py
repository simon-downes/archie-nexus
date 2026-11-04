"""Message widgets for the conversation transcript.

``transcript_view.TranscriptView`` mounts these as rows. Each message type has
its own styling:
- UserMessage: highlighted background so your messages stand out
- AssistantMessage: rendered Markdown for rich formatting
- StreamingMessage: Markdown that updates live during generation
- ErrorMessage: red styling for agent/server errors (recorded in the session)
- ClientErrorMessage: warning styling for client/transport errors (local only,
  NOT recorded in the session log)

The streaming flow:
1. When the model starts generating, a StreamingMessage row is mounted
2. Text chunks are appended and the Markdown content re-renders live
3. When generation completes, the streaming indicator is removed
"""

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.css.query import NoMatches
from textual.markup import escape
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Markdown, Static

from archie_cli.tui import theme


class TurnStatus(Static):
    """Muted client-only summary of one completed turn."""

    DEFAULT_CSS = """
    TurnStatus {
        margin: 0 0 1 0;
        padding: 0 2;
        height: auto;
    }
    """

    def __init__(
        self,
        duration_s: float,
        input_tokens: int,
        cache_read: int,
        cache_write: int,
        output_tokens: int,
        cost: float,
    ) -> None:
        super().__init__()
        self._duration_s = duration_s
        self._input_tokens = input_tokens
        self._cache_read = cache_read
        self._cache_write = cache_write
        self._output_tokens = output_tokens
        self._cost = cost

    def compose(self) -> ComposeResult:
        duration = f"{self._duration_s:.0f}s"
        input_tokens = " / ".join(
            _fmt_tokens(n) for n in (self._input_tokens, self._cache_read, self._cache_write)
        )
        output_tokens = _fmt_tokens(self._output_tokens)
        yield Static(
            Text.from_markup(
                f"[{theme.MUTED}]▸ Time: {duration} | In: {input_tokens}  Out: {output_tokens} │ ${self._cost:.4f}[/]"
            )
        )


def _fmt_tokens(value: int) -> str:
    return f"{value / 1000:.1f}K" if value >= 1000 else str(value)


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
    AssistantMessage > Markdown > MarkdownFence {
        background: #252535;
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

    The Markdown widget is updated in place as text arrives. This keeps the
    message mounted throughout streaming while still showing rich formatting.
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
    StreamingMessage > Markdown {
        margin: 0;
        padding: 0;
        height: auto;
    }
    StreamingMessage > Markdown > MarkdownFence {
        background: #252535;
    }
    """

    text: reactive[str] = reactive("")

    def __init__(self, text: str = "") -> None:
        super().__init__()
        self._markdown_timer: Timer | None = None
        self._markdown_dirty = False
        self.set_reactive(StreamingMessage.text, text)

    def compose(self) -> ComposeResult:
        """Build the streaming message widget with spinner and Markdown."""
        yield Static(
            Text.assemble(("● Archie", Style(color=theme.PRIMARY, bold=True)), (" ⟳")),
            classes="header",
        )
        yield Markdown(self.text, classes="content")

    def watch_text(self, value: str) -> None:
        """Schedule a throttled Markdown refresh when streamed text changes."""
        self._markdown_dirty = True
        if self._markdown_timer is None:
            self._markdown_timer = self.set_timer(0.1, self._refresh_markdown)

    def _refresh_markdown(self) -> None:
        """Render the latest text, at most once per throttle interval."""
        self._markdown_timer = None
        if not self._markdown_dirty:
            return
        self._markdown_dirty = False
        try:
            self.query_one(Markdown).update(self.text)
        except Exception:  # noqa: BLE001 — widget may not be mounted yet
            pass

    def append(self, chunk: str) -> None:
        """Append a text chunk and schedule a throttled Markdown update."""
        self.text += chunk

    def finish(self) -> None:
        """Flush Markdown and remove the streaming indicator."""
        if self._markdown_timer is not None:
            self._markdown_timer.stop()
            self._markdown_timer = None
        self._markdown_dirty = False
        try:
            self.query_one(Markdown).update(self.text.rstrip("\n"))
            self.query_one(".header", Static).update(
                Text.assemble(("● Archie", Style(color=theme.PRIMARY, bold=True)))
            )
        except Exception:  # noqa: BLE001 — widget may not be mounted yet
            pass


class CancelledMessage(Static):
    """A compact cancellation status message."""

    DEFAULT_CSS = """
    CancelledMessage {
        padding: 1 2;
        margin: 0 0 1 0;
        color: $error;
    }
    """

    can_focus = True

    def __init__(self) -> None:
        super().__init__("Cancelled")

    def get_copy_text(self) -> str:
        """Return the cancellation text for clipboard copy."""
        return "Cancelled"


class ErrorMessage(Static):
    """An error message block with red styling.

    Represents an agent/server error — these are recorded in the session log.
    """

    DEFAULT_CSS = """
    ErrorMessage {
        padding: 1 2;
        margin: 0 0 1 0;
    }
    """

    can_focus = True

    def __init__(self, content: str) -> None:
        super().__init__(f"[bold {theme.ERROR}]✗ Error[/]\n{content}")
        self._content = content

    def get_copy_text(self) -> str:
        """Return the error text for clipboard copy."""
        return self._content


class ClientErrorMessage(Static):
    """A client/transport error block, visually distinct from agent errors.

    These originate in the TUI or the WebSocket transport (e.g. connection
    lost, send failed). They are NOT recorded in the session log, so they are
    explicitly marked as local to the client to avoid confusion when comparing
    the on-screen conversation against the persisted session.
    """

    DEFAULT_CSS = """
    ClientErrorMessage {
        padding: 1 2;
        margin: 0 0 1 0;
        border-left: thick $warning;
    }
    """

    can_focus = True

    def __init__(self, content: str) -> None:
        super().__init__(
            f"[bold {theme.WARNING}]⚠ Client error[/] "
            f"[dim](local — not saved to session)[/]\n{content}"
        )
        self._content = content

    def get_copy_text(self) -> str:
        """Return the error text for clipboard copy."""
        return self._content


class ShellOutput(Widget):
    """Direct shell command output (! prefix), not involving the LLM.

    Shows the command with a $ prefix and dimmed output. Visually distinct
    from both user messages and assistant responses.
    """

    can_focus = True

    DEFAULT_CSS = """
    ShellOutput {
        margin: 0 0 1 0;
        padding: 1 2;
        height: auto;
    }
    ShellOutput > .shell-header {
        height: auto;
    }
    ShellOutput > .shell-output {
        height: auto;
        margin: 0 0 0 2;
    }
    """

    def __init__(self, command: str, output: str, exit_code: int = 0) -> None:
        super().__init__()
        self._command = command
        self._output = output
        self._exit_code = exit_code

    def compose(self) -> ComposeResult:
        """Build the shell output with command header and output body."""
        # Header: status bullet, then $ command (exit N) if non-zero.
        status_colour = theme.ERROR if self._exit_code else theme.SUCCESS
        exit_suffix = f" [dim red](exit {self._exit_code})[/]" if self._exit_code else ""
        yield Static(
            f"[bold {status_colour}]●[/] [bold {theme.MUTED}]$[/] "
            f"[bold]{_esc(self._command)}[/]{exit_suffix}",
            classes="shell-header",
            markup=True,
        )
        if self._output:
            yield Static(
                f"[dim]{_esc(self._output)}[/]",
                classes="shell-output",
                markup=True,
            )

    def get_copy_text(self) -> str:
        """Return command + output for clipboard."""
        return f"$ {self._command}\n{self._output}"


def _esc(text: str) -> str:
    """Escape Rich markup characters in arbitrary text."""
    return text.replace("[", r"\[").replace("]", r"\]")


class CompactionEntry(Widget):
    """A compaction operation with the same pending/terminal indicator as tools."""

    DEFAULT_CSS = """
    CompactionEntry {
        height: auto;
        margin: 0;
        padding: 0 2;
    }
    CompactionEntry > .tool-header {
        height: auto;
    }
    """

    def __init__(self, reason: str, before: int) -> None:
        super().__init__()
        self._reason = reason
        self._before = before
        self._markup = f"[bold {theme.PRIMARY}]○[/] Compaction {escape(reason)} ({before} tokens)"

    def compose(self) -> ComposeResult:
        yield Static(self._markup, classes="tool-header", markup=True)

    def complete(
        self, after: int | None, display_cost: float, duration_ms: int, error: str | None
    ) -> None:
        """Replace the pending header with the terminal success or failure line.

        The terminal markup is kept on the entry so a header composed after this
        call (an entry mounted and completed in the same tick) still shows it.
        """
        colour = theme.ERROR if error else theme.SUCCESS
        reason = escape(self._reason)
        if error:
            text = f"Compaction {reason} ({self._before} tokens, {duration_ms}ms) Failed: {escape(error)}"
        else:
            text = f"Compaction {reason} ({self._before} → {after}, ${display_cost:.2f}, {duration_ms}ms)"
        self._markup = f"[bold {colour}]●[/] {text}"
        try:
            self.query_one(".tool-header", Static).update(self._markup)
        except NoMatches:
            pass  # compose() renders self._markup once mounted


class CompactionMarker(Widget):
    """Inline marker and Markdown summary for a compacted context."""

    DEFAULT_CSS = """
    CompactionMarker {
        margin: 0 0 1 0;
        padding: 1 2;
        height: auto;
    }
    CompactionMarker > Markdown {
        margin: 1 0 0 0;
    }
    """

    def __init__(self, reason: str, before: int, after: int, summary: str) -> None:
        super().__init__()
        self._header = f"Context compacted ({reason} · {before} → {after} tokens)"
        self._summary = summary

    def compose(self) -> ComposeResult:
        yield Static(self._header)
        yield Markdown(self._summary)

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
2. Text fragments are written to a MarkdownStream as they arrive
3. When generation completes, the streaming indicator is removed
"""

import asyncio

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.css.query import NoMatches
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

    Text fragments are written to a ``MarkdownStream``, which appends them to the
    Markdown document and re-parses only its last block, so the cost of a tick
    does not grow with the length of the reply.
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

    def __init__(self, text: str = "") -> None:
        super().__init__()
        self.text = text
        self._written = 0  # length of ``text`` already in the Markdown document
        self._wake = asyncio.Event()
        self._finished = False

    def compose(self) -> ComposeResult:
        """Build the streaming message widget with spinner and Markdown."""
        yield Static(
            Text.assemble(("● Archie", Style(color=theme.PRIMARY, bold=True)), (" ⟳")),
            classes="header",
        )
        # Start empty and write the text so far through the stream: a Markdown
        # built with text records its last parsed line past a trailing blank line,
        # and the first ``append`` would then overwrite the last block.
        yield Markdown("", classes="content")

    def on_mount(self) -> None:
        self._wake.set()  # write the text so far
        self.run_worker(self._feed(), group="markdown-stream", exit_on_error=False)

    async def _feed(self) -> None:
        """Write new text to the Markdown stream until ``finish``."""
        markdown = self.query_one(Markdown)
        stream = Markdown.get_stream(markdown)
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                chunk = self.text[self._written :]
                self._written = len(self.text)
                await stream.write(chunk)
                if self._finished:
                    break
        finally:
            await stream.stop()
        final = self.text.rstrip("\n")
        if markdown.source.rstrip("\n") != final:
            await markdown.update(final)

    def append(self, chunk: str) -> None:
        """Append a text fragment; the stream renders it on its next pass."""
        self.text += chunk
        self._wake.set()

    def finish(self) -> None:
        """Flush the stream and remove the streaming indicator."""
        self._finished = True
        self._wake.set()
        try:
            self.query_one(".header", Static).update(
                Text.assemble(("● Archie", Style(color=theme.PRIMARY, bold=True)))
            )
        except NoMatches:  # not composed yet
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
        # Text, not markup: error text is arbitrary and must never be parsed.
        super().__init__(Text.assemble(("✗ Error", f"bold {theme.ERROR}"), "\n", content))
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
        header = Text.assemble(
            ("●", f"bold {status_colour}"),
            " ",
            ("$", f"bold {theme.MUTED}"),
            " ",
            (self._command, "bold"),
        )
        if self._exit_code:
            header.append(f" (exit {self._exit_code})", style="dim red")
        yield Static(header, classes="shell-header")
        if self._output:
            yield Static(Text(self._output, style="dim"), classes="shell-output")

    def get_copy_text(self) -> str:
        """Return command + output for clipboard."""
        return f"$ {self._command}\n{self._output}"


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
        # Text, not markup: the reason and error are arbitrary and must never be parsed.
        self._header = Text.assemble(
            ("○", f"bold {theme.PRIMARY}"), f" Compaction {reason} ({before} tokens)"
        )

    def compose(self) -> ComposeResult:
        yield Static(self._header, classes="tool-header")

    def complete(
        self, after: int | None, display_cost: float, duration_ms: int, error: str | None
    ) -> None:
        """Replace the pending header with the terminal success or failure line.

        The terminal header is kept on the entry so a header composed after this
        call (an entry mounted and completed in the same tick) still shows it.
        """
        colour = theme.ERROR if error else theme.SUCCESS
        reason = self._reason
        if error:
            text = f"Compaction {reason} ({self._before} tokens, {duration_ms}ms) Failed: {error}"
        else:
            text = f"Compaction {reason} ({self._before} → {after}, ${display_cost:.2f}, {duration_ms}ms)"
        self._header = Text.assemble(("●", f"bold {colour}"), f" {text}")
        try:
            self.query_one(".tool-header", Static).update(self._header)
        except NoMatches:
            pass  # compose() renders self._header once mounted


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

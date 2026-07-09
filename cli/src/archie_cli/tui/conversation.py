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


def _esc(text: str) -> str:
    """Escape Rich markup characters in arbitrary text."""
    return text.replace("[", r"\[").replace("]", r"\]")


class ToolEntry(Widget):
    """A single tool call within an IterationBlock.

    Shows source code (collapsible) while pending, then completion metrics.
    Click to expand/collapse the source beyond 10 lines.
    """

    can_focus = True

    DEFAULT_CSS = """
    ToolEntry {
        height: auto;
        margin: 0;
        padding: 0;
    }
    ToolEntry > .tool-header {
        height: auto;
    }
    ToolEntry > .tool-source {
        height: auto;
        margin: 0 0 0 2;
    }
    """

    _MAX_COLLAPSED_LINES = 10

    def __init__(self, tool_use_id: str, name: str, source: str) -> None:
        super().__init__()
        self._tool_use_id = tool_use_id
        self._name = name
        self._source = source
        self._expanded = False
        self._completed = False
        self._completion_text = ""

    def compose(self) -> ComposeResult:
        """Build the tool entry with header and source."""
        yield Static(
            f"[bold {theme.PRIMARY}]○[/] [bold]Exec[/]",
            classes="tool-header",
            markup=True,
        )
        yield Static(self._render_source(), classes="tool-source", markup=True)

    def _render_source(self) -> str:
        """Render source code with line limit and collapse indicator."""
        if not self._source:
            return ""
        lines = self._source.split("\n")
        if len(lines) <= self._MAX_COLLAPSED_LINES or self._expanded:
            rendered = "\n".join(f"[dim]{_esc(line)}[/]" for line in lines)
            if self._expanded and len(lines) > self._MAX_COLLAPSED_LINES:
                rendered += "\n[dim italic]▲ click to collapse[/]"
        else:
            shown = lines[: self._MAX_COLLAPSED_LINES]
            remaining = len(lines) - self._MAX_COLLAPSED_LINES
            rendered = "\n".join(f"[dim]{_esc(line)}[/]" for line in shown)
            rendered += f"\n[dim italic]▼ +{remaining} lines (click to expand)[/]"
        return rendered

    def complete(
        self, is_error: bool, duration_ms: int, result_bytes: int, summary: str = ""
    ) -> None:
        """Mark this tool entry as complete with metrics."""
        self._completed = True
        colour = theme.ERROR if is_error else theme.SUCCESS

        # Compute metrics
        result_lines = max(1, result_bytes // 40) if result_bytes else 0  # rough estimate
        result_tokens = result_bytes // 4 if result_bytes else 0

        if is_error:
            # Show the error message
            error_msg = summary.split("\n")[0][:120] if summary else "error"
            header = f"[bold {colour}]●[/] [bold]Exec[/] [dim red]{_esc(error_msg)}[/]"
        else:
            parts = []
            if duration_ms:
                if duration_ms < 1000:
                    parts.append(f"{duration_ms}ms")
                else:
                    parts.append(f"{duration_ms / 1000:.1f}s")
            if result_bytes:
                parts.append(f"~{result_lines} lines")
                parts.append(f"{result_bytes:,} bytes")
                parts.append(f"~{result_tokens:,} tokens")
            metrics = " · ".join(parts) if parts else "done"
            header = f"[bold {colour}]●[/] [bold]Exec[/] [dim]{metrics}[/]"

        try:
            self.query_one(".tool-header", Static).update(header)
        except Exception:
            pass

    def on_click(self) -> None:
        """Toggle source expansion on click."""
        lines = self._source.split("\n")
        if len(lines) <= self._MAX_COLLAPSED_LINES:
            return  # Nothing to expand
        self._expanded = not self._expanded
        try:
            self.query_one(".tool-source", Static).update(self._render_source())
        except Exception:
            pass

    def get_copy_text(self) -> str:
        """Return source code for clipboard."""
        return self._source


class IterationBlock(Widget):
    """A block showing tool calls for one agentic iteration.

    Groups multiple tool calls from a single LLM response.
    """

    can_focus = True

    DEFAULT_CSS = """
    IterationBlock {
        margin: 0 0 0 0;
        padding: 0 2;
        height: auto;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._tool_entries: dict[str, ToolEntry] = {}

    def add_pending(self, tool_use_id: str, name: str, source: str) -> None:
        """Add a pending tool entry showing the source code."""
        entry = ToolEntry(tool_use_id, name, source)
        self._tool_entries[tool_use_id] = entry
        self.mount(entry)

    def complete_tool(
        self,
        tool_use_id: str,
        is_error: bool,
        duration_ms: int,
        result_bytes: int,
        summary: str = "",
    ) -> None:
        """Mark a tool entry as complete with metrics."""
        entry = self._tool_entries.get(tool_use_id)
        if entry:
            entry.complete(is_error, duration_ms, result_bytes, summary)

    def get_copy_text(self) -> str:
        """Return all source code for clipboard."""
        parts = []
        for entry in self._tool_entries.values():
            parts.append(entry.get_copy_text())
        return "\n\n".join(parts)


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

    def begin_iteration(self) -> IterationBlock:
        """Start a new iteration block for tool calls."""
        block = IterationBlock()
        self.mount(block)
        self.scroll_end(animate=False)
        return block

    def end_iteration(self) -> None:
        """Mark the current iteration as done (no-op, kept for interface compat)."""

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

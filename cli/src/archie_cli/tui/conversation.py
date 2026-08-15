"""Conversation display widget with styled message blocks.

The conversation is a vertical scroll container that holds individual
message widgets. Each message type has its own styling:
- UserMessage: highlighted background so your messages stand out
- AssistantMessage: rendered Markdown for rich formatting
- StreamingMessage: plain text that updates live during generation
- ErrorMessage: red styling for agent/server errors (recorded in the session)
- ClientErrorMessage: warning styling for client/transport errors (local only,
  NOT recorded in the session log)

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
        super().__init__(f"[bold red]✗ Error[/]\n{content}")
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
        # Header: $ command (exit N) if non-zero
        exit_suffix = f" [dim red](exit {self._exit_code})[/]" if self._exit_code else ""
        yield Static(
            f"[bold {theme.MUTED}]$[/] [bold]{_esc(self._command)}[/]{exit_suffix}",
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


class ToolEntry(Widget):
    """A single tool call within an IterationBlock.

    For exec: shows source code (collapsible) while pending, then completion metrics.
    For native tools: shows a Rich-formatted one-liner that updates on completion.
    """

    can_focus = False

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

    def __init__(self, tool_use_id: str, name: str, input_summary: str) -> None:
        super().__init__()
        self._tool_use_id = tool_use_id
        self._name = name
        self._input_summary = input_summary
        self._is_exec = name == "exec"
        self._expanded = False
        self._completed = False

    def compose(self) -> ComposeResult:
        """Build the tool entry with header and optional source."""
        if self._is_exec:
            yield Static(
                f"[bold {theme.PRIMARY}]○[/] [bold]Exec[/]",
                classes="tool-header",
                markup=True,
            )
            yield Static(self._render_source(), classes="tool-source", markup=True)
        else:
            # Native tool: show the Rich-formatted pending summary
            yield Static(
                f"[bold {theme.PRIMARY}]○[/] {self._input_summary}",
                classes="tool-header",
                markup=True,
            )

    def _render_source(self) -> str:
        """Render exec source code with line limit and collapse indicator."""
        if not self._input_summary:
            return ""
        lines = self._input_summary.split("\n")
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

    def add_child(self, child: Widget) -> None:
        """Mount nested activity beneath this tool entry."""
        self.mount(child)

    def complete(
        self, is_error: bool, duration_ms: int, result_bytes: int, summary: str = ""
    ) -> None:
        """Mark this tool entry as complete with metrics.

        For exec: shows metrics (duration, bytes, tokens).
        For native tools: replaces header with the Rich-formatted completion summary.
        """
        self._completed = True
        colour = theme.ERROR if is_error else theme.SUCCESS

        if self._is_exec:
            if is_error:
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
                    result_lines = max(1, result_bytes // 40)
                    parts.append(f"~{result_lines} lines")
                    parts.append(f"{result_bytes:,} bytes")
                metrics = " · ".join(parts) if parts else "done"
                header = f"[bold {colour}]●[/] [bold]Exec[/] [dim]{metrics}[/]"
        else:
            # Native tool: summary is the Rich-formatted completion string
            if summary:
                header = f"[bold {colour}]●[/] {summary}"
            elif is_error:
                header = f"[bold {colour}]●[/] {self._input_summary} — [red]error[/]"
            else:
                header = f"[bold {colour}]●[/] {self._input_summary}"

        try:
            self.query_one(".tool-header", Static).update(header)
        except Exception:
            pass

    def on_click(self) -> None:
        """Toggle source expansion on click (exec only)."""
        if not self._is_exec:
            return
        lines = self._input_summary.split("\n")
        if len(lines) <= self._MAX_COLLAPSED_LINES:
            return
        self._expanded = not self._expanded
        try:
            self.query_one(".tool-source", Static).update(self._render_source())
        except Exception:
            pass

    def get_copy_text(self) -> str:
        """Return source code (exec) or summary (native) for clipboard."""
        return self._input_summary


class IterationBlock(Widget):
    """A block showing tool calls for one agentic iteration.

    Groups multiple tool calls from a single LLM response.
    """

    can_focus = True

    DEFAULT_CSS = """
    IterationBlock {
        margin: 0 0 1 0;
        padding: 0 2;
        height: auto;
        border-left: thick transparent;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._tool_entries: dict[str, ToolEntry] = {}

    def add_pending(self, tool_use_id: str, name: str, source: str) -> ToolEntry:
        """Add a pending tool entry showing the source code."""
        entry = ToolEntry(tool_use_id, name, source)
        self._tool_entries[tool_use_id] = entry
        self.mount(entry)
        return entry

    def get_tool(self, tool_use_id: str) -> ToolEntry | None:
        """Return a pending/completed tool entry by tool-use ID."""
        return self._tool_entries.get(tool_use_id)

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
        """Add an agent/server error message and scroll to show it."""
        self.mount(ErrorMessage(content))
        self.scroll_end(animate=False)

    def add_client_error(self, content: str) -> None:
        """Add a client/transport error, marked as local (not in session log)."""
        self.mount(ClientErrorMessage(content))
        self.scroll_end(animate=False)

    def add_shell_output(self, command: str, output: str, exit_code: int = 0) -> None:
        """Add direct shell command output (! prefix, no LLM involvement)."""
        self.mount(ShellOutput(command, output, exit_code))
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

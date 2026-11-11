"""One-line shell and exec rows for the prototype's transcripts.

The attach TUI previews up to 10 lines of every shell command and exec script in
the transcript. Those blocks of unhighlighted code make up much of what's on screen
(shell and exec are about a third of all tool calls in the logs) and say little
about what the agent is doing.

Here a shell or exec row is one line. If the call has a ``description`` input (a
proposed parameter: one sentence on what the call does), the row shows that.
Otherwise it shows the usual headline: the first line of the command, or exec's
metrics. The full source stays in the tool detail view.

The row renderer lives in ``archie_cli.tui.transcript_view`` and is shared with the
attach TUI, so the app swaps it in while it runs and puts it back when it exits.
"""

from __future__ import annotations

from collections.abc import Callable

from archie_shared.transcript import ToolNode, tool_extra, tool_headline
from rich.text import Text

from archie_cli.tui import theme, transcript_view
from archie_cli.tui.transcript_view import _markup, _status_glyph

COMPACT_TOOLS = ("shell", "exec")


def _metrics(tool: ToolNode) -> str:
    parts = []
    if tool.duration_ms:
        ms = tool.duration_ms
        parts.append(f"{ms}ms" if ms < 1000 else f"{ms / 1000:.1f}s")
    if tool.result_lines:
        parts.append(f"{tool.result_lines} lines")
    return ", ".join(parts)


def description(tool: ToolNode) -> str | None:
    value = tool.input.get("description")
    return value.strip() if isinstance(value, str) and value.strip() else None


def render_compact_row(tool: ToolNode) -> Text:
    """The tool row with shell and exec on one line; other tools are unchanged."""
    if tool.name not in COMPACT_TOOLS:
        return _original(tool)
    text = Text()
    text.append_text(_status_glyph(tool.status))
    text.append(" ")
    summary = description(tool)
    if summary is not None:
        text.append(tool.name.capitalize(), style="bold")
        text.append(f" {summary}")
        metrics = _metrics(tool)
        if metrics:
            text.append(f" ({metrics})", style=theme.MUTED)
    else:
        text.append_text(_markup(tool_headline(tool)))
    if tool.status == "error":
        extra = tool_extra(tool)
        if extra:
            text.append("\n")
            text.append_text(_markup(extra.split("\n")[0]))
    return text


_original: Callable[[ToolNode], Text] = transcript_view.render_tool_row


def install() -> Callable[[], None]:
    """Use the compact renderer for new and refreshed rows; returns the undo."""
    previous = transcript_view.render_tool_row
    transcript_view.render_tool_row = render_compact_row

    def restore() -> None:
        transcript_view.render_tool_row = previous

    return restore

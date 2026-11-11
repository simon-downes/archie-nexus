"""Transcript rows for the prototype: two-line tool rows and plain user messages.

Tool rows follow plans/tui-tool-rows.md.

Every tool call is two lines::

    ● Read · 141 of 880 lines · 4ms
    ╰ archie-nexus/shared/src/archie_shared/transcript.py:120-260

Line 1 is the status glyph, the tool name, muted metrics and the duration, and for a
failed call its error in red. Line 2 is what the call acted on: a path, pattern,
URL, query, or for shell and exec a ``description`` input when the call has one (a
proposed parameter). Diffs and output previews stay in the tool detail view.

User messages drop the attach TUI's "▶ You" header; their left rule marks them.

The attach TUI's row widgets live in ``archie_cli.tui.transcript_view`` and are
shared, so the app swaps these in while it runs and puts the originals back when it
exits.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from archie_shared.tool_summaries import (
    QUESTION_ANSWERED_PREFIX,
    QUESTION_DISMISSED_RESULT,
    QUESTION_SELECTED_PREFIX,
)
from archie_shared.transcript import ToolNode
from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static

from archie_cli.tui import theme, transcript_view
from archie_cli.tui.conversation import UserMessage

WORKSPACE = "/workspace"
CONNECTOR = "╰ "
TARGET_STYLE = "#9a9a9a"  # between the muted metrics and normal text
TOOL_NAME_STYLE = "bold #5fafff"  # bright blue, from the same xterm-256 range as theme.ANSWER

NAMES = {
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "glob": "Glob",
    "grep": "Grep",
    "code": "Code",
    "shell": "Shell",
    "exec": "Exec",
    "web_fetch": "Fetch",
    "web_search": "Web search",
    "brain_search": "Brain search",
    "skill": "Skill",
    "question": "Question",
    "task": "Task",
}

_ERROR_CLASS = re.compile(r"^[A-Z][A-Za-z]*(?:Error|Exception): ")
_TIMEOUT = re.compile(r"\[error: command timed out after (\d+)s")


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def strip_workspace(text: str) -> str:
    """Drop ``/workspace/`` from paths and commands; a bare ``/workspace`` becomes ``.``."""
    text = text.replace(f"{WORKSPACE}/", "")
    return re.sub(rf"{WORKSPACE}(?![\w.-])", ".", text)


def display_path(path: object) -> str:
    """A path for the row: workspace-relative, ``~`` for home, ``""`` for the root."""
    if not isinstance(path, str) or not path.strip():
        return ""
    path = path.strip().rstrip("/") or "/"
    if path in (WORKSPACE, "."):
        return ""
    home = str(Path.home())
    if path == home or path.startswith(f"{home}/"):
        return "~" + path[len(home) :]
    return strip_workspace(path)


def fmt_duration(ms: int) -> str:
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    minutes, seconds = divmod(ms // 1000, 60)
    return f"{minutes}m{seconds:02d}s"


def _plural(count: int, noun: str, plural: str | None = None) -> str:
    return f"{count:,} {noun if count == 1 else plural or noun + 's'}"


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def description(tool: ToolNode) -> str | None:
    value = tool.input.get("description")
    return value.strip() if isinstance(value, str) and value.strip() else None


# ---------------------------------------------------------------------------
# Row model: what goes on each line
# ---------------------------------------------------------------------------


@dataclass
class RowParts:
    name: str
    facts: list[str | Text] = field(default_factory=list)  # metrics, then the duration;
    # plain strings are drawn muted, Text keeps its own colours
    error: str = ""
    target: str = ""
    suffix: str = ""  # kept whole when line 2 is cut (the shell working directory)
    path_like: bool = False  # cut a long target in the middle, keeping the end
    waiting: bool = False  # a question waiting for an answer


def row_parts(tool: ToolNode) -> RowParts:
    """Split a tool call into the row's pieces (plans/tui-tool-rows.md)."""
    builder = _BUILDERS.get(tool.name, _other)
    parts = builder(tool)
    done = tool.result is not None
    if done and tool.duration_ms:
        parts.facts.append(fmt_duration(tool.duration_ms))
    if not done and not parts.facts:
        parts.facts.append("running")
    if tool.status == "error" and not parts.error:
        parts.error = error_message(tool, parts.target)
    return parts


def error_message(tool: ToolNode, target: str = "") -> str:
    """One line of the error: class prefix dropped, the target not repeated."""
    result = (tool.result or "").strip()
    if tool.name == "shell" and result.startswith("[exit:"):
        # A failed command: its last output line is usually the useful one.
        body = "\n".join(line for line in result.splitlines() if not line.startswith("[exit:"))
        message = _last_line(body)
    elif tool.name == "exec":
        found = re.search(r"^error: (.+)$", result, re.MULTILINE)
        message = found.group(1).strip() if found else _first_line(result)
        return strip_workspace(message)  # exec keeps the class: often it's the message
    else:
        message = _ERROR_CLASS.sub("", _first_line(result))
    message = strip_workspace(message)
    for repeated in {target, strip_workspace(str(tool.input.get("path", "")))} - {""}:
        if message.endswith(f": {repeated}"):
            message = message[: -len(repeated) - 2]
    return message


def _read(tool: ToolNode) -> RowParts:
    target = display_path(tool.input.get("path"))
    offset, limit = tool.input.get("offset"), tool.input.get("limit")
    parts = RowParts("Read", target=target, path_like=True)
    result = tool.result or ""
    if tool.status != "ok":
        if isinstance(offset, int) and isinstance(limit, int):
            parts.target += f":{offset}-{offset + limit - 1}"
        return parts
    head = result[:400]
    image = re.match(r"Image: .*?\((\w+), (\d+)x(\d+)(?:, resized from (\d+)x(\d+))?", head)
    showing = re.search(r"Showing lines (\d+)-(\d+) of (\d+)", head)
    total = re.search(r"\((\d+) lines\)", head)
    if image:
        width, height = image.group(4) or image.group(2), image.group(5) or image.group(3)
        parts.facts.append(f"image {width}×{height}")
    elif showing:
        start, end, of = (int(g) for g in showing.groups())
        parts.facts.append(f"{end - start + 1:,} of {of:,} lines")
        parts.target += f":{start}-{end}"
    elif total:
        parts.facts.append(_plural(int(total.group(1)), "line"))
    return parts


def _write(tool: ToolNode) -> RowParts:
    parts = RowParts("Write", target=display_path(tool.input.get("path")), path_like=True)
    found = re.search(r"\((\d+) lines\)", tool.result or "")
    if tool.status == "ok" and found:
        parts.facts.append(_plural(int(found.group(1)), "line"))
    return parts


def _edit(tool: ToolNode) -> RowParts:
    parts = RowParts("Edit", target=display_path(tool.input.get("path")), path_like=True)
    if tool.status == "ok" and tool.result:
        lines = tool.result.splitlines()
        added = sum(1 for x in lines if x.startswith("+") and not x.startswith("+++"))
        removed = sum(1 for x in lines if x.startswith("-") and not x.startswith("---"))
        parts.facts.append(
            Text.assemble((f"+{added}", theme.SUCCESS), " ", (f"−{removed}", theme.ERROR))
        )
    return parts


def _glob(tool: ToolNode) -> RowParts:
    pattern = str(tool.input.get("pattern", ""))
    root = display_path(tool.input.get("path"))
    parts = RowParts("Glob", target=f"{pattern} in {root}" if root else pattern)
    result = tool.result or ""
    if tool.status == "ok":
        found = re.match(r"(\d+) files?", result)
        if "No files found" in result or not found:
            parts.facts.append("no files")
        else:
            parts.facts.append(_plural(int(found.group(1)), "file"))
    return parts


def _grep(tool: ToolNode) -> RowParts:
    pattern = str(tool.input.get("pattern", ""))
    root = display_path(tool.input.get("path"))
    include = tool.input.get("include")
    target = f'"{pattern}"' + (f" in {root}" if root else "")
    if include:
        target += f" · {include}"
    parts = RowParts("Grep", target=target)
    result = tool.result or ""
    if tool.status == "ok":
        if "No matches found" in result:
            parts.facts.append("no matches")
        else:
            lines = result.splitlines()
            matches = sum(1 for x in lines if "|" in x[:8])
            files = sum(1 for x in lines if x.rstrip().endswith(":") and not x.startswith(" "))
            parts.facts.append(
                f"{_plural(matches, 'match', 'matches')} in {_plural(files, 'file')}"
            )
    return parts


def _code(tool: ToolNode) -> RowParts:
    root = display_path(tool.input.get("path")) or "."
    name = tool.input.get("name")
    parts = RowParts("Code", target=f'"{name}" in {root}' if name else root, path_like=not name)
    result = tool.result or ""
    if tool.status == "ok":
        files = re.match(r"\S+ \((\d+) files", result)
        if "No symbols found" in result:
            parts.facts.append("no symbols")
        elif files and not name:
            parts.facts.append(_plural(int(files.group(1)), "file"))
        else:
            symbols = [
                x
                for x in result.splitlines()
                if x.strip()
                and (("[" in x and "line " in x) or " — " in x)
                and not x.startswith(("Showing ", "…", "No symbols"))
            ]
            parts.facts.append(_plural(len(symbols), "symbol"))
    return parts


def _shell(tool: ToolNode) -> RowParts:
    command = strip_workspace(str(tool.input.get("command", "")))
    summary = description(tool)
    if summary:
        target = strip_workspace(summary)
    else:
        target = f"$ {_first_line(command)}" if command.strip() else "(no command)"
    cwd = display_path(tool.input.get("cwd"))
    parts = RowParts("Shell", target=target, suffix=f" · in {cwd}" if cwd else "")
    result = tool.result
    if result is None:
        return parts
    timeout = _TIMEOUT.search(result)
    if timeout:
        parts.facts.append(f"timed out after {timeout.group(1)}s")
        parts.error = " "  # failed, but the fact says it all
        return parts
    first = result.splitlines()[0] if result else ""
    exit_code = re.fullmatch(r"\[exit: (-?\d+)\]", first.strip())
    if exit_code is None:
        return parts  # not a command result (a validation error, say): no output metrics
    output_lines = len(result.splitlines()) - 1
    if exit_code.group(1) != "0":
        parts.facts.append(f"exit {exit_code.group(1)}")
    parts.facts.append(_plural(output_lines, "line") if output_lines else "no output")
    return parts


def _exec(tool: ToolNode) -> RowParts:
    source = str(tool.input.get("source", ""))
    summary = description(tool)
    target = summary or f"python · {_plural(len(source.splitlines()), 'line')}"
    parts = RowParts("Exec", target=strip_workspace(target))
    if tool.status == "ok" and tool.result is not None:
        returned = len(tool.result.splitlines())
        parts.facts.append(f"returned {_plural(returned, 'line')}" if returned else "no output")
    return parts


def _web_fetch(tool: ToolNode) -> RowParts:
    url = re.sub(r"^https?://", "", str(tool.input.get("url", "")))
    terms = tool.input.get("search_terms")
    parts = RowParts("Fetch", target=url, suffix=f' · "{terms}"' if terms else "")
    if tool.status == "ok" and tool.result is not None:
        parts.facts.append(_plural(len(tool.result.splitlines()), "line"))
    return parts


def _web_search(tool: ToolNode) -> RowParts:
    parts = RowParts("Web search", target=f'"{tool.input.get("query", "")}"')
    result = tool.result or ""
    if tool.status == "ok":
        if "No results found" in result:
            parts.facts.append("no results")
        else:
            count = len([b for b in result.split("\n\n") if b.strip()])
            parts.facts.append(_plural(count, "result"))
    return parts


def _brain_search(tool: ToolNode) -> RowParts:
    parts = RowParts("Brain search", target=f'"{tool.input.get("query", "")}"')
    if tool.status == "ok" and tool.result is not None:
        try:
            count = len(json.loads(tool.result))
        except (json.JSONDecodeError, TypeError):
            count = 0
        parts.facts.append(_plural(count, "result") if count else "no results")
    return parts


def _skill(tool: ToolNode) -> RowParts:
    name = str(tool.input.get("name", ""))
    references = tool.input.get("references")
    refs = [str(r) for r in references] if isinstance(references, list) else []
    file = tool.input.get("file")
    target = name + (f" · {', '.join(refs)}" if refs else f" · {file}" if file else "")
    parts = RowParts("Skill", target=target)
    result = tool.result or ""
    if tool.status == "ok":
        if "already available in earlier tool results" in result:
            parts.facts.append("already loaded")
        elif refs:
            parts.facts.append(_plural(len(refs), "reference"))
        else:
            parts.facts.append(_plural(len(result.splitlines()), "line"))
    return parts


def _question(tool: ToolNode) -> RowParts:
    parts = RowParts("Question", target=str(tool.input.get("question", "")))
    result = tool.result
    if result is None:
        parts.facts.append("waiting for you")
        parts.waiting = True
    elif result.startswith(QUESTION_SELECTED_PREFIX):
        parts.facts.append(f"→ {result.partition(': ')[2]}")
    elif result.startswith(QUESTION_ANSWERED_PREFIX):
        parts.facts.append(f"→ {result.removeprefix(QUESTION_ANSWERED_PREFIX)}")
    elif result == QUESTION_DISMISSED_RESULT:
        parts.facts.append("dismissed")
    return parts


def _task(tool: ToolNode) -> RowParts:
    tasks = tool.input.get("tasks")
    agents = (
        Counter(str(t.get("agent", "default")) for t in tasks if isinstance(t, dict))
        if isinstance(tasks, list)
        else Counter()
    )
    target = ", ".join(f"{n} × {agent}" for agent, n in agents.most_common())
    parts = RowParts("Task", target=target)
    total = sum(agents.values())
    if tool.result is None:
        done = sum(1 for sub in tool.children if sub.status != "running")
        parts.facts.append(f"{done} of {total} done")
    elif tool.result.strip() == "cancelled":
        parts.facts.append("cancelled")
        parts.error = " "
    else:
        parts.facts.append(_plural(total, "agent"))
    return parts


def _other(tool: ToolNode) -> RowParts:
    scalars = [
        f"{key}={json.dumps(value) if isinstance(value, str) else value}"
        for key, value in tool.input.items()
        if isinstance(value, str | int | float | bool)
    ][:3]
    parts = RowParts(tool.name, target=strip_workspace(", ".join(scalars)))
    if tool.status == "ok" and tool.result is not None:
        parts.facts.append(f"{len(tool.result):,} chars")
    return parts


_BUILDERS: dict[str, Callable[[ToolNode], RowParts]] = {
    "read": _read,
    "write": _write,
    "edit": _edit,
    "glob": _glob,
    "grep": _grep,
    "code": _code,
    "shell": _shell,
    "exec": _exec,
    "web_fetch": _web_fetch,
    "web_search": _web_search,
    "brain_search": _brain_search,
    "skill": _skill,
    "question": _question,
    "task": _task,
}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _glyph(tool: ToolNode, parts: RowParts) -> Text:
    if parts.waiting:
        return Text("?", style=f"bold {theme.WARNING}")
    if tool.status == "error" or parts.error:
        return Text("✕", style=f"bold {theme.ERROR}")
    if tool.status == "ok":
        return Text("●", style=f"bold {theme.SUCCESS}")
    return Text("○", style=f"bold {theme.PRIMARY}")


def _cut_middle(text: str, width: int) -> str:
    """Shorten ``text`` to ``width`` by replacing its middle with ``…``, keeping the end."""
    if len(text) <= width:
        return text
    if width <= 1:
        return "…"[:width]
    tail = max(1, (width - 1) * 2 // 3)
    head = width - 1 - tail
    return f"{text[:head]}…{text[-tail:]}"


def render_row(tool: ToolNode, width: int = 120) -> Text:
    """The two lines for ``tool`` at ``width`` columns."""
    parts = row_parts(tool)
    line1 = Text.assemble(_glyph(tool, parts), " ", (parts.name, TOOL_NAME_STYLE))
    for fact in parts.facts:
        line1.append(" · ", style=theme.MUTED)
        if isinstance(fact, Text):
            line1.append_text(fact)
        else:
            line1.append(fact, style=theme.MUTED)
    if parts.error.strip():
        line1.append(" · ", style=theme.MUTED)
        line1.append(parts.error, style=theme.ERROR)
    line1.truncate(width, overflow="ellipsis")

    room = max(1, width - len(CONNECTOR) - len(parts.suffix))
    target = parts.target
    if len(target) > room:
        target = _cut_middle(target, room) if parts.path_like else target[: room - 1] + "…"
    line2 = Text.assemble((CONNECTOR, theme.MUTED), (target, TARGET_STYLE))
    if parts.suffix:
        line2.append(parts.suffix, style=theme.MUTED)
    line2.truncate(width, overflow="ellipsis")
    return Text("\n").join([line1, line2])


class TwoLineToolRow(transcript_view.ToolRow):
    """The attach TUI's tool row, drawn as two lines cut to the row's width."""

    def render(self) -> Text:
        return render_row(self.tool, self.content_region.width or 120)

    def refresh_node(self) -> None:
        self.refresh(layout=True)

    def on_resize(self) -> None:
        self.refresh()


class PlainUserMessage(UserMessage):
    """A user message without the "▶ You" header: just the text beside its left rule."""

    def compose(self) -> ComposeResult:
        yield Static(Text(self._content))


def install() -> Callable[[], None]:
    """Use the prototype's tool rows and user messages until the returned undo is called."""
    previous = transcript_view.ToolRow, transcript_view.UserMessage
    transcript_view.ToolRow = TwoLineToolRow
    transcript_view.UserMessage = PlainUserMessage

    def restore() -> None:
        transcript_view.ToolRow, transcript_view.UserMessage = previous

    return restore

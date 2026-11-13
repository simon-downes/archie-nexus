"""Two-line tool rows: what a tool call's transcript row says (plans/tui-tool-rows.md).

Every tool call is drawn as exactly two lines::

    ● Read · 141 of 880 lines · 4ms
    ╰ archie-nexus/shared/src/archie_shared/transcript.py:120-260

Line 1 is the status glyph, the tool's label, its metrics, the duration and, for a
failed call, the error. Line 2 is what the call acted on, taken from the input, so it is
there while the call runs and the row keeps its height when it finishes.

``tool_row`` builds a ``ToolRowParts`` from a ``ToolNode``: from its structured fields
(``status``, ``duration_ms``, ``result_lines``) and its input, and from the result text
only where no field carries the fact (match counts, a shell's exit code). ``row_lines``
lays the parts out at a width as role-tagged segments, which a client styles; nothing
here depends on a UI library.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal

from archie_shared.tool_summaries import (
    QUESTION_ANSWERED_PREFIX,
    QUESTION_DISMISSED_RESULT,
    QUESTION_SELECTED_PREFIX,
)
from archie_shared.transcript import ToolNode

WORKSPACE = "/workspace"
"""The session container's workspace root, stripped from everything on a row."""

CONTAINER_HOME = "/home/archie"
"""The session container's home directory, shown as ``~``."""

CONNECTOR = "╰ "
"""Starts line 2, under the glyph, so the target lines up with the label."""

SEPARATOR = " · "
ELLIPSIS = "…"

GLYPHS = {"ok": "●", "error": "✕", "waiting": "?"}
"""Glyph characters; a running row shows the caller's spinner frame instead."""

Glyph = Literal["running", "ok", "error", "waiting"]
"""Row state: running (spinner), ok (green ●), error (red ✕), waiting (yellow ?)."""

Tone = Literal["muted", "added", "removed"]
"""Colour of a metric span: muted text, or an edit's green ``+N`` / red ``−N``."""

Role = Literal["glyph", "label", "muted", "added", "removed", "error", "target"]
"""What a laid-out segment is, for the client to style."""

LABELS = {
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
    "tool": "Tool",  # a result whose call was never seen
}

CANCELLED_RESULT = "cancelled"
"""Result the agent loop records for a call interrupted before it finished."""

OTHER_INPUTS = 3
"""Scalar inputs shown on line 2 of a tool without its own rule (plugins, MCP)."""

_ERROR_CLASS = re.compile(r"^(?:[A-Za-z_][\w.]*\.)?[A-Z]\w*(?:Error|Exception): ")
_TIMEOUT = re.compile(r"^\[error: command timed out after (\S+?)s\b", re.MULTILINE)
_EXIT = re.compile(r"\[exit: (-?\d+)\]")
_EXEC_ERROR = re.compile(r"^error: (.+)$", re.MULTILINE)
_READ_TOTAL = re.compile(r"^File: .* \((\d+) lines\)$", re.MULTILINE)
_READ_SHOWING = re.compile(r"^Showing lines (\d+)-(\d+) of (\d+)$", re.MULTILINE)
_READ_IMAGE = re.compile(r"^Image: .*\((\w+), (\d+)x(\d+)(?:, resized from (\d+)x(\d+))?")
_GLOB_SHOWN = re.compile(r"^\d+ files shown of (\d+)")
_GLOB_COUNT = re.compile(r"^(\d+) files?\b")
_GREP_MATCH = re.compile(r"^\s+\d+\| ", re.MULTILINE)
_GREP_FILE = re.compile(r"^\S.*:$", re.MULTILINE)
_GREP_SHOWING = re.compile(r"^Showing \d+ of (\d+) files", re.MULTILINE)
_CODE_TREE = re.compile(r"^\S+/ \((\d+) files?\b")
_CODE_SYMBOL = re.compile(r"\[\w+, line \d+-\d+\]$|^\S+:\d+-\d+ — ", re.MULTILINE)
_CODE_SHOWING = re.compile(r"^Showing \d+ of (\d+) —", re.MULTILINE)
_SEARCH_RESULT = re.compile(r"^\d+\. ", re.MULTILINE)


# --- The model ------------------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    """A run of metric text in one tone."""

    text: str
    tone: Tone = "muted"


Metric = tuple[Span, ...]
"""One metric (``80 lines``, or an edit's ``+4 −1`` in two tones)."""


@dataclass
class ToolRowParts:
    """What goes on a tool row's two lines.

    Attributes:
        glyph: The row's state, which picks the glyph and its colour.
        label: The tool's display name (``Read``, ``Web search``, or a plugin's own name).
        metrics: Outcome facts for line 1, in order (at most two counts).
        duration: ``6ms``/``1.7s``/``2m35s`` once finished, else ``""``.
        error: The failure's one-line message, last on line 1; ``""`` for none.
        target: What the call acted on, for line 2.
        suffix: Kept whole when line 2 is cut (a shell's `` · in <dir>``).
        path_like: Cut a long target in the middle, keeping its end (a path).
    """

    glyph: Glyph
    label: str
    metrics: list[Metric] = field(default_factory=list)
    duration: str = ""
    error: str = ""
    target: str = ""
    suffix: str = ""
    path_like: bool = False


@dataclass
class _Draft:
    """A row while its tool's rule fills it in."""

    target: str = ""
    suffix: str = ""
    path_like: bool = False
    metrics: list[Metric] = field(default_factory=list)
    failed: bool = False  # failed though the result isn't an error (a shell's exit code)
    error: str | None = None  # set by the rule; None takes the result's first line
    timed: bool = True  # show the duration

    def metric(self, text: str) -> None:
        self.metrics.append((Span(text),))


def tool_row(tool: ToolNode) -> ToolRowParts:
    """The two-line row of one tool call (plans/tui-tool-rows.md).

    Args:
        tool: The call, running or finished.

    Returns:
        The row's parts; ``row_lines`` lays them out at a width.
    """
    draft = _Draft()
    _RULES.get(tool.name, _other)(tool, draft)
    label = LABELS.get(tool.name, tool.name)
    result = tool.result
    if result is None:
        glyph: Glyph = "waiting" if tool.name == "question" else "running"
        return ToolRowParts(
            glyph, label, draft.metrics, "", "", draft.target, draft.suffix, draft.path_like
        )
    if tool.status == "error" and result.strip() == CANCELLED_RESULT:
        cancelled: list[Metric] = [(Span("cancelled"),)]
        return ToolRowParts(
            "error", label, cancelled, "", "", draft.target, draft.suffix, draft.path_like
        )
    failed = tool.status == "error" or draft.failed
    error = ""
    if failed:
        error = draft.error if draft.error is not None else _error_text(tool, draft.target)
    duration = format_duration(tool.duration_ms) if draft.timed and tool.duration_ms else ""
    return ToolRowParts(
        "error" if failed else "ok",
        label,
        draft.metrics,
        duration,
        error,
        draft.target,
        draft.suffix,
        draft.path_like,
    )


# --- Formatting helpers -----------------------------------------------------------------


def format_duration(ms: int) -> str:
    """``6ms`` under a second, ``1.7s`` under a minute, ``2m35s`` from a minute."""
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    minutes, seconds = divmod(ms // 1000, 60)
    return f"{minutes}m{seconds:02d}s"


def strip_workspace(text: str) -> str:
    """Drop the workspace root from paths and commands in ``text``.

    ``/workspace/archie-nexus`` becomes ``archie-nexus``; a bare ``/workspace`` becomes
    ``.``. Only whole path components match (``/srv/workspace/x`` is kept).
    """
    text = re.sub(rf"(?<![\w.~/-]){WORKSPACE}/", "", text)
    return re.sub(rf"(?<![\w.~/-]){WORKSPACE}(?![\w./-])", ".", text)


def display_path(path: object, home: str = CONTAINER_HOME) -> str:
    """A path as a row shows it: workspace-relative, ``~`` for home, ``""`` for the root.

    Args:
        path: The input value; anything but a non-blank string gives ``""``.
        home: The home directory to show as ``~``.
    """
    if not isinstance(path, str) or not path.strip():
        return ""
    path = path.strip()
    path = path.rstrip("/") or "/"
    if path in (WORKSPACE, "."):
        return ""
    if path == home or path.startswith(f"{home}/"):
        return "~" + path[len(home) :]
    return strip_workspace(path)


def elide_end(text: str, width: int) -> str:
    """``text`` cut to ``width`` characters, ending in ``…`` when cut."""
    if len(text) <= width:
        return text
    if width <= 0:
        return ""
    return text[: width - 1] + ELLIPSIS


def elide_path(path: str, width: int) -> str:
    """``path`` cut to ``width`` characters by losing its middle, keeping the file name.

    Whole directories go first (``archie-nexus/…/prototype/data.py``), keeping the
    first component and as many trailing ones as fit; a path that still doesn't fit
    loses characters from its middle.
    """
    if len(path) <= width:
        return path
    if width <= 1:
        return ELLIPSIS[: max(0, width)]
    parts = path.split("/")
    if len(parts) > 2:
        head = parts[0]
        for start in range(2, len(parts)):
            candidate = f"{head}/{ELLIPSIS}/{'/'.join(parts[start:])}"
            if len(candidate) <= width:
                return candidate
        candidate = f"{ELLIPSIS}/{parts[-1]}"
        if len(candidate) <= width:
            return candidate
    tail = max(1, (width - 1) * 2 // 3)
    return f"{path[: width - 1 - tail]}{ELLIPSIS}{path[-tail:]}"


def _count(n: int, noun: str, plural: str | None = None) -> str:
    return f"{n:,} {noun if n == 1 else plural or noun + 's'}"


def _first_line(text: object) -> str:
    if not isinstance(text, str):
        return ""
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _result_lines(tool: ToolNode) -> int:
    """Lines in the result: the event's ``result_lines``, else counted (older logs)."""
    if tool.result_lines:
        return tool.result_lines
    return len((tool.result or "").splitlines())


def _error_text(tool: ToolNode, target: str) -> str:
    """One line of a failure: no exception class, no repeat of the target.

    Exec keeps the class, which is often the whole message (``KeyError: 'team'``).
    """
    result = (tool.result or "").strip()
    if tool.name == "exec":
        found = _EXEC_ERROR.search(result)
        return strip_workspace(found.group(1).strip() if found else _first_line(result))
    message = strip_workspace(_ERROR_CLASS.sub("", _first_line(result)))
    raw = tool.input.get("path")
    repeats = {target, target.rpartition(":")[0], display_path(raw)}
    if isinstance(raw, str):
        repeats.add(raw.strip())
    for repeated in sorted(repeats - {""}, key=len, reverse=True):
        if message.endswith(f": {repeated}"):
            return message[: -len(repeated) - 2]
    return message


# --- Rules per tool ----------------------------------------------------------------------


def _read(tool: ToolNode, row: _Draft) -> None:
    row.target = display_path(tool.input.get("path"))
    row.path_like = True
    offset, limit = tool.input.get("offset"), tool.input.get("limit")
    result = tool.result
    if result is None or tool.status != "ok":
        if isinstance(offset, int):
            row.target += f":{offset}" + (
                f"-{offset + limit - 1}" if isinstance(limit, int) else ""
            )
        return
    head = result[:600]
    image = _READ_IMAGE.match(head)
    showing = _READ_SHOWING.search(head)
    total = _READ_TOTAL.search(head)
    if image:
        width, height = image.group(4) or image.group(2), image.group(5) or image.group(3)
        row.metric(f"image {width}×{height}")
    elif showing:
        start, end, of = (int(group) for group in showing.groups())
        row.metric(f"{end - start + 1:,} of {of:,} lines")
        row.target += f":{start}-{end}"
    elif total:
        lines = int(total.group(1))
        if isinstance(offset, int) and 1 < offset <= lines:
            row.metric(f"{lines - offset + 1:,} of {lines:,} lines")
            row.target += f":{offset}-{lines}"
        else:
            row.metric(_count(lines, "line"))
    else:
        row.metric(_count(_result_lines(tool), "line"))


def _write(tool: ToolNode, row: _Draft) -> None:
    row.target = display_path(tool.input.get("path"))
    row.path_like = True
    content = tool.input.get("content")
    if tool.status == "ok" and isinstance(content, str):
        row.metric(_count(len(content.splitlines()), "line"))


def _edit(tool: ToolNode, row: _Draft) -> None:
    row.target = display_path(tool.input.get("path"))
    row.path_like = True
    result = tool.result
    if tool.status != "ok" or not result or "@@" not in result:
        return
    lines = result.splitlines()
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    row.metrics.append((Span(f"+{added}", "added"), Span(" "), Span(f"−{removed}", "removed")))


def _glob(tool: ToolNode, row: _Draft) -> None:
    pattern = str(tool.input.get("pattern", ""))
    root = display_path(tool.input.get("path"))
    row.target = f"{pattern} in {root}" if root else pattern
    result = tool.result
    if tool.status != "ok" or result is None:
        return
    shown = _GLOB_SHOWN.match(result)
    found = _GLOB_COUNT.match(result)
    if shown:
        row.metric(_count(int(shown.group(1)), "file"))
    elif found and int(found.group(1)):
        row.metric(_count(int(found.group(1)), "file"))
    else:
        row.metric("no files")


def _grep(tool: ToolNode, row: _Draft) -> None:
    pattern = str(tool.input.get("pattern", ""))
    root = display_path(tool.input.get("path"))
    include = tool.input.get("include")
    row.target = f'"{pattern}"' + (f" in {root}" if root else "")
    if isinstance(include, str) and include:
        row.target += f" · {include}"
    result = tool.result
    if tool.status != "ok" or result is None:
        return
    if result.startswith("No matches found"):
        row.metric("no matches")
        return
    matches = len(_GREP_MATCH.findall(result))
    showing = _GREP_SHOWING.search(result)
    files = int(showing.group(1)) if showing else len(_GREP_FILE.findall(result))
    row.metric(f"{_count(matches, 'match', 'matches')} in {_count(files, 'file')}")


def _code(tool: ToolNode, row: _Draft) -> None:
    root = display_path(tool.input.get("path")) or "."
    name = tool.input.get("name")
    if isinstance(name, str) and name:
        row.target = f'"{name}" in {root}'
    else:
        row.target = root
        row.path_like = True
    result = tool.result
    if tool.status != "ok" or result is None:
        return
    tree = _CODE_TREE.match(result)
    if result.startswith("No symbols found"):
        row.metric("no symbols")
    elif result.startswith("No files found"):
        row.metric("no files")
    elif tree:
        row.metric(_count(int(tree.group(1)), "file"))
    else:
        showing = _CODE_SHOWING.search(result)
        symbols = int(showing.group(1)) if showing else len(_CODE_SYMBOL.findall(result))
        row.metric(_count(symbols, "symbol"))


def _shell(tool: ToolNode, row: _Draft) -> None:
    command = strip_workspace(_first_line(tool.input.get("command")))
    row.target = f"$ {command}" if command else "$"
    cwd = display_path(tool.input.get("cwd"))
    row.suffix = f" · in {cwd}" if cwd else ""
    result = tool.result
    if result is None:
        return
    timeout = _TIMEOUT.search(result)
    if timeout:
        row.metric(f"timed out after {timeout.group(1)}s")
        row.failed, row.error, row.timed = True, "", False
        return
    first, _, body = result.partition("\n")
    exit_code = _EXIT.fullmatch(first.strip())
    if exit_code is None:
        return  # not a command result (a bad working directory, say): the error says it
    code = int(exit_code.group(1))
    if code:
        row.metric(f"exit {code}")
        row.failed = True
        row.error = strip_workspace(_last_line(body))
        return
    output = _result_lines(tool) - 1 if tool.result_lines else len(body.splitlines())
    row.metric(_count(output, "line") if output > 0 else "no output")


def _exec(tool: ToolNode, row: _Draft) -> None:
    source = tool.input.get("source")
    lines = len(source.splitlines()) if isinstance(source, str) else 0
    row.target = f"python · {_count(lines, 'line')}"
    if tool.status == "ok" and tool.result is not None:
        returned = _result_lines(tool)
        row.metric(f"returned {_count(returned, 'line')}" if returned else "no output")


def _web_fetch(tool: ToolNode, row: _Draft) -> None:
    row.target = re.sub(r"^https?://", "", str(tool.input.get("url", "")))
    row.path_like = True  # keep the host and the page
    terms = tool.input.get("search_terms")
    row.suffix = f' · "{terms}"' if isinstance(terms, str) and terms else ""
    if tool.status == "ok" and tool.result is not None:
        row.metric(_count(_result_lines(tool), "line"))


def _web_search(tool: ToolNode, row: _Draft) -> None:
    row.target = f'"{tool.input.get("query", "")}"'
    result = tool.result
    if tool.status != "ok" or result is None:
        return
    count = len(_SEARCH_RESULT.findall(result))
    row.metric(_count(count, "result") if count else "no results")


def _brain_search(tool: ToolNode, row: _Draft) -> None:
    row.target = f'"{tool.input.get("query", "")}"'
    if tool.status != "ok" or tool.result is None:
        return
    try:
        found = json.loads(tool.result)
    except (json.JSONDecodeError, TypeError):
        found = None
    count = len(found) if isinstance(found, list) else 0
    row.metric(_count(count, "result") if count else "no results")


def _skill(tool: ToolNode, row: _Draft) -> None:
    name = str(tool.input.get("name", ""))
    references = tool.input.get("references")
    refs = [str(ref) for ref in references] if isinstance(references, list) else []
    file = tool.input.get("file")
    if refs:
        row.target = f"{name} · {', '.join(refs)}"
    elif isinstance(file, str) and file:
        row.target = f"{name} · {file}"
    else:
        row.target = name
    result = tool.result
    if tool.status != "ok" or result is None:
        return
    if "already available in earlier tool results" in result:
        row.metric("already loaded")
    elif refs:
        row.metric(_count(len(refs), "reference"))
    else:
        row.metric(_count(_result_lines(tool), "line"))


def _question(tool: ToolNode, row: _Draft) -> None:
    row.target = _first_line(tool.input.get("question"))
    result = tool.result
    if result is None:
        row.metric("waiting for you")
    elif result.startswith(QUESTION_SELECTED_PREFIX):
        row.metric(f"→ {_first_line(result.partition(': ')[2])}")
    elif result.startswith(QUESTION_ANSWERED_PREFIX):
        row.metric(f"→ {_first_line(result.removeprefix(QUESTION_ANSWERED_PREFIX))}")
    elif result == QUESTION_DISMISSED_RESULT:
        row.metric("dismissed")


def _task(tool: ToolNode, row: _Draft) -> None:
    tasks = tool.input.get("tasks")
    agents: Counter[str] = Counter(
        str(task.get("agent") or "default")
        for task in (tasks if isinstance(tasks, list) else [])
        if isinstance(task, dict)
    )
    row.target = ", ".join(f"{n} × {agent}" for agent, n in agents.most_common())
    total = sum(agents.values()) or len(tool.children)
    if tool.result is None:
        done = sum(1 for sub in tool.children if sub.status != "running")
        row.metric(f"{done} of {total} done")
    elif tool.status == "ok":
        row.metric(_count(total, "agent"))


def _other(tool: ToolNode, row: _Draft) -> None:
    row.target = strip_workspace(", ".join(_scalars(tool.input.items())))
    if tool.status == "ok" and tool.result is not None:
        row.metric(f"{len(tool.result):,} chars")


def _scalars(items: Iterable[tuple[str, object]]) -> list[str]:
    shown = []
    for key, value in items:
        if isinstance(value, str):
            shown.append(f"{key}={json.dumps(_first_line(value) or value, ensure_ascii=False)}")
        elif isinstance(value, bool | int | float):
            shown.append(f"{key}={json.dumps(value)}")
        if len(shown) == OTHER_INPUTS:
            break
    return shown


_RULES: dict[str, Callable[[ToolNode, _Draft], None]] = {
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


# --- Layout ------------------------------------------------------------------------------


Segment = tuple[str, Role]
"""Laid-out text and what it is."""


def row_lines(
    parts: ToolRowParts, width: int, spinner: str = "⠋"
) -> tuple[list[Segment], list[Segment]]:
    """Lay a row out as two lines of at most ``width`` characters.

    Line 1 is cut at its end (the error goes first). On line 2 the target gives way so
    the suffix stays whole: a path loses its middle, other text its end.

    Args:
        parts: The row.
        width: Columns available.
        spinner: The glyph of a running row (the animation's current frame).

    Returns:
        Each line as ``(text, role)`` segments.
    """
    glyph = spinner if parts.glyph == "running" else GLYPHS[parts.glyph]
    first: list[Segment] = [(glyph, "glyph"), (" ", "muted"), (parts.label, "label")]
    for metric in parts.metrics:
        first.append((SEPARATOR, "muted"))
        first.extend((span.text, span.tone) for span in metric)
    if parts.duration:
        first += [(SEPARATOR, "muted"), (parts.duration, "muted")]
    if parts.error:
        first += [(SEPARATOR, "muted"), (parts.error, "error")]

    room = width - len(CONNECTOR) - len(parts.suffix)
    target = parts.target
    if len(target) > room:
        target = elide_path(target, room) if parts.path_like else elide_end(target, room)
    second: list[Segment] = [(CONNECTOR, "muted"), (target, "target")]
    if parts.suffix:
        second.append((parts.suffix, "muted"))
    return _cut(first, width), _cut(second, width)


def _cut(segments: list[Segment], width: int) -> list[Segment]:
    """Segments cut to ``width`` characters, the last kept one ending in ``…``."""
    if sum(len(text) for text, _ in segments) <= width:
        return [segment for segment in segments if segment[0]]
    kept: list[Segment] = []
    used = 0
    for text, role in segments:
        if used + len(text) >= width:
            kept.append((text[: max(0, width - used - 1)] + ELLIPSIS, role))
            break
        kept.append((text, role))
        used += len(text)
    return [segment for segment in kept if segment[0]] if width > 0 else []


def row_text(tool: ToolNode, width: int = 120, spinner: str = "⠋") -> str:
    """The row of ``tool`` as two plain-text lines (tests, logs, plain clients)."""
    first, second = row_lines(tool_row(tool), width, spinner)
    return "\n".join("".join(text for text, _ in line) for line in (first, second))

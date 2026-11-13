"""Where a session works, worked out from its tool calls (plan 084 D26, D27).

No agent cooperation and no model. Each root ``tool_call`` gives candidate paths and a
rank (:func:`candidate`):

- ``shell``: the directories the command runs in. It starts in ``cwd`` (default
  ``/workspace``; relative to ``/workspace``), then follows each ``cd`` in order
  (commands split on ``&&``, ``;``, ``||`` and newlines, heredoc bodies ignored),
  stopping at a target with ``$``, ``~`` or a backtick, since the log can't say where
  that leads. ``git -C <dir>`` adds ``<dir>``. A command that runs ``git`` ranks
  :data:`RANK_GIT`, any other :data:`RANK_OTHER`.
- ``edit`` and ``write``: ``path``, :data:`RANK_EDIT`.
- ``read``, ``grep``, ``glob`` and ``code``: ``path``, :data:`RANK_OTHER`.

Relative paths are relative to ``/workspace``; paths outside it are dropped. A call is
only a candidate once its ``tool_result`` has been seen, since ``git worktree add X &&
cd X`` only leads somewhere after it ran.

:class:`LocationState` holds a session's calls. :meth:`LocationState.best` picks the
location: the latest turn with a candidate that resolves wins, and within it git
shells, then edits and writes, then the rest, newest first; earlier turns are tried
only when it has none. Subagent calls count only when no root call resolves.

Tool calls are untrusted and :func:`candidate` runs on the event loop, so its work is
bounded to a few milliseconds: only the first :data:`MAX_COMMAND_CHARS` and
:data:`MAX_SEGMENTS` segments of a command are parsed, a call keeps its latest
:data:`MAX_CALL_PATHS` paths, and paths longer than PATH_MAX allows
(``workspaces.MAX_PATH_CHARS``, ``workspaces.MAX_PATH_COMPONENTS``) are dropped. A
session keeps its latest :data:`MAX_ENTRIES` eligible calls, and a location job
resolves at most :data:`MAX_LOOKUPS` distinct paths.

Everything here is pure apart from :func:`scan_log`, which reads a session log.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from archie_shared.workspaces import SessionLocation

from archie_orchestrator.docker import CONTAINER_WORKSPACE_DIR as WORKSPACE
from archie_orchestrator.workspaces import MAX_PATH_CHARS, MAX_PATH_COMPONENTS

RANK_GIT, RANK_EDIT, RANK_OTHER = 0, 1, 2
"""Candidate ranks, best first."""

MAX_PENDING = 1000
"""Calls kept waiting for their result; the oldest are dropped past this."""

MAX_COMMAND_CHARS = 16 * 1024
"""Characters of a shell command that are parsed; the segment cut short is dropped."""

MAX_SEGMENTS = 256
"""Segments (``&&``, ``;``, ``||``, newline) of a shell command that are parsed."""

MAX_CALL_PATHS = 16
"""Paths kept per call (the latest; :meth:`LocationState.best` tries them newest first)."""

MAX_ENTRIES = 200
"""Eligible calls kept per session; the oldest are dropped past this."""

MAX_LOOKUPS = 128
"""Distinct paths :meth:`LocationState.best` resolves before giving up."""

PATH_TOOLS: Mapping[str, int] = {
    "edit": RANK_EDIT,
    "write": RANK_EDIT,
    "read": RANK_OTHER,
    "grep": RANK_OTHER,
    "glob": RANK_OTHER,
    "code": RANK_OTHER,
}
"""Tools whose ``path`` input is a candidate, with its rank."""

_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
_UNRESOLVABLE = ("$", "~", "`")


@dataclass(frozen=True)
class Candidate:
    """The paths one tool call worked in, oldest first, and their rank."""

    rank: int
    paths: tuple[str, ...]


# ---------------------------------------------------------------------------
# Candidates from one call
# ---------------------------------------------------------------------------


def candidate(name: object, tool_input: object) -> Candidate | None:
    """The candidate of one tool call, or None if it names no path in the workspace.

    ``tool_input`` is untrusted: unknown keys are ignored and values of the wrong type
    give no path.
    """
    if not isinstance(name, str) or not isinstance(tool_input, Mapping):
        return None
    if name == "shell":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return None
        paths, runs_git = shell_paths(command, tool_input.get("cwd"))
        rank = RANK_GIT if runs_git else RANK_OTHER
    elif name in PATH_TOOLS:
        path = _container_path(WORKSPACE, tool_input.get("path"))
        paths = [path] if path is not None else []
        rank = PATH_TOOLS[name]
    else:
        return None
    inside = tuple(p for p in paths if _in_workspace(p))
    return Candidate(rank, inside) if inside else None


def shell_paths(command: str, cwd: object = None) -> tuple[list[str], bool]:
    """Directories a shell command runs in or points git at, and whether it runs git.

    The directories are container paths in order. The walk starts at ``cwd``
    (``/workspace`` when missing or empty) and follows ``cd`` targets in order; a
    target with ``$``, ``~`` or a backtick (or ``cd`` alone, or ``cd -``) ends it.
    ``git -C <dir>`` targets are added relative to the current directory. A command
    runs git when a command word of one of its segments is ``git``. Segments that
    don't parse as shell words are skipped.

    Bounded for untrusted input: only the first :data:`MAX_COMMAND_CHARS` are parsed
    (dropping the segment the cut falls in, so a truncated path isn't taken) and of
    them the first :data:`MAX_SEGMENTS` segments, at most the latest
    :data:`MAX_CALL_PATHS` directories are returned, and a target past the path caps
    ends the walk like an unresolvable one.
    """
    current = WORKSPACE
    if isinstance(cwd, str) and cwd.strip():
        start = _container_path(WORKSPACE, cwd.strip())
        if start is None:
            return [], False
        current = start
    paths: deque[str] = deque([current], maxlen=MAX_CALL_PATHS)
    runs_git = False
    walking = True
    segments = _segments(_without_heredocs(command[:MAX_COMMAND_CHARS]))
    if len(command) > MAX_COMMAND_CHARS:
        segments = segments[:-1]
    for segment in segments[:MAX_SEGMENTS]:
        if runs_git and not walking:
            break  # nothing left to learn
        try:
            words = shlex.split(segment.lstrip("({ \t"), comments=True)
        except ValueError:
            continue
        words = _command_words(words)
        if not words:
            continue
        if _is_git(words[0]):
            runs_git = True
            if walking:
                paths.extend(_git_dirs(words, current))
        elif words[0] in ("cd", "pushd") and walking:
            args = [word for word in words[1:] if word != "--"]
            target = args[0].rstrip(")") if args else ""
            moved = None if target in ("", "-") else _container_path(current, target)
            if moved is None or _unresolvable(target):
                walking = False  # the rest runs somewhere the log can't tell
                continue
            current = moved
            paths.append(current)
    return list(paths), runs_git


_PREFIXES = frozenset({"sudo", "env", "command", "time", "nohup", "exec"})


def _command_words(words: list[str]) -> list[str]:
    """``words`` from the command word on: ``VAR=value`` and ``sudo``-style prefixes dropped."""
    i = 0
    while i < len(words) and (words[i] in _PREFIXES or _ASSIGNMENT.match(words[i])):
        i += 1
    return words[i:]


def _is_git(word: str) -> bool:
    return word == "git" or word.endswith("/git")


def _git_dirs(words: list[str], current: str) -> list[str]:
    """The ``git -C`` targets of one git command (chained ``-C`` options combine)."""
    found: list[str] = []
    directory = current
    j = 1
    while j < len(words) and words[j].startswith("-"):
        option = words[j]
        if option == "-C" and j + 1 < len(words):
            target = words[j + 1]
            moved = None if _unresolvable(target) else _container_path(directory, target)
            if moved is None:
                break
            directory = moved
            found.append(directory)
            j += 2
        elif option in ("-c", "--git-dir", "--work-tree", "--namespace"):
            j += 2
        else:
            j += 1
    return found


def _unresolvable(text: str) -> bool:
    return any(c in text for c in _UNRESOLVABLE)


def _segments(command: str) -> list[str]:
    """``command`` split on unquoted ``&&``, ``||``, ``;`` and newlines."""
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        char = command[i]
        if quote is not None:
            current.append(char)
            if char == "\\" and quote == '"' and i + 1 < len(command):
                current.append(command[i + 1])
                i += 1
            elif char == quote:
                quote = None
        elif char == "\\" and i + 1 < len(command):
            current.append(command[i : i + 2])
            i += 1
        elif char in "'\"":
            quote = char
            current.append(char)
        elif char in ";\n" or command.startswith(("&&", "||"), i):
            parts.append("".join(current))
            current = []
            if char in "&|":
                i += 1
        else:
            current.append(char)
        i += 1
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _without_heredocs(command: str) -> str:
    """``command`` with heredoc bodies removed (the lines up to each delimiter)."""
    if "<<" not in command:
        return command
    kept: list[str] = []
    waiting: list[tuple[str, bool]] = []  # (delimiter, tabs stripped)
    for line in command.split("\n"):
        if waiting:
            delimiter, strip_tabs = waiting[0]
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                waiting.pop(0)
            continue
        kept.append(line)
        waiting.extend(
            (match.group(3), match.group(1) == "-")
            for match in _HEREDOC.finditer(line)
            if not line.startswith("<<<", match.start())
        )
    return "\n".join(kept)


def _container_path(base: str, value: object) -> str | None:
    """``value`` as a normalised absolute container path, relative ones from ``base``.

    None for a path past PATH_MAX-style caps (:data:`MAX_PATH_CHARS` characters or
    :data:`MAX_PATH_COMPONENTS` components), which no real call could have used.
    """
    if not isinstance(value, str) or not value or "\x00" in value or _unresolvable(value[:1]):
        return None
    if len(value) > MAX_PATH_CHARS:
        return None
    path = posixpath.normpath(posixpath.join(base, value))
    if len(path) > MAX_PATH_CHARS or path.count("/") > MAX_PATH_COMPONENTS:
        return None
    return path


def _in_workspace(path: str) -> bool:
    return path == WORKSPACE or path.startswith(WORKSPACE + "/")


# ---------------------------------------------------------------------------
# A session's candidates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    """A call whose result has been seen."""

    order: int
    turn: int
    root: bool
    candidate: Candidate


class LocationState:
    """One session's tool calls that can place it, fed in log order.

    Calls are keyed by ``(scope, tool_use_id)``; a call becomes eligible when its
    result arrives (:meth:`on_result`). Feeding the same call twice (a log scan that
    overlaps the stream) keeps the first.

    Attributes:
        from_root: Whether the last :meth:`best` came from a root call (False before
            the first, or when only subagent calls resolved).
    """

    def __init__(self) -> None:
        self._pending: dict[tuple[str | None, str], tuple[int, bool, Candidate]] = {}
        # Every job re-resolves the kept calls until one resolves, so a session that
        # never touches a repo would make each result cost its whole history. Two caps
        # bound that: only the latest MAX_ENTRIES calls are kept, and best() resolves
        # at most MAX_LOOKUPS distinct paths (a deep path costs milliseconds). Caps
        # rather than a negative-lookup cache: nothing goes stale, so a repo created
        # later (git init, worktree add) is still found on the next job. What they drop
        # is what best() tries last: the oldest calls (though a turn of more than
        # MAX_ENTRIES calls can lose its early git shells) and the lowest-ranked paths.
        self._entries: deque[_Entry] = deque(maxlen=MAX_ENTRIES)
        self._seen: set[tuple[str | None, str]] = set()
        self._order = 0
        self.from_root = False

    def on_call(
        self, scope: str | None, tool_use_id: object, name: object, tool_input: object, turn: object
    ) -> bool:
        """Note a ``tool_call``; True if it is a candidate waiting for its result."""
        if not isinstance(tool_use_id, str):
            return False
        key = (scope, tool_use_id)
        if key in self._seen or key in self._pending:
            return False
        found = candidate(name, tool_input)
        if found is None:
            return False
        self._pending[key] = (turn if isinstance(turn, int) else 0, scope is None, found)
        if len(self._pending) > MAX_PENDING:
            del self._pending[next(iter(self._pending))]
        return True

    def on_result(self, scope: str | None, tool_use_id: object) -> bool:
        """Note a ``tool_result``; True if it made a candidate eligible."""
        if not isinstance(tool_use_id, str):
            return False
        key = (scope, tool_use_id)
        pending = self._pending.pop(key, None)
        if pending is None:
            return False
        turn, root, found = pending
        self._seen.add(key)
        self._order += 1
        self._entries.append(_Entry(self._order, turn, root, found))
        return True

    def snapshot(self) -> LocationState:
        """A copy whose :meth:`best` can run in a thread while this one is fed."""
        copy = LocationState()
        copy._entries = deque(self._entries, maxlen=MAX_ENTRIES)
        return copy

    @property
    def has_candidates(self) -> bool:
        """True once any call is eligible."""
        return bool(self._entries)

    def best(
        self, resolve: Callable[[str], SessionLocation | None]
    ) -> tuple[SessionLocation | None, bool]:
        """The session's location and whether a root call gave it (D26).

        ``resolve`` maps a container path to a location (None when it isn't in a repo);
        each path is resolved at most once per call, and after :data:`MAX_LOOKUPS`
        distinct paths have not resolved the session has no location. Within one call
        its paths are tried newest first.
        """
        tried: set[str] = set()
        for root, path in self._ordered():
            if path in tried:
                continue  # it didn't resolve the first time
            if len(tried) >= MAX_LOOKUPS:
                break
            tried.add(path)
            found = resolve(path)
            if found is not None:
                self.from_root = root
                return found, root
        self.from_root = False
        return None, False

    def _ordered(self) -> Iterator[tuple[bool, str]]:
        """``(root, path)`` for every kept path, in the order :meth:`best` tries them."""
        for root in (True, False):
            entries = [entry for entry in self._entries if entry.root is root]
            # Turns by their latest call (so a turn counter that restarts can't put an old
            # turn first); within a turn by rank, then newest first.
            latest: dict[int, int] = {}
            for entry in entries:
                latest[entry.turn] = entry.order
            entries.sort(key=lambda e: (-latest[e.turn], e.candidate.rank, -e.order))
            for entry in entries:
                for path in reversed(entry.candidate.paths):
                    yield root, path


def scan_log(path: Path) -> LocationState:
    """A session's :class:`LocationState` from its log.

    Only lines that mention ``tool_call`` or ``tool_result`` are parsed, as plain JSON
    by ``type`` (like ``titles.read_title_input``), so logs in older shapes still read.

    Raises:
        OSError: If the log can't be read.
    """
    state = LocationState()
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if '"tool_call"' not in line and '"tool_result"' not in line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            scope = data.get("scope")
            if scope is not None and not isinstance(scope, str):
                continue
            kind = data.get("type")
            if kind == "tool_call":
                state.on_call(
                    scope,
                    data.get("tool_use_id"),
                    data.get("name"),
                    data.get("input"),
                    data.get("turn"),
                )
            elif kind == "tool_result":
                state.on_result(scope, data.get("tool_use_id"))
    return state

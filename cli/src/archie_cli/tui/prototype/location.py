"""Where a session works: repo, worktree and branch, worked out from its tool calls.

No agent cooperation and no model. The steps:

1. Collect candidate paths from the session's root tool calls, in order: a shell
   call's ``cwd``, its ``cd`` targets and ``git -C`` paths, edit and write paths, and
   read, grep, glob and code paths.
2. Rank them: paths from shell calls that run ``git`` first, then edits and writes,
   then the rest. Within a rank the most recent path wins.
3. Walk up from the path to the first ``.git`` at or below the workspace root. A
   ``.git`` directory is a repo; a ``.git`` file is a worktree, and its ``gitdir:``
   line points back into the main repo. Walking up covers both layouts: repos in
   subdirectories of ``/workspace``, and ``/workspace`` itself being a repo.
4. Read ``HEAD`` for the branch (a file read, not a git process). It is re-read on
   refresh, so a branch switch shows up.

Paths in the log are container paths under ``/workspace``. On the host, the
workspace is ``<global.workspace_root>/<workspace>``; inside an Archie session it is
``/workspace`` itself, for the session's own workspace only.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

WORKSPACE = "/workspace"

_QUOTED = r"(\"[^\"]+\"|'[^']+'|[^\s;&|]+)"
_CD = re.compile(rf"(?:^|&&|;|\|\||\n)\s*cd\s+{_QUOTED}")
_GIT_C = re.compile(rf"\bgit\s+-C\s+{_QUOTED}")
_GIT = re.compile(r"(?:^|[\s;&|(])git\s")

RANK_GIT, RANK_EDIT, RANK_OTHER = 0, 1, 2


@dataclass
class Location:
    """A session's repo, its worktree (if any) and the branch checked out there."""

    repo: str
    worktree: str = ""  # the worktree's directory name; "" for a main checkout
    head: Path | None = None  # the HEAD file the branch is read from
    branch: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        """Re-read the branch: the name, a short hash when detached, or ``?``."""
        try:
            content = self.head.read_text().strip() if self.head else ""
        except OSError:
            content = ""
        if content.startswith("ref: refs/heads/"):
            self.branch = content.removeprefix("ref: refs/heads/")
        else:
            self.branch = content[:8] or "?"


def workspace_dir(workspace: str) -> Path | None:
    """Where ``workspace`` lives on this machine, or None if it isn't here."""
    from archie_shared.schemas import expand_workspace_root, load_nexus_config

    try:
        host = expand_workspace_root(load_nexus_config()) / workspace
    except Exception:  # noqa: BLE001 — a bad config just means no host lookup
        host = None
    if host is not None and host.is_dir():
        return host
    own = os.environ.get("ARCHIE_SESSION_ID", "")
    if own.startswith(f"{workspace}-") and Path(WORKSPACE).is_dir():
        return Path(WORKSPACE)
    return None


def _unquote(text: str) -> str:
    return text[1:-1] if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'" else text


def candidates(log_path: Path) -> list[tuple[int, str]]:
    """``(rank, path)`` for each path in the session's root tool calls, in log order."""
    found: list[tuple[int, str]] = []
    try:
        lines = log_path.open(encoding="utf-8")
    except OSError:
        return found
    with lines:
        for line in lines:
            if '"tool_call"' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") != "tool_call" or event.get("scope") is not None:
                continue
            found.extend(_paths(event.get("name"), event.get("input") or {}))
    return found


def _paths(name: object, tool_input: dict) -> list[tuple[int, str]]:
    if name == "shell":
        command = str(tool_input.get("command", ""))
        rank = RANK_GIT if _GIT.search(command) else RANK_OTHER
        cwd = str(tool_input.get("cwd") or "")
        targets = [_unquote(t) for t in _CD.findall(command) + _GIT_C.findall(command)]
        paths = [cwd] if cwd else []
        for target in targets:
            if "$" in target or target.startswith("~"):
                continue  # variables and home paths can't be resolved from the log
            paths.append(target if target.startswith("/") or not cwd else f"{cwd}/{target}")
        return [(rank, p) for p in paths]
    path = tool_input.get("path")
    if not isinstance(path, str) or not path:
        return []
    if name in ("edit", "write"):
        return [(RANK_EDIT, path)]
    if name in ("read", "grep", "glob", "code"):
        return [(RANK_OTHER, path)]
    return []


def _local(path: str, root: Path) -> Path | None:
    """A logged container path as a path under ``root``; None if outside the workspace."""
    if path == WORKSPACE or path.startswith(f"{WORKSPACE}/"):
        return root / path[len(WORKSPACE) :].lstrip("/")
    if path.startswith(("/", "~")):
        return None  # /tmp, /opt, the home directory: not part of the workspace
    return root / path


def resolve(path: str, root: Path, workspace: str) -> Location | None:
    """The repo or worktree containing ``path``, walking up no further than ``root``."""
    local = _local(path, root)
    if local is None:
        return None
    for directory in (local, *local.parents):
        if directory != root and root not in directory.parents:
            return None
        git = directory / ".git"
        name = workspace if directory == root else directory.name
        if git.is_dir():
            return Location(repo=name, head=git / "HEAD")
        if git.is_file():
            return _worktree(git, directory.name, root)
    return None


def _worktree(git_file: Path, name: str, root: Path) -> Location | None:
    try:
        line = git_file.read_text().strip()
    except OSError:
        return None
    gitdir = line.removeprefix("gitdir:").strip()
    local = _local(gitdir, root) if gitdir.startswith(WORKSPACE) else Path(gitdir)
    if local is None:
        return None
    parts = local.parts
    repo = parts[parts.index(".git") - 1] if ".git" in parts[1:] else "?"
    return Location(repo=repo, worktree=name, head=local / "HEAD")


def locate(log_path: Path, workspace: str, root: Path | None = None) -> Location | None:
    """Where the session in ``log_path`` works, or None if no repo can be found."""
    root = root if root is not None else workspace_dir(workspace)
    if root is None:
        return None
    found = candidates(log_path)
    for rank in (RANK_GIT, RANK_EDIT, RANK_OTHER):
        for candidate_rank, path in reversed(found):
            if candidate_rank == rank:
                location = resolve(path, root, workspace)
                if location is not None:
                    return location
    return None

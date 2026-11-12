"""Workspace descriptors, as served by ``/api/workspaces``.

A workspace is a directory directly under ``global.workspace_root``; a session mounts
one at ``/workspace``. Its repos are the workspace directory itself and its direct
children when they hold a git repository. Paths are relative to the workspace
directory (``""`` is the workspace itself), so a client can match them against the
``/workspace/...`` paths in session logs without knowing the host layout.

The git fields come from reading git's files, not from running git, so they are cheap
enough to work out on every request, and nothing a session writes into a repo's
config is ever executed.
"""

import msgspec


class Worktree(msgspec.Struct):
    """A linked worktree of a repo (``git worktree add``).

    Attributes:
        path: Location relative to the workspace, or the recorded absolute host path
            when the worktree is outside the workspace.
        branch: Checked-out branch; None when HEAD is detached.
        commit: Commit hash HEAD resolves to; None when it can't be resolved.
        missing: True when the worktree's checkout no longer exists (git would call
            it prunable).
    """

    path: str
    branch: str | None = None
    commit: str | None = None
    missing: bool = False


class Repo(msgspec.Struct):
    """A git repository in a workspace: the workspace itself or a direct child.

    Attributes:
        path: Location relative to the workspace; ``""`` for the workspace itself.
        origin: URL of the ``origin`` remote as configured; None without one.
        branch: Checked-out branch; None when HEAD is detached.
        commit: Commit hash HEAD resolves to; None on a branch with no commits yet, or
            when the refs can't be read.
        worktrees: Linked worktrees, sorted by path.
    """

    path: str
    origin: str | None = None
    branch: str | None = None
    commit: str | None = None
    worktrees: list[Worktree] = msgspec.field(default_factory=list)


class Workspace(msgspec.Struct):
    """One workspace directory and its repos.

    Attributes:
        name: Directory name under ``workspace_root``; the ``workspace`` of its sessions.
        live_sessions: Sessions in this workspace that haven't ended.
        last_session_at: ISO 8601 UTC start time of its newest session; None if it
            has never had one.
        repos: Repos sorted by path (the workspace itself first); empty when there are
            none.
    """

    name: str
    live_sessions: int = 0
    last_session_at: str | None = None
    repos: list[Repo] = msgspec.field(default_factory=list)

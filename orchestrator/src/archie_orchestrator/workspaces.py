"""Workspaces and their git repos, read from the filesystem on every request.

A workspace is a directory directly under ``global.workspace_root``. Its repos are the
workspace directory itself and its direct children that hold a git repository; linked
worktrees are listed under the repo they belong to. See ``archie_shared.workspaces``
for the descriptors.

Nothing here runs ``git``. Sessions can write anything inside their workspace,
including ``.git/config``, and git executes commands named there (``core.fsmonitor``,
filters) for ordinary commands such as ``git status``. Running git on the host would
hand that to the session. Reading the files instead covers what's needed: ``HEAD``,
the refs, the ``origin`` URL and the worktree records.

The files are untrusted too, so every read is confined to the workspace directory
(after resolving symlinks), refuses anything but a regular file (a FIFO would block
the worker thread) and is size-capped. Worktree checkouts outside the workspace are
only checked for existence. Unreadable or malformed files give None fields, or leave
out the repo or worktree they describe; they never fail the request. Repos and
worktrees are capped (:data:`MAX_REPOS`, :data:`MAX_WORKTREES`) so a session can't
make a scan arbitrarily slow.

Recorded paths (a ``.git`` file's ``gitdir:`` line, a worktree's ``gitdir`` file) are
container paths under ``/workspace`` when git ran inside a session, and host paths
when it ran on the host; container paths are mapped to the host workspace directory.
"""

import logging
import os
import re
import stat
from pathlib import Path
from typing import BinaryIO, NamedTuple

from archie_shared.workspaces import Repo, Workspace, Worktree

from archie_orchestrator.docker import CONTAINER_WORKSPACE_DIR as CONTAINER_WORKSPACE

MAX_FILE_BYTES = 64 * 1024
"""Largest ``HEAD``, ref, ``gitdir`` or ``config`` file that is read."""

MAX_PACKED_REFS_BYTES = 8 * 1024 * 1024
"""Bytes of ``packed-refs`` searched for a branch; past them, its commit is None."""

MAX_REPOS = 100
"""Most repos listed per workspace."""

MAX_WORKTREES = 100
"""Most worktrees listed per repo."""

REFTABLE_HEAD = "refs/heads/.invalid"
"""The placeholder HEAD of a repo whose refs are in a reftable."""

log = logging.getLogger(__name__)

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_HELPER = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*")
_SECTION = re.compile(r'\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\\n]|\\.)*)")?\s*\]')


def resolve_workspace(root: Path, name: str) -> Path:
    """The resolved directory of workspace ``name`` under ``root``.

    Args:
        root: The expanded ``workspace_root``.
        name: Workspace directory name.

    Returns:
        The workspace directory, symlinks resolved.

    Raises:
        ValueError: If ``name`` isn't a single directory name, resolves outside
            ``root``, or isn't an existing directory.
    """
    # Reject separators and traversal so a user-supplied name can't escape root.
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        raise ValueError(f"Invalid workspace name '{name}': must be a single directory name.")
    workspace_path = root / name
    resolved = workspace_path.resolve()
    root_resolved = root.resolve()
    if resolved == root_resolved or not resolved.is_relative_to(root_resolved):
        raise ValueError(f"Invalid workspace name '{name}': resolves outside workspace_root.")
    if not resolved.is_dir():
        raise ValueError(f"Workspace '{name}' not found under {root}.\nExpected: {workspace_path}")
    return resolved


def list_workspaces(root: Path) -> list[Workspace]:
    """Every workspace under ``root`` with its repos, sorted by name.

    Hidden directories are skipped, as are symlinks that resolve outside ``root``.
    ``live_sessions`` and ``last_session_at`` are left at their defaults for the
    caller to fill. A missing or unreadable ``root`` gives an empty list.
    """
    try:
        names = sorted(entry.name for entry in os.scandir(root) if not entry.name.startswith("."))
    except OSError:
        return []
    workspaces = []
    for name in names:
        try:
            directory = resolve_workspace(root, name)
        except (ValueError, OSError):
            continue
        workspaces.append(Workspace(name=name, repos=scan_repos(directory)))
    return workspaces


def read_workspace(root: Path, name: str) -> Workspace:
    """One workspace with its repos.

    Raises:
        ValueError: As :func:`resolve_workspace`.
    """
    return Workspace(name=name, repos=scan_repos(resolve_workspace(root, name)))


def scan_repos(directory: Path) -> list[Repo]:
    """The repos in a workspace: the directory itself and its direct children.

    Hidden children and symlinks are skipped. A child whose ``.git`` file points at a
    linked worktree's git directory is not a repo of its own; it appears in its main
    repo's ``worktrees``. At most :data:`MAX_REPOS` repos are returned, and a repo that
    can't be read at all is left out.

    Args:
        directory: The workspace directory.
    """
    reader = _Reader(directory)
    candidates = [reader.workspace]
    try:
        children = sorted(os.scandir(reader.workspace), key=lambda entry: entry.name)
    except OSError:
        children = []
    for entry in children:
        try:
            if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False):
                candidates.append(Path(entry.path))
        except OSError:
            continue
    repos: list[Repo] = []
    for path in candidates:
        if len(repos) >= MAX_REPOS:
            break
        try:
            repo = reader.repo(path)
        except (OSError, ValueError) as exc:  # session-written files: NUL bytes, modes
            log.debug("Skipping unreadable repo %s: %s", path, exc)
            continue
        if repo is not None:
            repos.append(repo)
    return repos


class _Head(NamedTuple):
    """What a HEAD file says: the branch, the ref to resolve, or a detached commit."""

    branch: str | None = None
    ref: str | None = None
    commit: str | None = None


class _Reader:
    """Confined reads of git files inside one workspace directory."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(os.path.realpath(workspace))

    # -- files ---------------------------------------------------------------

    def _confined(self, path: Path) -> Path | None:
        """``path`` with symlinks resolved when that is inside the workspace, else None."""
        try:
            real = Path(os.path.realpath(path))
        except (OSError, ValueError):
            return None
        return real if real.is_relative_to(self.workspace) else None

    def _open(self, path: Path) -> BinaryIO | None:
        """A confined regular file opened for reading, or None.

        The last component isn't followed and FIFOs don't block the open. Symlinks in
        earlier components are resolved by the confinement check, so a session that
        swaps a directory for a symlink between the check and the open could still
        steer one read outside the workspace; only branch names, commit hashes and an
        ``origin`` URL are ever taken from the content.
        """
        real = self._confined(path)
        if real is None:
            return None
        try:
            fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except (OSError, ValueError):
            return None
        handle = os.fdopen(fd, "rb")
        try:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                return handle
        except OSError:
            pass
        handle.close()
        return None

    def read(self, path: Path, limit: int = MAX_FILE_BYTES) -> str | None:
        """A regular file's text, or None (outside, missing, special, too big)."""
        handle = self._open(path)
        if handle is None:
            return None
        with handle:
            try:
                data = handle.read(limit + 1)
            except OSError:
                return None
        if len(data) > limit:
            return None
        return data.decode("utf-8", errors="replace")

    def is_dir(self, path: Path) -> bool:
        try:
            return self._confined(path) is not None and path.is_dir()
        except (OSError, ValueError):
            return False

    def host_path(self, recorded: str, base: Path) -> Path:
        """Map a path recorded by git to the host: relative, absolute or container.

        A path already inside the workspace is kept, so a host whose workspace itself
        lives under ``/workspace`` isn't remapped.
        """
        path = Path(os.path.normpath(base / recorded))
        if path.is_relative_to(self.workspace):
            return path
        if recorded == CONTAINER_WORKSPACE or recorded.startswith(CONTAINER_WORKSPACE + "/"):
            mapped = str(self.workspace) + recorded.removeprefix(CONTAINER_WORKSPACE)
            return Path(os.path.normpath(mapped))
        return path

    def relative(self, path: Path) -> str:
        """``path`` relative to the workspace (``""`` for itself), else as given."""
        if path == self.workspace:
            return ""
        if path.is_relative_to(self.workspace):
            return path.relative_to(self.workspace).as_posix()
        return str(path)

    # -- repos ---------------------------------------------------------------

    def repo(self, checkout: Path) -> Repo | None:
        """The repo checked out at ``checkout``, or None if it isn't one."""
        dotgit = checkout / ".git"
        try:
            mode = dotgit.lstat().st_mode
        except OSError:
            return None
        if stat.S_ISDIR(mode):
            git_dir = dotgit
        elif stat.S_ISREG(mode):
            git_dir = self._gitdir_from_file(dotgit)
            # A linked worktree's git dir has a commondir file; it's listed under its repo.
            if git_dir is None or os.path.lexists(git_dir / "commondir"):
                return None
        else:
            return None
        if not self.is_dir(git_dir):
            return None
        head = self._head(git_dir / "HEAD")
        found = self._worktrees(git_dir)
        commits = self._resolve(git_dir, {h.ref for h in (head, *(h for _, h in found)) if h.ref})
        worktrees = [
            Worktree(
                path=self.relative(tree.parent),
                branch=h.branch,
                commit=h.commit or commits.get(h.ref or ""),
                missing=not _lexists(tree),
            )
            for tree, h in found
        ]
        return Repo(
            path=self.relative(checkout),
            origin=self._origin(git_dir / "config"),
            branch=head.branch,
            commit=head.commit or commits.get(head.ref or ""),
            worktrees=sorted(worktrees, key=lambda worktree: worktree.path),
        )

    def _gitdir_from_file(self, dotgit: Path) -> Path | None:
        text = self.read(dotgit)
        if text is None or not text.strip():
            return None
        line = text.strip().splitlines()[0]
        if not line.startswith("gitdir:"):
            return None
        return self.host_path(line.removeprefix("gitdir:").strip(), dotgit.parent)

    def _worktrees(self, git_dir: Path) -> list[tuple[Path, _Head]]:
        """(checkout ``.git`` path, HEAD) per linked worktree, at most MAX_WORKTREES."""
        admin = git_dir / "worktrees"
        if not self.is_dir(admin):
            return []
        try:
            entries = sorted(entry.name for entry in os.scandir(admin))
        except OSError:
            return []
        found: list[tuple[Path, _Head]] = []
        for name in entries:
            if len(found) >= MAX_WORKTREES:
                break
            entry = admin / name
            try:
                if entry.is_symlink() or not entry.is_dir():
                    continue
                recorded = self.read(entry / "gitdir")
                if recorded is None or not recorded.strip():
                    continue
                # The gitdir file names the checkout's .git file.
                found.append((self.host_path(recorded.strip(), entry), self._head(entry / "HEAD")))
            except (OSError, ValueError) as exc:
                log.debug("Skipping unreadable worktree %s: %s", entry, exc)
        return found

    # -- refs ----------------------------------------------------------------

    def _head(self, head: Path) -> _Head:
        text = self.read(head)
        if text is None:
            return _Head()
        value = text.strip()
        if value.startswith("ref:"):
            ref = value.removeprefix("ref:").strip()
            if ref == REFTABLE_HEAD:  # refs live in a reftable, which isn't read
                return _Head()
            branch = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else None
            return _Head(branch=branch, ref=ref)
        return _Head(commit=value if _SHA.fullmatch(value) else None)

    def _resolve(self, common: Path, refs: set[str]) -> dict[str, str]:
        """Commit hashes for ``refs`` from loose refs, then one pass over packed-refs."""
        commits: dict[str, str] = {}
        for ref in refs:
            if not ref.startswith("refs/") or ".." in ref.split("/") or "\\" in ref:
                continue
            loose = self.read(common / ref)
            if loose is not None:
                if _SHA.fullmatch(value := loose.strip()):
                    commits[ref] = value
            else:
                commits[ref] = ""  # look it up in packed-refs
        wanted = {ref.encode(): ref for ref, sha in commits.items() if not sha}
        if wanted:
            commits.update(self._packed(common / "packed-refs", wanted))
        return {ref: sha for ref, sha in commits.items() if sha}

    def _packed(self, path: Path, wanted: dict[bytes, str]) -> dict[str, str]:
        """The wanted refs found in a packed-refs file, searching at most its size cap."""
        handle = self._open(path)
        if handle is None:
            return {}
        with handle:
            try:
                data = handle.read(MAX_PACKED_REFS_BYTES)
            except OSError:
                return {}
        if len(data) < MAX_PACKED_REFS_BYTES and not data.endswith(b"\n"):
            data += b"\n"  # the whole file was read; let its last line match
        found: dict[str, str] = {}
        for name, ref in wanted.items():
            # Each line is "<sha> <refname>"; peeled "^<sha>" lines never end in a name.
            pattern = b" " + name + b"\n"
            index = data.find(pattern)
            while index != -1:
                sha = data[data.rfind(b"\n", 0, index) + 1 : index].decode("ascii", "replace")
                if _SHA.fullmatch(sha):
                    found[ref] = sha
                    break
                index = data.find(pattern, index + 1)
        return found

    # -- config --------------------------------------------------------------

    def _origin(self, config: Path) -> str | None:
        text = self.read(config)
        return origin_url(text) if text is not None else None


def _lexists(path: Path) -> bool:
    try:
        return os.path.lexists(path)
    except ValueError:
        return False


def origin_url(config: str) -> str | None:
    """The first ``url`` of ``[remote "origin"]`` in git config text, or None.

    Reads the file only: ``include``, ``includeIf`` and ``url.<base>.insteadOf`` are
    not applied, so the URL is the one written in the repo's own config. Credentials
    in the URL are removed (see :func:`_without_userinfo`).
    """
    in_origin = False
    for raw in config.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            match = _SECTION.match(line)
            if match is None:
                in_origin = False
                continue
            section, subsection = match.group(1).lower(), match.group(2)
            in_origin = (section == "remote" and subsection == "origin") or (
                subsection is None and section == "remote.origin"
            )
            line = line[match.end() :].strip()
            if not line:
                continue
        if not in_origin:
            continue
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() == "url":
            return _without_userinfo(_config_value(value)) or None
    return None


def _without_userinfo(url: str) -> str:
    """``url`` without credentials, so a token written into it is never served.

    URLs with a scheme lose their query and fragment (``?access_token=...``). HTTP(S)
    URLs lose their whole ``user[:password]@`` (a token is often the user name).
    Other schemes keep the user, which names the login (``ssh://git@host``), and lose
    only a password. A remote-helper prefix (``persistent-https::https://...``) is kept
    and the URL after it is cleaned the same way. Scp-style ``git@host:path`` has no
    password to remove.
    """
    helper, sep, address = url.partition("::")
    if sep and _HELPER.fullmatch(helper):
        return f"{helper}::{_without_userinfo(address)}"
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    authority, slash, path = rest.partition("/")
    authority = authority.partition("?")[0].partition("#")[0]
    path = path.partition("?")[0].partition("#")[0]
    userinfo, at, host = authority.rpartition("@")
    if at and "http" in scheme.lower():  # http, https and helpers such as git+https
        userinfo = ""
    elif at:
        userinfo = userinfo.partition(":")[0] + "@"
    return f"{scheme}://{userinfo}{host}{slash}{path}"


def _config_value(raw: str) -> str:
    """A git config value: quotes removed, escapes applied, trailing comment dropped."""
    out: list[str] = []
    quoted = False
    chars = iter(raw.strip())
    for char in chars:
        if char == "\\":
            escaped = next(chars, "")
            out.append({"n": "\n", "t": "\t", "b": "\b"}.get(escaped, escaped))
        elif char == '"':
            quoted = not quoted
        elif char in "#;" and not quoted:
            break
        else:
            out.append(char)
    return "".join(out).strip()

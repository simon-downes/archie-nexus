"""Centralised subprocess execution for exec tools.

All tools that shell out (rg for grep/glob/discovery, arbitrary shell commands)
route through this module so that the working directory is handled in ONE place.

Why this matters: the agent process starts in /opt/archie (the runtime venv, see
entrypoint.sh), NOT the project mount at /workspace. ripgrep's `-g`/`--glob`
patterns are matched relative to the process CWD, so running rg from /opt/archie
made glob patterns like "project/**/*.py" match nothing. Defaulting the CWD to
/workspace here fixes that once for every current and future tool.

ripgrep is a hard dependency (installed in the container image); callers surface
errors rather than falling back.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The container's project mount. Tool subprocesses run here so file discovery,
# git, and shell operations all act on the same tree.
WORKSPACE = Path("/workspace")


def kill_process_group(proc: asyncio.subprocess.Process, sig: int = signal.SIGKILL) -> None:
    """Send ``sig`` to the process group led by ``proc``.

    Subprocesses are spawned with ``start_new_session=True`` so the process and
    ALL its descendants share a new process group (== the child's PID). A hung
    ``sh -c "uv run pytest"`` is really a tree (sh -> uv -> python -> pytest);
    signalling only ``proc.pid`` leaves the descendants running and the turn
    wedged. ``killpg`` reaches the whole tree in one call.

    Falls back to signalling the single process if the group lookup fails
    (e.g. the process already exited, or it was not started in a new session).
    """
    if proc.returncode is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        pgid = None
    try:
        if pgid is not None:
            os.killpg(pgid, sig)
        else:
            proc.send_signal(sig)
    except (ProcessLookupError, PermissionError):
        pass


@dataclass(frozen=True)
class CompletedProcess:
    """Result of a subprocess run."""

    returncode: int | None
    stdout: str
    stderr: str


def _resolve_cwd(cwd: Path | str | None) -> str | None:
    """Pick the working directory, defaulting to /workspace when it exists.

    Falls back to inheriting the process CWD (None) when the target directory
    is absent — e.g. host-side tests running outside the container.
    """
    target = Path(cwd) if cwd is not None else WORKSPACE
    return str(target) if target.is_dir() else None


async def run_exec(
    *args: str,
    cwd: Path | str | None = None,
    timeout: float | None = None,
) -> CompletedProcess:
    """Run a command via create_subprocess_exec, capturing stdout/stderr.

    Args:
        *args: Command and arguments (no shell interpretation).
        cwd: Working directory. Defaults to /workspace (see module docstring).
        timeout: Optional seconds before the process is killed.

    Returns:
        CompletedProcess with decoded stdout/stderr.

    Raises:
        asyncio.TimeoutError: If the process exceeds the timeout.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=_resolve_cwd(cwd),
        start_new_session=True,
    )
    try:
        if timeout is not None:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout)
        else:
            stdout_b, stderr_b = await proc.communicate()
    except TimeoutError:
        kill_process_group(proc)
        await proc.wait()
        raise

    return CompletedProcess(
        returncode=proc.returncode,
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
    )


async def run_shell(
    command: str,
    cwd: Path | str | None = None,
    timeout: float | None = None,
    on_start: Callable[[asyncio.subprocess.Process], Any] | None = None,
) -> CompletedProcess:
    """Run a command through the shell (shell=True semantics).

    Uses create_subprocess_shell so the process can be killed on timeout
    or via interrupt. A blocking subprocess.run() in an executor cannot be
    cancelled, which would wedge the tool (and the whole agent turn) forever;
    this avoids that.

    Args:
        command: Shell command line (interpreted by /bin/sh).
        cwd: Working directory. Defaults to /workspace (see _resolve_cwd).
        timeout: Optional seconds before the process is killed.
        on_start: Optional callback invoked with the Process after spawn,
            used by the harness to capture the handle for cancellation (ESC).

    Returns:
        CompletedProcess with decoded stdout/stderr.

    Raises:
        asyncio.TimeoutError: If the process exceeds the timeout.
    """
    proc = await asyncio.create_subprocess_shell(  # noqa: S604
        command,  # start_new_session below puts sh + descendants in one group
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=_resolve_cwd(cwd),
        start_new_session=True,
    )
    if on_start:
        on_start(proc)
    try:
        if timeout is not None:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout)
        else:
            stdout_b, stderr_b = await proc.communicate()
    except TimeoutError:
        kill_process_group(proc)
        await proc.wait()
        raise
    return CompletedProcess(
        returncode=proc.returncode,
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
    )

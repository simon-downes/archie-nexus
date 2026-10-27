"""Shell tool — execute commands inside the container.

This runs INSIDE the container. There's no nesting — shell()
is just subprocess.run(command, shell=True). The container is
the security boundary.

Commands run in /workspace (the project mount) so that shell/git
operations act on the same tree as the file tools. Without this,
the process CWD is /opt/archie (the runtime), causing git/shell to
silently operate on the runtime instead of the project.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from archie_agent.exec.tools import FileNotFoundError as ToolFileNotFoundError
from archie_agent.exec.tools import tool
from archie_agent.exec.tools._subprocess import run_shell
from archie_agent.exec.tools.fs import _resolve_path


@tool(
    guidelines=(
        "Pass `timeout` (seconds) for commands that may hang; a timed-out command is killed "
        "and returned as an error so the turn always completes.",
        "Pass `cwd` to run in a subdirectory instead of prefixing `cd dir &&`; each call is a "
        "fresh non-interactive process (no TTY), so cd/export/activate don't persist.",
    )
)
async def shell(
    command: str,
    timeout: float | None = None,
    cwd: str | None = None,
    on_start: Callable[[asyncio.subprocess.Process], Any] | None = None,
) -> str:
    """Execute a shell command and return stdout, stderr, and exit code.
    Output starts with "[exit: N]"; a non-zero exit is returned, not raised.

    Args:
        command: Shell command to execute.
        timeout: Optional seconds before the command is killed. If omitted the
            command may run indefinitely; supply a timeout for anything that
            could hang (e.g. test runners, servers).
        cwd: Working directory, relative to /workspace or absolute
            (default: /workspace).

    Returns:
        Formatted string: "[exit: {code}]\\n{output}".
        Non-zero exit is returned as data, not raised as an exception.
        A timeout is returned as an error string, not raised.
    """
    workdir = None
    if cwd:
        workdir = _resolve_path(cwd)
        if not workdir.is_dir():
            raise ToolFileNotFoundError(f"Working directory not found: {cwd}")
    try:
        result = await run_shell(command, cwd=workdir, timeout=timeout, on_start=on_start)
    except TimeoutError:
        return f"$ {command}\n[error: command timed out after {timeout}s and was killed]"

    # Combine stdout and stderr (stderr after stdout, if present)
    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout.rstrip("\n"))
    if result.stderr:
        parts.append(result.stderr.rstrip("\n"))
    output = "\n".join(parts)

    header = f"[exit: {result.returncode}]"
    if output:
        return f"{header}\n{output}"
    return header

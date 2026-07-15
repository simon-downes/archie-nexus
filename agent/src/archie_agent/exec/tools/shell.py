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
import subprocess
from pathlib import Path

from archie_agent.exec.tools import tool

# The container's project mount. Commands run here so shell/git align with
# the file tools (see fs.py). Falls back to None (inherit CWD) when absent,
# e.g. running host-side tests outside the container.
_WORKSPACE = Path("/workspace")


@tool(guidelines=("Use `shell` for tests, builds, package commands, and git.",))
async def shell(command: str) -> str:
    """Execute a shell command and return stdout, stderr, and exit code.

    Args:
        command: Shell command to execute.

    Returns:
        Formatted string: "$ {command}\\n[exit: {code}]\\n{output}".
        Non-zero exit is returned as data, not raised as an exception.
    """
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: subprocess.run(  # noqa: S603, S602
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=_WORKSPACE if _WORKSPACE.is_dir() else None,
        ),
    )

    # Combine stdout and stderr (stderr after stdout, if present)
    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout.rstrip("\n"))
    if result.stderr:
        parts.append(result.stderr.rstrip("\n"))
    output = "\n".join(parts)

    header = f"$ {command}\n[exit: {result.returncode}]"
    if output:
        return f"{header}\n{output}"
    return header

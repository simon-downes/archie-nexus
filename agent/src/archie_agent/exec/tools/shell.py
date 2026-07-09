"""Shell tool — execute commands inside the container.

This runs INSIDE the container. There's no nesting — shell()
is just subprocess.run(command, shell=True). The container is
the security boundary.
"""

from __future__ import annotations

import asyncio
import subprocess

from archie_agent.exec.tools import tool


@tool(
    guidelines=("Use `shell` for tests, builds, package commands, and git.",)
)
async def shell(command: str) -> dict:
    """Execute a shell command and return stdout, stderr, and exit code.

    Args:
        command: Shell command to execute.

    Returns:
        Dict with keys: {"stdout": str, "stderr": str, "exit_code": int}.
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
        ),
    )

    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
    }

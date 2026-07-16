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

from archie_agent.exec.tools import tool
from archie_agent.exec.tools._subprocess import run_shell


@tool(guidelines=("Use `shell` for tests, builds, package commands, and git.",))
async def shell(command: str) -> str:
    """Execute a shell command and return stdout, stderr, and exit code.

    Args:
        command: Shell command to execute.

    Returns:
        Formatted string: "$ {command}\\n[exit: {code}]\\n{output}".
        Non-zero exit is returned as data, not raised as an exception.
    """
    result = await run_shell(command)

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

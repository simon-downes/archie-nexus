"""Project directory detection.

Determines which project directory to use as the working context.
The logic: walk up from the current working directory to find the first
directory that's a direct child of workspace_root (e.g. ~/dev/myproject).

This gives IDE-like behaviour — launching archie from ~/dev/myproject/src/lib
still treats ~/dev/myproject as the project root, so file tools see the
whole project tree.
"""

from pathlib import Path

DEFAULT_WORKSPACE_ROOT = Path.home() / "dev"


def detect_project_dir(cwd: Path | None = None, workspace_root: Path | None = None) -> Path:
    """Detect the project directory by walking up from cwd.

    Finds the first ancestor of cwd (or cwd itself) that's a direct child
    of workspace_root. Falls back to cwd if it's not under workspace_root.

    Examples:
        detect_project_dir(Path("~/dev/myproj/src"), Path("~/dev"))
        → Path("~/dev/myproj")

        detect_project_dir(Path("/tmp/random"), Path("~/dev"))
        → Path("/tmp/random")  # fallback

    Args:
        cwd: Current working directory (resolved/absolute). Defaults to Path.cwd().
        workspace_root: The parent directory that contains all workspaces.
            Defaults to ~/dev.

    Returns:
        The detected project directory path.
    """
    if cwd is None:
        cwd = Path.cwd()
    if workspace_root is None:
        workspace_root = DEFAULT_WORKSPACE_ROOT

    # Resolve both to ensure consistent comparison
    cwd = cwd.resolve()
    workspace_root = workspace_root.resolve()

    # Walk up from cwd, checking if each ancestor is a direct child of workspace_root
    current = cwd
    while current != current.parent:  # Stop at filesystem root
        if current.parent == workspace_root:
            return current
        current = current.parent

    # Fallback: cwd is not under workspace_root, use cwd as-is
    return cwd

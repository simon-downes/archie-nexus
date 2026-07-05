"""Project directory detection.

Determines which project directory to use as the working context.
The logic: walk up from the current working directory to find the first
directory that's a direct child of project_root (e.g. ~/dev/myproject).

This gives IDE-like behaviour — launching archie from ~/dev/myproject/src/lib
still treats ~/dev/myproject as the project root, so file tools see the
whole project tree.
"""

from pathlib import Path

DEFAULT_PROJECT_ROOT = Path.home() / "dev"


def detect_project_dir(cwd: Path | None = None, project_root: Path | None = None) -> Path:
    """Detect the project directory by walking up from cwd.

    Finds the first ancestor of cwd (or cwd itself) that's a direct child
    of project_root. Falls back to cwd if it's not under project_root.

    Examples:
        detect_project_dir(Path("~/dev/myproj/src"), Path("~/dev"))
        → Path("~/dev/myproj")

        detect_project_dir(Path("/tmp/random"), Path("~/dev"))
        → Path("/tmp/random")  # fallback

    Args:
        cwd: Current working directory (resolved/absolute). Defaults to Path.cwd().
        project_root: The parent directory that contains all projects.
            Defaults to ~/dev.

    Returns:
        The detected project directory path.
    """
    if cwd is None:
        cwd = Path.cwd()
    if project_root is None:
        project_root = DEFAULT_PROJECT_ROOT

    # Resolve both to ensure consistent comparison
    cwd = cwd.resolve()
    project_root = project_root.resolve()

    # Walk up from cwd, checking if each ancestor is a direct child of project_root
    current = cwd
    while current != current.parent:  # Stop at filesystem root
        if current.parent == project_root:
            return current
        current = current.parent

    # Fallback: cwd is not under project_root, use cwd as-is
    return cwd

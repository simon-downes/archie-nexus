"""Filesystem tools — read, grep, glob, write, edit.

These run INSIDE the container. All paths are relative to /workspace/
(the container's project mount). Uses ripgrep for grep, pathlib for glob.
"""

from __future__ import annotations

import asyncio
import difflib
import json
from pathlib import Path

from archie_agent.exec.tools import (
    BinaryFileError,
    EditError,
    FileNotFoundError,
    PathValidationError,
    tool,
)

# The container's project mount point.
WORKSPACE = Path("/workspace")

# Maximum characters per line before truncation.
_LINE_LENGTH_CAP = 500

# Maximum file groups to show in grep output.
_GREP_MAX_GROUPS = 50

# Maximum files to show in glob output.
_GLOB_MAX_FILES = 100


def _resolve_path(path: str) -> Path:
    """Resolve a path relative to /workspace/ and validate it.

    Accepts:
      - Relative paths: resolved against /workspace/
      - /workspace/ prefixed paths: used as-is

    Rejects:
      - Absolute paths not under /workspace/
      - Paths that traverse above /workspace/ via ..

    Returns the resolved absolute Path.
    Raises PathValidationError on invalid paths.
    """
    if not path:
        raise PathValidationError("Path must not be empty")

    p = Path(path)

    if p.is_absolute():
        if not (str(p) == str(WORKSPACE) or str(p).startswith(str(WORKSPACE) + "/")):
            raise PathValidationError(
                f"Absolute path '{path}' is not under /workspace/. "
                "Use relative paths or /workspace/ prefix."
            )
        resolved = p.resolve()
    else:
        resolved = (WORKSPACE / p).resolve()

    # After resolution, verify still under /workspace/
    try:
        resolved.relative_to(WORKSPACE.resolve())
    except ValueError:
        raise PathValidationError(
            f"Path '{path}' resolves outside /workspace/ (traversal detected)."
        ) from None

    return resolved


@tool(guidelines=("Use `read` to examine file contents.",))
async def read(
    path: str, offset: int | None = None, limit: int | None = None, raw: bool = False
) -> str:
    """Read a file from the workspace.

    Args:
        path: File path (relative to /workspace/ or absolute under /workspace/).
        offset: Start line, 1-indexed (default: 1).
        limit: Maximum lines to return (default: all).
        raw: If True, return plain content without line numbers.

    Returns:
        File content with line numbers (e.g. "    1| content") unless raw=True.

    Raises:
        PathValidationError: Path is outside /workspace/.
        FileNotFoundError: File does not exist.
        BinaryFileError: File is binary.
    """
    resolved = _resolve_path(path)

    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {path}")

    if resolved.is_dir():
        raise FileNotFoundError(f"Path is a directory, not a file: {path}")

    # Binary detection — check first 8KB for null bytes
    try:
        with resolved.open("rb") as f:
            chunk = f.read(8192)
        if b"\x00" in chunk:
            raise BinaryFileError(f"Binary file detected: {path}")
    except OSError as e:
        raise FileNotFoundError(f"Cannot read file: {e}") from e

    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise FileNotFoundError(f"Cannot read file: {e}") from e

    lines = text.splitlines()
    total_lines = len(lines)

    # Apply pagination (offset is 1-indexed externally, convert to 0-based)
    start = 0
    if offset is not None:
        start = max(offset - 1, 0)

    if limit is not None:
        selected = lines[start : start + limit]
    else:
        selected = lines[start:]

    if raw:
        return "\n".join(selected)

    # Format with line numbers
    numbered: list[str] = []
    for i, line in enumerate(selected, start=start + 1):
        if len(line) > _LINE_LENGTH_CAP:
            line = line[:_LINE_LENGTH_CAP] + "...[truncated]"
        numbered.append(f"{i:>5}| {line}")

    # Header with metadata
    header = f"File: {path} ({total_lines} lines)"
    lines_shown = len(numbered)
    actual_end = start + lines_shown
    if actual_end < total_lines:
        header += f"\nShowing lines {start + 1}-{actual_end} of {total_lines}"
        header += f"\nUse offset={actual_end + 1} to continue reading"

    return header + "\n\n" + "\n".join(numbered)


@tool(guidelines=("Use `grep` to search file contents by regex pattern.",))
async def grep(pattern: str, include: str | None = None, path: str | None = None) -> str:
    """Search file contents using regex via ripgrep.

    Args:
        pattern: Regex pattern to search for.
        include: File glob filter (e.g. '*.py').
        path: Directory to search in (relative to /workspace/, default: /workspace/).

    Returns:
        Formatted results grouped by file (most recently modified first).
        Each file group shows matching lines with line numbers.

    Raises:
        PathValidationError: Search path is outside /workspace/.
    """
    if not pattern:
        raise PathValidationError("Pattern must not be empty")

    if path:
        search_dir = _resolve_path(path)
    else:
        search_dir = WORKSPACE

    if not search_dir.exists():
        raise PathValidationError(f"Search path does not exist: {path or '/workspace/'}")

    # Build ripgrep command
    cmd = [
        "rg",
        "--json",
        "-i",  # case-insensitive
        "--max-count",
        "200",  # per-file cap
    ]
    if include:
        cmd.extend(["-g", include])
    cmd.append(pattern)
    cmd.append(str(search_dir))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()

    # rg exit codes: 0 = matches, 1 = no matches, 2+ = error
    if proc.returncode == 1:
        return "No matches found."
    if proc.returncode not in (0, 1) and proc.returncode is not None:
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ripgrep error (exit {proc.returncode}): {stderr_text}")

    # Parse JSON output into per-file groups
    file_matches: dict[str, list[tuple[int, str]]] = {}
    stdout_text = stdout_bytes.decode("utf-8", errors="replace")

    for line in stdout_text.strip().split("\n"):
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        if obj.get("type") == "match":
            data = obj.get("data", {})
            file_path = data.get("path", {}).get("text", "")
            line_number = data.get("line_number", 0)
            text = data.get("lines", {}).get("text", "").rstrip("\n")

            # Make path relative to /workspace/
            try:
                rel_path = str(Path(file_path).relative_to(WORKSPACE))
            except ValueError:
                rel_path = file_path

            if rel_path not in file_matches:
                file_matches[rel_path] = []
            file_matches[rel_path].append((line_number, text))

    if not file_matches:
        return "No matches found."

    # Sort files by mtime descending (most recently modified first)
    files_with_mtime: list[tuple[str, float, list[tuple[int, str]]]] = []
    for rel_path, matches in file_matches.items():
        abs_path = WORKSPACE / rel_path
        try:
            mtime = abs_path.stat().st_mtime
        except OSError:
            mtime = 0.0
        files_with_mtime.append((rel_path, mtime, matches))

    files_with_mtime.sort(key=lambda x: x[1], reverse=True)

    # Format output — cap at 50 file groups
    total_files = len(files_with_mtime)
    display_files = files_with_mtime[:_GREP_MAX_GROUPS]

    output_lines: list[str] = []
    for rel_path, _mtime, matches in display_files:
        # Determine line number width for alignment
        width = len(str(max(ln for ln, _ in matches))) if matches else 1
        output_lines.append(f"{rel_path}:")
        for lineno, text in matches:
            if len(text) > _LINE_LENGTH_CAP:
                text = text[:_LINE_LENGTH_CAP] + "...[truncated]"
            output_lines.append(f"  {lineno:>{width}}| {text}")
        output_lines.append("")  # blank line between file groups

    # Remove trailing blank line
    if output_lines and output_lines[-1] == "":
        output_lines.pop()

    if total_files > _GREP_MAX_GROUPS:
        output_lines.append(
            f"\nShowing {_GREP_MAX_GROUPS} of {total_files} files — narrow your query."
        )

    return "\n".join(output_lines)


@tool(guidelines=("Use `glob` to find files by name pattern.",))
async def glob(pattern: str, path: str | None = None) -> str:
    """Find files matching a glob pattern within the workspace.

    Args:
        pattern: Glob pattern (e.g. '**/*.py', 'src/**/*.ts', '*.md').
        path: Directory to search from (relative to /workspace/, default: /workspace/).

    Returns:
        Formatted file list sorted by modification time (most recent first),
        with a header showing count.

    Raises:
        PathValidationError: Search path is outside /workspace/.
    """
    if not pattern:
        raise PathValidationError("Pattern must not be empty")

    if path:
        search_dir = _resolve_path(path)
    else:
        search_dir = WORKSPACE

    if not search_dir.exists():
        raise PathValidationError(f"Search path does not exist: {path or '/workspace/'}")

    if not search_dir.is_dir():
        raise PathValidationError(f"Not a directory: {path or '/workspace/'}")

    # Collect matching files with mtime
    files_with_mtime: list[tuple[str, float]] = []
    try:
        for match in search_dir.glob(pattern):
            if match.is_file() and ".git" not in match.parts:
                try:
                    rel = str(match.relative_to(WORKSPACE))
                    mtime = match.stat().st_mtime
                    files_with_mtime.append((rel, mtime))
                except (ValueError, OSError):
                    pass
    except OSError:
        pass

    if not files_with_mtime:
        return "No files found."

    # Sort by mtime descending (most recently modified first)
    files_with_mtime.sort(key=lambda x: x[1], reverse=True)

    # Cap at 100 files
    total_count = len(files_with_mtime)
    display_files = files_with_mtime[:_GLOB_MAX_FILES]

    # Format output
    if total_count > _GLOB_MAX_FILES:
        header = f"{_GLOB_MAX_FILES} files shown of {total_count}, most recent first. Narrow the pattern for more."
    else:
        header = f"{total_count} files, most recent first"

    lines = [header, ""]
    for rel_path, _ in display_files:
        lines.append(rel_path)

    return "\n".join(lines)


@tool(guidelines=("Use `write` for new files or complete rewrites.",))
async def write(path: str, content: str) -> str:
    """Write content to a file, creating parent directories as needed.

    Args:
        path: File path (relative to /workspace/ or absolute under /workspace/).
        content: Content to write to the file.

    Returns:
        Confirmation string with path and line count.

    Raises:
        PathValidationError: Path is outside /workspace/.
    """
    resolved = _resolve_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    return f"Written: {path} ({line_count} lines)"


@tool(
    guidelines=(
        "Use `edit` to modify existing files — it shows a diff and is safer than a full rewrite.",
    )
)
async def edit(path: str, old: str, new: str, replace_all: bool = False) -> str:
    """Apply a string replacement edit to a file.

    Args:
        path: File path (relative to /workspace/ or absolute under /workspace/).
        old: Exact text to find in the file.
        new: Replacement text.
        replace_all: If True, replace all occurrences. If False (default),
            the old text must match exactly once.

    Returns:
        A unified diff string showing what changed.

    Raises:
        PathValidationError: Path is outside /workspace/.
        FileNotFoundError: File does not exist.
        EditError: old text not found, or ambiguous match.
    """
    resolved = _resolve_path(path)

    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {path}")

    if not resolved.is_file():
        raise FileNotFoundError(f"Path is a directory, not a file: {path}")

    try:
        original = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise FileNotFoundError(f"Cannot read file: {e}") from e

    if not old:
        raise EditError("'old' text cannot be empty.")

    count = original.count(old)

    if count == 0:
        raise EditError(
            "Text not found in file. Ensure the 'old' text "
            "matches exactly (including whitespace and indentation)."
        )

    if count > 1 and not replace_all:
        raise EditError(
            f"Found {count} matches. Include more surrounding "
            "context to disambiguate, or set replace_all=True to replace all."
        )

    if replace_all:
        modified = original.replace(old, new)
    else:
        modified = original.replace(old, new, 1)

    resolved.write_text(modified, encoding="utf-8")

    # Generate unified diff
    original_lines = original.splitlines(keepends=True)
    modified_lines = modified.splitlines(keepends=True)
    diff = difflib.unified_diff(
        original_lines,
        modified_lines,
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )

    return "".join(diff)

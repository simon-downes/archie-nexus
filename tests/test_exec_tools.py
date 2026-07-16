"""Tests for exec tools (fs + shell).

All tests patch WORKSPACE to point at a tmp_path so they run without /workspace/.
"""

from unittest.mock import patch

import pytest
from archie_agent.exec.tools import (
    BinaryFileError,
    EditError,
    FileNotFoundError,
    PathValidationError,
    get_all_tools,
)
from archie_agent.exec.tools.fs import _resolve_path


@pytest.fixture
def workspace(tmp_path):
    """Patch WORKSPACE to a temporary directory (both fs and subprocess wrapper)."""
    with (
        patch("archie_agent.exec.tools.fs.WORKSPACE", tmp_path),
        patch("archie_agent.exec.tools._subprocess.WORKSPACE", tmp_path),
    ):
        yield tmp_path


# --- Registration ---


def test_get_all_tools_returns_expected():
    tools = get_all_tools()
    assert "read" in tools
    assert "write" in tools
    assert "edit" in tools
    assert "grep" in tools
    assert "glob" in tools
    assert "shell" in tools
    assert callable(tools["read"])


# --- Path validation ---


def test_resolve_path_relative(workspace):
    resolved = _resolve_path("src/main.py")
    assert resolved == workspace / "src" / "main.py"


def test_resolve_path_absolute_under_workspace(workspace):
    """Absolute paths under /workspace/ are valid."""
    # Patch uses tmp_path as WORKSPACE, so we need to use that path
    resolved = _resolve_path(str(workspace / "src" / "main.py"))
    assert resolved == workspace / "src" / "main.py"


def test_resolve_path_empty():
    with pytest.raises(PathValidationError, match="must not be empty"):
        _resolve_path("")


def test_resolve_path_traversal(workspace):
    with pytest.raises(PathValidationError, match="traversal"):
        _resolve_path("../../etc/passwd")


def test_resolve_path_absolute_outside(workspace):
    with pytest.raises(PathValidationError, match="not under /workspace/"):
        _resolve_path("/etc/passwd")


# --- read ---


async def test_read_basic(workspace):
    f = workspace / "hello.txt"
    f.write_text("line1\nline2\nline3\n")
    tools = get_all_tools()
    result = await tools["read"](path="hello.txt")
    assert "hello.txt (3 lines)" in result
    assert "1| line1" in result
    assert "2| line2" in result


async def test_read_with_offset_and_limit(workspace):
    f = workspace / "hello.txt"
    f.write_text("a\nb\nc\nd\ne\n")
    tools = get_all_tools()
    result = await tools["read"](path="hello.txt", offset=2, limit=2)
    assert "2| b" in result
    assert "3| c" in result
    assert "1| a" not in result


async def test_read_raw(workspace):
    f = workspace / "hello.txt"
    f.write_text("raw content\n")
    tools = get_all_tools()
    result = await tools["read"](path="hello.txt", raw=True)
    assert result == "raw content"


async def test_read_file_not_found(workspace):
    tools = get_all_tools()
    with pytest.raises(FileNotFoundError, match="not found"):
        await tools["read"](path="nope.txt")


async def test_read_binary_file(workspace):
    f = workspace / "data.bin"
    f.write_bytes(b"\x00\x01\x02\x03binary")
    tools = get_all_tools()
    with pytest.raises(BinaryFileError, match="Binary"):
        await tools["read"](path="data.bin")


async def test_read_directory(workspace):
    d = workspace / "subdir"
    d.mkdir()
    tools = get_all_tools()
    with pytest.raises(FileNotFoundError, match="directory"):
        await tools["read"](path="subdir")


# --- write ---


async def test_write_creates_file(workspace):
    tools = get_all_tools()
    await tools["write"](path="new.txt", content="hello")
    assert (workspace / "new.txt").read_text() == "hello"


async def test_write_creates_parents(workspace):
    tools = get_all_tools()
    await tools["write"](path="a/b/c.txt", content="deep")
    assert (workspace / "a" / "b" / "c.txt").read_text() == "deep"


async def test_write_path_outside(workspace):
    tools = get_all_tools()
    with pytest.raises(PathValidationError):
        await tools["write"](path="../../evil.txt", content="bad")


# --- edit ---


async def test_edit_single_replace(workspace):
    f = workspace / "code.py"
    f.write_text("x = 1\ny = 2\n")
    tools = get_all_tools()
    result = await tools["edit"](path="code.py", old="x = 1", new="x = 42")
    assert "-x = 1" in result
    assert "+x = 42" in result
    assert (workspace / "code.py").read_text() == "x = 42\ny = 2\n"


async def test_edit_not_found(workspace):
    f = workspace / "code.py"
    f.write_text("x = 1\n")
    tools = get_all_tools()
    with pytest.raises(EditError, match="not found"):
        await tools["edit"](path="code.py", old="z = 99", new="z = 0")


async def test_edit_ambiguous(workspace):
    f = workspace / "code.py"
    f.write_text("x = 1\nx = 1\n")
    tools = get_all_tools()
    with pytest.raises(EditError, match="matches"):
        await tools["edit"](path="code.py", old="x = 1", new="x = 2")


async def test_edit_replace_all(workspace):
    f = workspace / "code.py"
    f.write_text("x = 1\nx = 1\n")
    tools = get_all_tools()
    await tools["edit"](path="code.py", old="x = 1", new="x = 2", replace_all=True)
    assert (workspace / "code.py").read_text() == "x = 2\nx = 2\n"


async def test_edit_empty_old(workspace):
    f = workspace / "code.py"
    f.write_text("x = 1\n")
    tools = get_all_tools()
    with pytest.raises(EditError, match="empty"):
        await tools["edit"](path="code.py", old="", new="x")


# --- glob ---


async def test_glob_finds_files(workspace):
    (workspace / "a.py").write_text("a")
    (workspace / "b.py").write_text("b")
    (workspace / "c.txt").write_text("c")
    tools = get_all_tools()
    result = await tools["glob"](pattern="*.py")
    assert "2 files, most recent first" in result
    assert "a.py" in result
    assert "b.py" in result
    assert "c.txt" not in result


async def test_glob_recursive(workspace):
    (workspace / "src").mkdir()
    (workspace / "src" / "mod.py").write_text("")
    tools = get_all_tools()
    result = await tools["glob"](pattern="**/*.py")
    assert "src/mod.py" in result


async def test_glob_respects_gitignore(workspace):
    import subprocess

    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / ".gitignore").write_text("ignored/\n")
    ignored = workspace / "ignored"
    ignored.mkdir()
    (ignored / "skip.py").write_text("")
    (workspace / "keep.py").write_text("")
    tools = get_all_tools()
    result = await tools["glob"](pattern="**/*.py")
    assert "keep.py" in result
    assert "skip.py" not in result


async def test_glob_works_when_process_cwd_differs(workspace, monkeypatch, tmp_path):
    """Regression: glob must work when the process CWD is not the search dir.

    In the container the agent runs from /opt/archie (runtime), not /workspace.
    ripgrep anchors -g globs to the process CWD, so a path-prefixed pattern
    returned nothing. glob now runs rg with cwd=search_dir, so it must find
    files regardless of where the process itself is running from.
    """
    (workspace / "sub").mkdir()
    (workspace / "sub" / "found.py").write_text("x")

    # Simulate the process running from an unrelated directory.
    elsewhere = tmp_path.parent / "elsewhere-cwd"
    elsewhere.mkdir(exist_ok=True)
    monkeypatch.chdir(elsewhere)

    tools = get_all_tools()
    result = await tools["glob"](pattern="sub/**/*.py")
    assert "sub/found.py" in result


async def test_glob_excludes_noise_dirs_for_broad_pattern(workspace):
    """A broad `**/*` whitelist must not flood output with .venv/node_modules.

    An explicit `-g` whitelist overrides .gitignore in ripgrep, so glob adds
    explicit negations for common noise directories.
    """
    (workspace / "real.py").write_text("x")
    for noise in (".venv", "node_modules", "__pycache__"):
        d = workspace / noise / "pkg"
        d.mkdir(parents=True)
        (d / "junk.py").write_text("x")

    tools = get_all_tools()
    result = await tools["glob"](pattern="**/*")
    assert "real.py" in result
    assert ".venv" not in result
    assert "node_modules" not in result
    assert "__pycache__" not in result


async def test_glob_no_matches(workspace):
    tools = get_all_tools()
    result = await tools["glob"](pattern="*.xyz")
    assert result == "No files found."


async def test_glob_empty_pattern(workspace):
    tools = get_all_tools()
    with pytest.raises(PathValidationError, match="must not be empty"):
        await tools["glob"](pattern="")


# --- grep ---


async def test_grep_finds_matches(workspace):
    (workspace / "code.py").write_text("def hello():\n    pass\n")
    tools = get_all_tools()
    result = await tools["grep"](pattern="hello")
    assert "code.py:" in result
    assert "hello" in result
    assert "1|" in result or "1 |" in result


async def test_grep_no_matches(workspace):
    (workspace / "code.py").write_text("def hello():\n    pass\n")
    tools = get_all_tools()
    result = await tools["grep"](pattern="nonexistent_xyz")
    assert result == "No matches found."


async def test_grep_with_include(workspace):
    (workspace / "a.py").write_text("target\n")
    (workspace / "b.txt").write_text("target\n")
    tools = get_all_tools()
    result = await tools["grep"](pattern="target", include="*.py")
    assert "a.py:" in result
    assert "b.txt" not in result


async def test_grep_empty_pattern(workspace):
    tools = get_all_tools()
    with pytest.raises(PathValidationError, match="must not be empty"):
        await tools["grep"](pattern="")


# --- shell ---


async def test_shell_basic(workspace):
    tools = get_all_tools()
    result = await tools["shell"](command="echo hello")
    assert "$ echo hello" in result
    assert "[exit: 0]" in result
    assert "hello" in result


async def test_shell_nonzero_exit(workspace):
    tools = get_all_tools()
    result = await tools["shell"](command="exit 42")
    assert "[exit: 42]" in result


async def test_shell_stderr(workspace):
    tools = get_all_tools()
    result = await tools["shell"](command="echo err >&2")
    assert "err" in result
    assert "[exit: 0]" in result


async def test_shell_runs_in_workspace(workspace, tmp_path):
    """shell() runs in /workspace when it exists (aligns with file tools).

    The `workspace` fixture patches _subprocess.WORKSPACE to tmp_path.
    """
    tools = get_all_tools()
    result = await tools["shell"](command="pwd")
    assert str(tmp_path) in result


async def test_shell_falls_back_when_workspace_absent(monkeypatch):
    """shell() inherits CWD when /workspace is absent (host-side runs)."""
    from pathlib import Path

    from archie_agent.exec.tools import _subprocess as sp_mod

    monkeypatch.setattr(sp_mod, "WORKSPACE", Path("/nonexistent-workspace-xyz"))
    tools = get_all_tools()
    result = await tools["shell"](command="echo ok")
    assert "ok" in result
    assert "[exit: 0]" in result


# --- get_tool_guidelines ---


def test_get_tool_guidelines_returns_all_tools():
    """get_tool_guidelines returns guidelines from all registered tools."""
    from archie_agent.exec.tools import get_all_tools, get_tool_guidelines

    guidelines = get_tool_guidelines()

    # Every registered tool should have at least one guideline
    assert len(guidelines) >= len(get_all_tools())

    # Check representative entries from each tool
    joined = "\n".join(guidelines)
    assert "read" in joined
    assert "write" in joined
    assert "edit" in joined
    assert "grep" in joined
    assert "glob" in joined
    assert "shell" in joined


def test_get_tool_guidelines_deterministic_order():
    """get_tool_guidelines returns the same order on repeated calls."""
    from archie_agent.exec.tools import get_tool_guidelines

    first = get_tool_guidelines()
    second = get_tool_guidelines()
    assert first == second

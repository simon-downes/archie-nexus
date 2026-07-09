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
    """Patch WORKSPACE to a temporary directory."""
    with patch("archie_agent.exec.tools.fs.WORKSPACE", tmp_path):
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
    assert result == ["a.py", "b.py"]


async def test_glob_recursive(workspace):
    (workspace / "src").mkdir()
    (workspace / "src" / "mod.py").write_text("")
    tools = get_all_tools()
    result = await tools["glob"](pattern="**/*.py")
    assert "src/mod.py" in result


async def test_glob_no_matches(workspace):
    tools = get_all_tools()
    result = await tools["glob"](pattern="*.xyz")
    assert result == []


async def test_glob_empty_pattern(workspace):
    tools = get_all_tools()
    with pytest.raises(PathValidationError, match="must not be empty"):
        await tools["glob"](pattern="")


# --- grep ---


async def test_grep_finds_matches(workspace):
    (workspace / "code.py").write_text("def hello():\n    pass\n")
    tools = get_all_tools()
    result = await tools["grep"](pattern="hello")
    assert len(result) >= 1
    assert result[0]["path"] == "code.py"
    assert result[0]["line"] == 1
    assert "hello" in result[0]["text"]


async def test_grep_no_matches(workspace):
    (workspace / "code.py").write_text("def hello():\n    pass\n")
    tools = get_all_tools()
    result = await tools["grep"](pattern="nonexistent_xyz")
    assert result == []


async def test_grep_with_include(workspace):
    (workspace / "a.py").write_text("target\n")
    (workspace / "b.txt").write_text("target\n")
    tools = get_all_tools()
    result = await tools["grep"](pattern="target", include="*.py")
    paths = [r["path"] for r in result]
    assert "a.py" in paths
    assert "b.txt" not in paths


async def test_grep_empty_pattern(workspace):
    tools = get_all_tools()
    with pytest.raises(PathValidationError, match="must not be empty"):
        await tools["grep"](pattern="")


# --- shell ---


async def test_shell_basic(workspace):
    tools = get_all_tools()
    result = await tools["shell"](command="echo hello")
    assert result["stdout"].strip() == "hello"
    assert result["exit_code"] == 0


async def test_shell_nonzero_exit(workspace):
    tools = get_all_tools()
    result = await tools["shell"](command="exit 42")
    assert result["exit_code"] == 42


async def test_shell_stderr(workspace):
    tools = get_all_tools()
    result = await tools["shell"](command="echo err >&2")
    assert "err" in result["stderr"]
    assert result["exit_code"] == 0


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

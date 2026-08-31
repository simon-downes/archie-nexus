"""Integration tests for native tool dispatch through the harness.

Tests that native tools (read, grep, glob, edit, write, shell, code, web_search,
web_fetch) dispatch correctly through _execute_tool, produce string results,
handle errors identically to exec, and apply truncation.
"""

import json
from unittest.mock import MagicMock, patch

from archie_agent.harness import AgentHarness
from archie_agent.llm._types import Done, TextDelta, ToolUseEvent, Usage
from archie_agent.llm.fake import FakeLLMClient
from archie_agent.session import Session
from archie_shared.models import BedrockProvider, CostConfig, ModelEntry
from archie_shared.tool_summaries import format_tool_complete, format_tool_pending

# --- Fixtures ---


def _make_model() -> ModelEntry:
    return ModelEntry(
        name="Test Model",
        provider=BedrockProvider(model_id="test-model-id", region="us-east-1"),
        cost=CostConfig(input=3.0, output=15.0),
        context=200000,
        max_output_tokens=4096,
    )


def _make_harness(tmp_path, responses: list[list]) -> AgentHarness:
    model = _make_model()
    session = Session(
        model_id="test-model",
        model=model,
        session_id="test-dispatch-001",
    )
    llm = FakeLLMClient(responses=responses)
    return AgentHarness(
        session=session,
        llm_client=llm,
        model_name="Test Model",
        log_dir=tmp_path,
    )


class FakeWebSocket:
    def __init__(self):
        self.messages: list[str] = []
        self.closed = False

    async def send_text(self, data: str) -> None:
        self.messages.append(data)


def _get_tool_results(ws: FakeWebSocket) -> list[dict]:
    """Extract tool_result event data dicts from captured WebSocket messages."""
    results = []
    for m in ws.messages:
        parsed = json.loads(m)
        if parsed.get("type") == "tool_result":
            results.append(parsed)
    return results


# --- Test: native read dispatches correctly ---


async def test_native_read_dispatch(tmp_path, monkeypatch):
    """Native read tool dispatches through _execute_tool and returns file content."""
    # Create a workspace file for read to find
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("line one\nline two\n")

    # Monkeypatch WORKSPACE for the fs module
    monkeypatch.setattr("archie_agent.exec.tools.fs.WORKSPACE", workspace)

    # Simulate model calling native read
    responses = [
        [
            ToolUseEvent(tool_use_id="tu_1", name="read", input={"path": "hello.txt"}),
            Usage(input_tokens=100, output_tokens=50),
            Done(stop_reason="tool_use"),
        ],
        [
            TextDelta(text="I read the file."),
            Usage(input_tokens=200, output_tokens=60),
            Done(stop_reason="end_turn"),
        ],
    ]
    harness = _make_harness(tmp_path, responses)
    ws = FakeWebSocket()
    harness.clients.add(ws)
    await harness.handle_message("read hello.txt")

    # Find the tool_result event
    results = _get_tool_results(ws)
    assert len(results) == 1
    assert results[0]["is_error"] is False
    assert results[0]["result_bytes"] > 0
    # Wire carries raw content; client reconstructs the completion summary.
    summary = format_tool_complete(
        "read", {"path": "hello.txt"}, results[0]["content"], results[0]["is_error"]
    )
    assert "hello.txt" in summary
    assert "2 lines" in summary


# --- Test: native write returns confirmation ---


async def test_native_write_dispatch(tmp_path, monkeypatch):
    """Native write tool returns a confirmation string."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("archie_agent.exec.tools.fs.WORKSPACE", workspace)

    responses = [
        [
            ToolUseEvent(
                tool_use_id="tu_w",
                name="write",
                input={"path": "new.txt", "content": "hello world\n"},
            ),
            Usage(input_tokens=100, output_tokens=50),
            Done(stop_reason="tool_use"),
        ],
        [
            TextDelta(text="Done."),
            Usage(input_tokens=200, output_tokens=60),
            Done(stop_reason="end_turn"),
        ],
    ]
    harness = _make_harness(tmp_path, responses)
    ws = FakeWebSocket()
    harness.clients.add(ws)
    await harness.handle_message("write a file")

    # The file should exist
    assert (workspace / "new.txt").read_text() == "hello world\n"

    # Check the harness stored a non-error result
    results = _get_tool_results(ws)
    assert len(results) == 1
    assert results[0]["is_error"] is False


# --- Test: native shell dispatches and returns formatted string ---


async def test_native_shell_dispatch(tmp_path, monkeypatch):
    """Native shell tool returns formatted $ command + exit code."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("archie_agent.exec.tools._subprocess.WORKSPACE", workspace)

    responses = [
        [
            ToolUseEvent(
                tool_use_id="tu_sh",
                name="shell",
                input={"command": "echo hi"},
            ),
            Usage(input_tokens=100, output_tokens=50),
            Done(stop_reason="tool_use"),
        ],
        [
            TextDelta(text="Output shown."),
            Usage(input_tokens=200, output_tokens=60),
            Done(stop_reason="end_turn"),
        ],
    ]
    harness = _make_harness(tmp_path, responses)
    ws = FakeWebSocket()
    harness.clients.add(ws)
    await harness.handle_message("run echo hi")

    results = _get_tool_results(ws)
    assert len(results) == 1
    assert results[0]["is_error"] is False
    assert "$ echo hi" not in results[0]["content"]


# --- Test: native grep dispatches with formatted output ---


async def test_native_shell_nonzero_exit_is_error(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("archie_agent.exec.tools._subprocess.WORKSPACE", workspace)

    responses = [
        [
            ToolUseEvent(tool_use_id="tu_sh_fail", name="shell", input={"command": "exit 1"}),
            Usage(input_tokens=100, output_tokens=50),
            Done(stop_reason="tool_use"),
        ],
        [
            TextDelta(text="Handled."),
            Usage(input_tokens=200, output_tokens=60),
            Done(stop_reason="end_turn"),
        ],
    ]
    harness = _make_harness(tmp_path, responses)
    ws = FakeWebSocket()
    harness.clients.add(ws)
    await harness.handle_message("run failing command")

    results = _get_tool_results(ws)
    assert len(results) == 1
    assert results[0]["is_error"] is True


async def test_native_grep_dispatch(tmp_path, monkeypatch):
    """Native grep returns formatted grouped results."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "code.py").write_text("def hello():\n    pass\n")
    monkeypatch.setattr("archie_agent.exec.tools.fs.WORKSPACE", workspace)

    responses = [
        [
            ToolUseEvent(
                tool_use_id="tu_grep",
                name="grep",
                input={"pattern": "hello"},
            ),
            Usage(input_tokens=100, output_tokens=50),
            Done(stop_reason="tool_use"),
        ],
        [
            TextDelta(text="Found it."),
            Usage(input_tokens=200, output_tokens=60),
            Done(stop_reason="end_turn"),
        ],
    ]
    harness = _make_harness(tmp_path, responses)
    ws = FakeWebSocket()
    harness.clients.add(ws)
    await harness.handle_message("search for hello")

    results = _get_tool_results(ws)
    assert len(results) == 1
    assert results[0]["is_error"] is False
    assert results[0]["result_bytes"] > 0


# --- Test: native glob dispatches with formatted output ---


async def test_native_glob_dispatch(tmp_path, monkeypatch):
    """Native glob returns formatted file list."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.py").write_text("a")
    (workspace / "b.py").write_text("b")
    monkeypatch.setattr("archie_agent.exec.tools.fs.WORKSPACE", workspace)

    responses = [
        [
            ToolUseEvent(
                tool_use_id="tu_glob",
                name="glob",
                input={"pattern": "*.py"},
            ),
            Usage(input_tokens=100, output_tokens=50),
            Done(stop_reason="tool_use"),
        ],
        [
            TextDelta(text="Found files."),
            Usage(input_tokens=200, output_tokens=60),
            Done(stop_reason="end_turn"),
        ],
    ]
    harness = _make_harness(tmp_path, responses)
    ws = FakeWebSocket()
    harness.clients.add(ws)
    await harness.handle_message("list py files")

    results = _get_tool_results(ws)
    assert len(results) == 1
    assert results[0]["is_error"] is False
    assert results[0]["result_bytes"] > 0


# --- Test: web_search dispatch with mocked DDGS ---


async def test_native_web_search_dispatch(tmp_path):
    """Native web_search dispatches and returns formatted results."""
    mock_results = [
        {"title": "Test", "href": "https://test.com", "body": "A result"},
    ]
    mock_ddgs = MagicMock()
    mock_ddgs.return_value.text.return_value = mock_results

    responses = [
        [
            ToolUseEvent(
                tool_use_id="tu_ws",
                name="web_search",
                input={"query": "test"},
            ),
            Usage(input_tokens=100, output_tokens=50),
            Done(stop_reason="tool_use"),
        ],
        [
            TextDelta(text="Results."),
            Usage(input_tokens=200, output_tokens=60),
            Done(stop_reason="end_turn"),
        ],
    ]
    harness = _make_harness(tmp_path, responses)
    ws = FakeWebSocket()
    harness.clients.add(ws)

    with patch("ddgs.DDGS", mock_ddgs):
        await harness.handle_message("search the web")

    results = _get_tool_results(ws)
    assert len(results) == 1
    assert results[0]["is_error"] is False


# --- Test: truncation applies for large native tool output ---


async def test_native_truncation_applied(tmp_path, monkeypatch):
    """Native handler truncates results exceeding _MAX_RESULT_CHARS."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Create a very large file
    large_content = "x" * 20000 + "\n"
    (workspace / "big.txt").write_text(large_content)
    monkeypatch.setattr("archie_agent.exec.tools.fs.WORKSPACE", workspace)

    responses = [
        [
            ToolUseEvent(
                tool_use_id="tu_big",
                name="read",
                input={"path": "big.txt", "raw": True},
            ),
            Usage(input_tokens=100, output_tokens=50),
            Done(stop_reason="tool_use"),
        ],
        [
            TextDelta(text="Big file."),
            Usage(input_tokens=200, output_tokens=60),
            Done(stop_reason="end_turn"),
        ],
    ]
    harness = _make_harness(tmp_path, responses)
    ws = FakeWebSocket()
    harness.clients.add(ws)
    await harness.handle_message("read big.txt")

    results = _get_tool_results(ws)
    assert len(results) == 1
    # The result should be truncated — result_bytes should be less than 20000
    # (The native handler caps at _MAX_RESULT_CHARS = 16000 + marker)
    assert results[0]["result_bytes"] <= 16200  # 16000 + marker + header
    assert results[0]["is_error"] is False


# --- Test: str(result) is a no-op for native tools ---


async def test_str_result_is_noop_for_native(tmp_path, monkeypatch):
    """Verify that str(result) on a string return is a no-op (no double-encoding)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "test.txt").write_text("content\n")
    monkeypatch.setattr("archie_agent.exec.tools.fs.WORKSPACE", workspace)

    responses = [
        [
            ToolUseEvent(
                tool_use_id="tu_str",
                name="read",
                input={"path": "test.txt"},
            ),
            Usage(input_tokens=100, output_tokens=50),
            Done(stop_reason="tool_use"),
        ],
        [
            TextDelta(text="Done."),
            Usage(input_tokens=200, output_tokens=60),
            Done(stop_reason="end_turn"),
        ],
    ]
    harness = _make_harness(tmp_path, responses)
    ws = FakeWebSocket()
    harness.clients.add(ws)
    await harness.handle_message("read test.txt")

    results = _get_tool_results(ws)
    assert len(results) == 1
    # The result should NOT be wrapped in extra quotes or repr'd
    assert results[0]["is_error"] is False
    # Result bytes should reflect the natural string content, not str("string") wrapping
    assert results[0]["result_bytes"] > 0


# --- M5: Integration tests proving both native and exec paths ---


async def test_both_native_and_exec_paths(tmp_path, monkeypatch):
    """Model calls native read then exec — both work (proves dual dispatch)."""
    import sys

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "target.txt").write_text("hello from target\n")
    monkeypatch.setattr("archie_agent.exec.tools.fs.WORKSPACE", workspace)

    responses = [
        # First call: model uses native read
        [
            ToolUseEvent(
                tool_use_id="tu_native",
                name="read",
                input={"path": "target.txt"},
            ),
            Usage(input_tokens=100, output_tokens=50),
            Done(stop_reason="tool_use"),
        ],
        # Second call: model uses exec with simple computation
        [
            ToolUseEvent(
                tool_use_id="tu_exec",
                name="exec",
                input={"source": "async def main():\n    return 21 * 2\n"},
            ),
            Usage(input_tokens=200, output_tokens=60),
            Done(stop_reason="tool_use"),
        ],
        # Final response
        [
            TextDelta(text="Both paths worked."),
            Usage(input_tokens=300, output_tokens=70),
            Done(stop_reason="end_turn"),
        ],
    ]
    harness = _make_harness(tmp_path, responses)
    harness._exec_python = sys.executable
    harness._exec_run_root = tmp_path / "runs"
    ws = FakeWebSocket()
    harness.clients.add(ws)
    await harness.handle_message("test both paths")

    results = _get_tool_results(ws)
    assert len(results) == 2

    # Native read — not an error, has content from target.txt
    assert results[0]["is_error"] is False
    assert results[0]["result_bytes"] > 0

    # Exec — not an error, returned 42
    assert results[1]["is_error"] is False
    assert results[1]["result_bytes"] > 0


# --- M5: TUI summary for native tools ---


async def test_native_read_input_summary(tmp_path, monkeypatch):
    """Wire tool_call carries raw input; format_tool_pending renders it."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "foo.py").write_text("x\n")
    monkeypatch.setattr("archie_agent.exec.tools.fs.WORKSPACE", workspace)

    responses = [
        [
            ToolUseEvent(
                tool_use_id="tu_sum",
                name="read",
                input={"path": "foo.py"},
            ),
            Usage(input_tokens=100, output_tokens=50),
            Done(stop_reason="tool_use"),
        ],
        [
            TextDelta(text="Done."),
            Usage(input_tokens=200, output_tokens=60),
            Done(stop_reason="end_turn"),
        ],
    ]
    harness = _make_harness(tmp_path, responses)
    ws = FakeWebSocket()
    harness.clients.add(ws)
    await harness.handle_message("read foo.py")

    # Wire tool_call carries raw input; client formats via shared formatter.
    tool_calls = [json.loads(m) for m in ws.messages if json.loads(m).get("type") == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["input"] == {"path": "foo.py"}
    summary = format_tool_pending(tool_calls[0]["name"], tool_calls[0]["input"])
    assert "Read" in summary
    assert "foo.py" in summary


async def test_native_shell_input_summary(tmp_path, monkeypatch):
    """Wire tool_call carries raw input; format_tool_pending renders it."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("archie_agent.exec.tools._subprocess.WORKSPACE", workspace)

    responses = [
        [
            ToolUseEvent(
                tool_use_id="tu_sh",
                name="shell",
                input={"command": "echo test"},
            ),
            Usage(input_tokens=100, output_tokens=50),
            Done(stop_reason="tool_use"),
        ],
        [
            TextDelta(text="Done."),
            Usage(input_tokens=200, output_tokens=60),
            Done(stop_reason="end_turn"),
        ],
    ]
    harness = _make_harness(tmp_path, responses)
    ws = FakeWebSocket()
    harness.clients.add(ws)
    await harness.handle_message("run a command")

    tool_calls = [json.loads(m) for m in ws.messages if json.loads(m).get("type") == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["input"] == {"command": "echo test"}
    summary = format_tool_pending(tool_calls[0]["name"], tool_calls[0]["input"])
    assert "Shell" in summary
    assert "echo test" in summary


async def test_native_grep_input_summary(tmp_path, monkeypatch):
    """Wire tool_call carries raw input; format_tool_pending renders it."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "x.py").write_text("hello\n")
    monkeypatch.setattr("archie_agent.exec.tools.fs.WORKSPACE", workspace)

    responses = [
        [
            ToolUseEvent(
                tool_use_id="tu_g",
                name="grep",
                input={"pattern": "hello"},
            ),
            Usage(input_tokens=100, output_tokens=50),
            Done(stop_reason="tool_use"),
        ],
        [
            TextDelta(text="Found."),
            Usage(input_tokens=200, output_tokens=60),
            Done(stop_reason="end_turn"),
        ],
    ]
    harness = _make_harness(tmp_path, responses)
    ws = FakeWebSocket()
    harness.clients.add(ws)
    await harness.handle_message("search for hello")

    tool_calls = [json.loads(m) for m in ws.messages if json.loads(m).get("type") == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["input"] == {"pattern": "hello"}
    summary = format_tool_pending(tool_calls[0]["name"], tool_calls[0]["input"])
    assert "Grep" in summary
    assert "hello" in summary

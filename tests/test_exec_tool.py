"""Tests for exec/tool.py — host-side handler + registered exec tool.

Tests use sys.executable as the Python interpreter and tmp_path as run_root.
"""

import sys

import pytest
from archie_agent.exec.envelope import Envelope, ErrorInfo
from archie_agent.exec.tool import (
    _build_exec_description,
    create_registry,
    format_result,
    run_exec,
)


@pytest.fixture
def run_root(tmp_path):
    return tmp_path / "runs"


async def test_run_exec_simple(run_root):
    envelope = await run_exec(
        "async def main():\n    return 21 * 2\n",
        run_root=run_root,
        python=sys.executable,
    )
    assert envelope.ok is True
    assert envelope.return_value == 42


async def test_run_exec_with_print(run_root):
    envelope = await run_exec(
        'async def main():\n    print("hello")\n    return "done"\n',
        run_root=run_root,
        python=sys.executable,
    )
    assert envelope.ok is True
    assert "hello" in envelope.stdout
    assert envelope.return_value == "done"


async def test_run_exec_error(run_root):
    envelope = await run_exec(
        'async def main():\n    raise ValueError("oops")\n',
        run_root=run_root,
        python=sys.executable,
    )
    assert envelope.ok is False
    assert envelope.error.type == "ValueError"
    assert "oops" in envelope.error.message


async def test_run_exec_contract_error(run_root):
    envelope = await run_exec(
        "x = 1\n",
        run_root=run_root,
        python=sys.executable,
    )
    assert envelope.ok is False
    assert envelope.error.type == "ContractError"


async def test_run_exec_bad_python(run_root):
    """OSError on bad python path → RunnerCrash envelope."""
    envelope = await run_exec(
        "async def main(): return 1\n",
        run_root=run_root,
        python="/nonexistent/python",
    )
    assert envelope.ok is False
    assert envelope.error.type == "RunnerCrash"
    assert "spawn" in envelope.error.message.lower() or "Failed" in envelope.error.message


async def test_run_exec_on_start_callback(run_root):
    """on_start callback receives the subprocess Process."""
    captured = []

    def on_start(proc):
        captured.append(proc)

    await run_exec(
        "async def main(): return 1\n",
        run_root=run_root,
        python=sys.executable,
        on_start=on_start,
    )
    assert len(captured) == 1
    assert captured[0].returncode == 0


# --- format_result ---


def test_format_result_ok():
    env = Envelope(
        ok=True,
        return_value={"answer": 42},
        stdout="some output\n",
        duration_ms=150,
    )
    result = format_result(env)
    assert '{"answer": 42}' in result
    assert "some output" in result
    assert "150ms" in result


def test_format_result_error():
    env = Envelope(
        ok=False,
        error=ErrorInfo(type="ValueError", message="bad", traceback="tb here"),
    )
    result = format_result(env)
    assert "ValueError" in result
    assert "bad" in result
    assert "tb here" in result


def test_format_result_truncation():
    env = Envelope(
        ok=True,
        return_value="x" * 20000,
    )
    result = format_result(env)
    assert len(result) <= 16_000 + 50  # some margin for the truncation marker
    assert "[…truncated]" in result


# --- description auto-gen ---


def test_build_exec_description_contains_tools():
    desc = _build_exec_description()
    assert "read" in desc
    assert "write" in desc
    assert "edit" in desc
    assert "grep" in desc
    assert "glob" in desc
    assert "shell" in desc
    assert "async def main()" in desc


# --- create_registry ---


def test_create_registry():
    reg = create_registry()
    spec = reg.get("exec")
    assert spec is not None
    assert spec.name == "exec"
    assert "source" in spec.schema["properties"]
    config = reg.to_tool_config()
    assert len(config) == 1
    assert config[0]["name"] == "exec"


# --- error_envelope ---


def test_error_envelope():
    env = Envelope.error_envelope("RunnerCrash", "something went wrong")
    assert env.ok is False
    assert env.error.type == "RunnerCrash"
    assert "something went wrong" in env.error.message

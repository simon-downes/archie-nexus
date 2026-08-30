"""Tests for exec/runner.py — invoked directly via run(run_dir).

Tests write a main.py source into a tmp_path run dir and call run() directly.
Exec tools are available since the package is importable in the test environment.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from archie_agent.exec.envelope import Envelope
from archie_agent.exec.runner import run


@pytest.fixture
def run_dir(tmp_path):
    """Create a run directory."""
    return tmp_path


def _write_source(run_dir: Path, source: str) -> None:
    (run_dir / "main.py").write_text(source)


def _read_envelope(run_dir: Path) -> Envelope:
    return Envelope.read(run_dir)


def test_simple_return(run_dir):
    _write_source(run_dir, "async def main():\n    return 1 + 1\n")
    run(run_dir)
    env = _read_envelope(run_dir)
    assert env.ok is True
    assert env.return_value == 2
    assert env.error is None


def test_preinjected_modules_available(run_dir):
    """os, json, re, Path, and asyncio are usable without import."""
    _write_source(
        run_dir,
        "async def main():\n"
        "    return {\n"
        '        "sep": os.sep,\n'
        '        "dumped": json.dumps({"x": 1}),\n'
        '        "matched": bool(re.match(r"\\d+", "42")),\n'
        '        "name": Path("/workspace/src/app.py").name,\n'
        "    }\n",
    )
    run(run_dir)
    env = _read_envelope(run_dir)
    assert env.ok is True, env.error
    assert env.return_value == {
        "sep": "/",
        "dumped": '{"x": 1}',
        "matched": True,
        "name": "app.py",
    }


def test_string_return(run_dir):
    _write_source(run_dir, 'async def main():\n    return "hello"\n')
    run(run_dir)
    env = _read_envelope(run_dir)
    assert env.ok is True
    assert env.return_value == "hello"


def test_dict_return(run_dir):
    _write_source(run_dir, 'async def main():\n    return {"a": 1, "b": [2, 3]}\n')
    run(run_dir)
    env = _read_envelope(run_dir)
    assert env.ok is True
    assert env.return_value == {"a": 1, "b": [2, 3]}


def test_none_return(run_dir):
    _write_source(run_dir, "async def main():\n    pass\n")
    run(run_dir)
    env = _read_envelope(run_dir)
    assert env.ok is True
    assert env.return_value is None


def test_unserialisable_return(run_dir):
    _write_source(run_dir, "async def main():\n    return object()\n")
    run(run_dir)
    env = _read_envelope(run_dir)
    assert env.ok is True
    assert env.return_repr is True
    assert "object" in env.return_value


def test_print_capture(run_dir):
    _write_source(run_dir, 'async def main():\n    print("hello stdout")\n    return 42\n')
    run(run_dir)
    env = _read_envelope(run_dir)
    assert env.ok is True
    assert "hello stdout" in env.stdout
    assert env.return_value == 42


def test_stderr_capture(run_dir):
    _write_source(
        run_dir,
        "import sys\nasync def main():\n    print('err', file=sys.stderr)\n    return 1\n",
    )
    run(run_dir)
    env = _read_envelope(run_dir)
    assert env.ok is True
    assert "err" in env.stderr


def test_exception(run_dir):
    _write_source(run_dir, 'async def main():\n    raise ValueError("bad")\n')
    run(run_dir)
    env = _read_envelope(run_dir)
    assert env.ok is False
    assert env.error.type == "ValueError"
    assert "bad" in env.error.message
    assert env.error.traceback


def test_contract_missing_main(run_dir):
    _write_source(run_dir, "x = 1\n")
    run(run_dir)
    env = _read_envelope(run_dir)
    assert env.ok is False
    assert env.error.type == "ContractError"
    assert "async def main()" in env.error.message


def test_contract_sync_main(run_dir):
    _write_source(run_dir, "def main():\n    return 1\n")
    run(run_dir)
    env = _read_envelope(run_dir)
    assert env.ok is False
    assert env.error.type == "ContractError"


def test_contract_main_with_required_args(run_dir):
    _write_source(run_dir, "async def main(x):\n    return x\n")
    run(run_dir)
    env = _read_envelope(run_dir)
    assert env.ok is False
    assert env.error.type == "ContractError"
    assert "must not require arguments" in env.error.message


def test_syntax_error(run_dir):
    _write_source(run_dir, "def main(\n")
    run(run_dir)
    env = _read_envelope(run_dir)
    assert env.ok is False
    assert env.error.type == "SyntaxError"


def test_missing_source_file(run_dir):
    # Don't write main.py
    run(run_dir)
    env = _read_envelope(run_dir)
    assert env.ok is False
    assert env.error.type == "ContractError"
    assert "not found" in env.error.message


def test_tool_call_recorded(run_dir):
    """Exec tools should be available and calls recorded in the audit log."""
    # Patch WORKSPACE so write goes to our tmp_path
    workspace = run_dir / "workspace"
    workspace.mkdir()
    _write_source(
        run_dir,
        'async def main():\n    await write(path="test.txt", content="hi")\n    return "done"\n',
    )
    with patch("archie_agent.exec.tools.fs.WORKSPACE", workspace):
        run(run_dir)

    env = _read_envelope(run_dir)
    assert env.ok is True
    assert env.return_value == "done"
    assert len(env.calls) == 1
    assert env.calls[0].fn == "write"
    assert env.calls[0].ok is True
    assert (workspace / "test.txt").read_text() == "hi"


def test_tool_error_surfaces(run_dir):
    """ToolError from exec tools surfaces as an exception in the envelope."""
    _write_source(
        run_dir,
        'async def main():\n    return await read(path="nonexistent.txt")\n',
    )
    # Don't patch WORKSPACE — the file won't exist under /workspace either
    # but let's patch to a tmp that has no file
    workspace = run_dir / "workspace"
    workspace.mkdir()
    with patch("archie_agent.exec.tools.fs.WORKSPACE", workspace):
        run(run_dir)

    env = _read_envelope(run_dir)
    assert env.ok is False
    assert "FileNotFoundError" in env.error.type


def test_duration_not_in_envelope(run_dir):
    _write_source(run_dir, "async def main():\n    return 1\n")
    run(run_dir)
    env = _read_envelope(run_dir)
    assert not hasattr(env, "duration_ms")

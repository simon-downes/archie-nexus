"""Tool processes are the OOM killer's first choice (094)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from archie_agent import oom
from archie_agent.exec.tool import run_exec as run_exec_tool
from archie_agent.exec.tools._subprocess import run_exec
from archie_agent.exec.tools.shell import shell

SCORE = Path("/proc/self/oom_score_adj")

pytestmark = pytest.mark.skipif(not SCORE.exists(), reason="needs Linux /proc")


def _own_score() -> str:
    return SCORE.read_text().strip()


async def test_shell_commands_and_children_run_with_score_1000():
    before = _own_score()
    assert await shell("cat /proc/self/oom_score_adj") == "[exit: 0]\n1000"
    assert await shell("sh -c 'cat /proc/self/oom_score_adj'") == "[exit: 0]\n1000"
    assert _own_score() == before


async def test_the_prefix_leaves_output_and_exit_code_alone():
    assert await shell("echo hi; exit 3") == "[exit: 3]\nhi"
    assert await shell("# a comment first\necho two\necho lines") == "[exit: 0]\ntwo\nlines"


async def test_an_unwritable_score_file_prints_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(oom, "OOM_SCORE_ADJ_PATH", str(tmp_path / "missing" / "oom_score_adj"))
    assert await shell("echo hi") == "[exit: 0]\nhi"
    result = await run_exec("echo", "hi")
    assert (result.returncode, result.stdout, result.stderr) == (0, "hi\n", "")


async def test_run_exec_runs_with_score_1000():
    result = await run_exec("cat", "/proc/self/oom_score_adj")
    assert (result.returncode, result.stdout.strip(), result.stderr) == (0, "1000", "")


async def test_run_exec_of_a_missing_program_still_raises():
    with pytest.raises(FileNotFoundError):
        await run_exec("/nonexistent/program")


async def test_exec_runner_runs_with_score_1000_in_its_own_group(tmp_path):
    seen = []
    envelope = await run_exec_tool(
        "from pathlib import Path\n"
        "async def main():\n"
        "    return Path('/proc/self/oom_score_adj').read_text().strip()\n",
        run_root=tmp_path,
        python=sys.executable,
        on_start=lambda proc: seen.append((proc.pid, os.getpgid(proc.pid))),
    )
    assert envelope.ok, envelope.error
    assert envelope.return_value == "1000"
    pid, pgid = seen[0]
    assert pgid == pid != os.getpgid(0)

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


@pytest.fixture
def events(monkeypatch, tmp_path) -> Path:
    """A ``memory.events`` stand-in with no OOM kills yet."""
    path = tmp_path / "memory.events"
    path.write_text("oom 0\noom_kill 0\n")
    monkeypatch.setattr(oom, "MEMORY_EVENTS", path)
    return path


def _bump(events: Path) -> str:
    return f"printf 'oom 1\\noom_kill 1\\n' > {events}"


async def test_shell_result_says_the_oom_killer_ran(events):
    result = await shell(f"echo out; {_bump(events)}; echo err >&2; exit 137")
    assert result == f"[exit: 137]\nout\nerr\n{oom.tool_warning(1)}"
    assert "the out-of-memory killer killed 1 process(es)" in result


async def test_shell_result_has_no_warning_without_a_kill(events):
    assert await shell("echo quiet") == "[exit: 0]\nquiet"


async def test_runner_crash_says_the_oom_killer_ran(events, tmp_path):
    envelope = await run_exec_tool(
        "import os, signal\n"
        "from pathlib import Path\n"
        "async def main():\n"
        f"    Path({str(events)!r}).write_text('oom_kill 2\\n')\n"
        "    os.kill(os.getpid(), signal.SIGKILL)\n",
        run_root=tmp_path / "runs",
        python=sys.executable,
    )
    assert envelope.error.type == "RunnerCrash"
    assert "code=-9" in envelope.error.message
    assert oom.tool_warning(2) in envelope.error.message


def test_oom_kill_count_reads_the_counter(events):
    assert oom.oom_kill_count() == 0
    events.write_text("low 0\noom_kill 7\noom_group_kill 0\n")
    assert oom.oom_kill_count() == 7
    events.unlink()
    assert oom.oom_kill_count() is None
    assert oom.kills_since(None) == 0

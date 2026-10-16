"""Tests for the build-it skill's worktree.py, run as a subprocess against a temp repo."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "persona/skills/build-it/scripts/worktree.py"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, env={**os.environ, **GIT_ENV}
    )


def worktree_py(*args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    git("add", "README.md", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)
    return root


def build_with_plan(repo: Path, outcome_lines: str) -> dict:
    started = worktree_py("start", "--repo", str(repo), "--slug", "demo")
    wt = Path(started["worktree"])
    plan = wt / started["plan_path"]
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"# {started['number']} — Demo\n\nStatus: done\n"
        f"Branch: {started['branch']} (base: main @ {started['base_short_sha']})\n\n"
        f"## Outcome\n\n{outcome_lines}\n",
        encoding="utf-8",
    )
    git("add", started["plan_path"], cwd=wt)
    git("commit", "-q", "-m", "docs: plan", cwd=wt)
    return started


def listed(repo: Path, branch: str) -> dict:
    builds = worktree_py("list", "--repo", str(repo))["builds"]
    return next(b for b in builds if b["branch"] == branch)


@pytest.mark.parametrize(
    ("outcome_lines", "unfinished"),
    [
        ("PR: pending", True),
        ("PR: https://github.com/o/r/pull/1", True),
        ("PR: https://github.com/o/r/pull/1\nCI: pending", True),
        ("PR: https://github.com/o/r/pull/1\nCI: passed", False),
        ("PR: no remote", False),
        ("PR: no change", False),
    ],
)
def test_list_reports_done_builds_awaiting_pr_or_ci_as_unfinished(
    repo: Path, outcome_lines: str, unfinished: bool
):
    started = build_with_plan(repo, outcome_lines)

    assert listed(repo, started["branch"])["unfinished"] is unfinished

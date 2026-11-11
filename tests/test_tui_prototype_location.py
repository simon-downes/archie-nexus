"""Prototype session locations: repo, worktree and branch from logged tool calls."""

import json
from pathlib import Path

import pytest
from archie_cli.tui.prototype import location


def _repo(path: Path, branch: str) -> Path:
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
    return path


def _worktree(repo: Path, name: str, branch: str, where: Path) -> Path:
    """A worktree as git writes it: a .git file whose gitdir is a /workspace path."""
    gitdir = repo / ".git" / "worktrees" / name
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
    where.mkdir(parents=True)
    container = "/workspace/" + str(gitdir.relative_to(repo.parent))
    (where / ".git").write_text(f"gitdir: {container}\n")
    return where


def _log(tmp_path: Path, *calls: tuple[str, dict]) -> Path:
    path = tmp_path / "session.jsonl"
    lines = [
        json.dumps({"type": "tool_call", "scope": None, "name": name, "input": tool_input})
        for name, tool_input in calls
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    _repo(root / "app", "main")
    _repo(root / "lib", "feature/x")
    return root


def test_git_activity_outranks_later_edits(tmp_path, workspace):
    log = _log(
        tmp_path,
        ("shell", {"command": "cd /workspace/lib && git status"}),
        ("edit", {"path": "/workspace/app/main.py"}),
    )
    found = location.locate(log, "ws", workspace)
    assert (found.repo, found.worktree, found.branch) == ("lib", "", "feature/x")


def test_edits_outrank_reads_and_relative_paths_resolve(tmp_path, workspace):
    log = _log(
        tmp_path,
        ("edit", {"path": "app/src/a.py"}),
        ("read", {"path": "/workspace/lib/README.md"}),
    )
    assert location.locate(log, "ws", workspace).repo == "app"


def test_cd_is_relative_to_the_shell_cwd(tmp_path, workspace):
    log = _log(tmp_path, ("shell", {"command": "cd lib; git log -1", "cwd": "/workspace"}))
    assert location.locate(log, "ws", workspace).repo == "lib"


def test_worktree_gives_its_name_and_the_main_repo(tmp_path, workspace):
    tree = workspace / "app" / ".git" / "build-it-worktrees" / "079-question-tool"
    _worktree(workspace / "app", "079-question-tool", "plan/079", tree)
    log = _log(
        tmp_path,
        (
            "shell",
            {"command": "git -C /workspace/app/.git/build-it-worktrees/079-question-tool diff"},
        ),
    )
    found = location.locate(log, "ws", workspace)
    assert (found.repo, found.worktree, found.branch) == ("app", "079-question-tool", "plan/079")


def test_a_workspace_that_is_itself_a_repo(tmp_path):
    root = _repo(tmp_path / "single", "main")
    log = _log(tmp_path, ("edit", {"path": "/workspace/src/a.py"}))
    assert location.locate(log, "single", root).repo == "single"


def test_paths_outside_the_workspace_and_no_paths_give_nothing(tmp_path, workspace):
    outside = _log(tmp_path, ("read", {"path": "/tmp/x.png"}), ("shell", {"command": "ls ~"}))
    assert location.locate(outside, "ws", workspace) is None
    assert location.locate(_log(tmp_path), "ws", workspace) is None


def test_branch_switches_show_on_refresh(tmp_path, workspace):
    found = location.locate(_log(tmp_path, ("edit", {"path": "app/a.py"})), "ws", workspace)
    (workspace / "app" / ".git" / "HEAD").write_text("ref: refs/heads/other\n")
    found.refresh()
    assert found.branch == "other"

    (workspace / "app" / ".git" / "HEAD").write_text("0123456789abcdef\n")
    found.refresh()
    assert found.branch == "01234567"  # detached: a short hash

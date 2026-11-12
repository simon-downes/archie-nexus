"""Tests for the workspace scanner and ``/api/workspaces``."""

import os
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

import msgspec
import pytest
from archie_orchestrator import workspaces
from archie_orchestrator.app import app
from archie_orchestrator.workspaces import (
    list_workspaces,
    origin_url,
    read_workspace,
    resolve_workspace,
    scan_repos,
)
from archie_shared.schemas import GlobalConfig, NexusConfig
from archie_shared.workspaces import Repo, Workspace, Worktree
from httpx import ASGITransport, AsyncClient

from tests.session_fakes import record, serve

ORIGIN = "git@github.com:example/app.git"


def git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def make_repo(path: Path, *, branch: str = "main", origin: str | None = ORIGIN) -> str:
    """A repo at ``path`` with one commit; returns the commit hash."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    if origin is not None:
        git(path, "remote", "add", "origin", origin)
    git(path, "commit", "-q", "--allow-empty", "-m", "init")
    return git(path, "rev-parse", "HEAD")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "root" / "proj"
    directory.mkdir(parents=True)
    return directory


# ---------------------------------------------------------------------------
# Scanning repos
# ---------------------------------------------------------------------------


def test_children_that_are_repos_are_listed_with_origin_branch_and_commit(workspace):
    app_sha = make_repo(workspace / "app")
    make_repo(workspace / "lib", branch="develop", origin=None)
    (workspace / "notes").mkdir()
    (workspace / "README.md").write_text("hi")

    assert scan_repos(workspace) == [
        Repo(path="app", origin=ORIGIN, branch="main", commit=app_sha),
        Repo(
            path="lib",
            origin=None,
            branch="develop",
            commit=git(workspace / "lib", "rev-parse", "HEAD"),
        ),
    ]


def test_a_workspace_that_is_a_repo_is_listed_first_with_an_empty_path(workspace):
    sha = make_repo(workspace)
    make_repo(workspace / "vendor")

    repos = scan_repos(workspace)

    assert [repo.path for repo in repos] == ["", "vendor"]
    assert repos[0].commit == sha


def test_hidden_children_symlinks_and_grandchildren_are_not_scanned(workspace, tmp_path):
    make_repo(workspace / ".cache")
    make_repo(tmp_path / "elsewhere")
    (workspace / "link").symlink_to(tmp_path / "elsewhere")
    make_repo(workspace / "group" / "nested")

    assert scan_repos(workspace) == []


def test_a_detached_head_has_no_branch_and_its_commit(workspace):
    sha = make_repo(workspace / "app")
    git(workspace / "app", "checkout", "-q", "--detach")

    [repo] = scan_repos(workspace)

    assert (repo.branch, repo.commit) == (None, sha)


def test_branch_commits_resolve_from_packed_refs(workspace):
    sha = make_repo(workspace / "app")
    git(workspace / "app", "pack-refs", "--all")
    assert not (workspace / "app" / ".git" / "refs" / "heads" / "main").exists()

    [repo] = scan_repos(workspace)

    assert repo.commit == sha


def test_a_branch_with_no_commits_has_no_commit(workspace):
    (workspace / "app").mkdir()
    git(workspace / "app", "init", "-q", "-b", "main")

    [repo] = scan_repos(workspace)

    assert (repo.branch, repo.commit) == ("main", None)


def test_a_separate_git_dir_inside_the_workspace_is_a_repo(workspace):
    (workspace / "app").mkdir()
    git(workspace / "app", "init", "-q", "-b", "main", "--separate-git-dir", "../.app-git")

    [repo] = scan_repos(workspace)

    assert (repo.path, repo.branch) == ("app", "main")


# ---------------------------------------------------------------------------
# Worktrees
# ---------------------------------------------------------------------------


def test_linked_worktrees_are_listed_under_their_repo_not_as_repos(workspace):
    sha = make_repo(workspace / "app")
    git(workspace / "app", "worktree", "add", "-q", "-b", "feat/x", "../app-x")
    git(workspace / "app", "worktree", "add", "-q", "-b", "fix/y", ".git/wt/fix-y")

    [repo] = scan_repos(workspace)

    assert repo.path == "app"
    assert repo.worktrees == [
        Worktree(path="app-x", branch="feat/x", commit=sha),
        Worktree(path="app/.git/wt/fix-y", branch="fix/y", commit=sha),
    ]


def _container_worktree(workspace: Path, name: str, *, checkout: bool) -> None:
    """A worktree as ``git worktree add`` leaves it when run inside a session."""
    admin = workspace / "app" / ".git" / "worktrees" / name
    admin.mkdir(parents=True)
    (admin / "gitdir").write_text(f"/workspace/app/.git/wt/{name}/.git\n")
    (admin / "commondir").write_text("../..\n")
    (admin / "HEAD").write_text(f"ref: refs/heads/{name}\n")
    if checkout:
        tree = workspace / "app" / ".git" / "wt" / name
        tree.mkdir(parents=True)
        (tree / ".git").write_text(f"gitdir: /workspace/app/.git/worktrees/{name}\n")


def test_worktrees_recorded_with_container_paths_map_to_the_workspace(workspace):
    make_repo(workspace / "app")
    _container_worktree(workspace, "present", checkout=True)
    _container_worktree(workspace, "gone", checkout=False)

    [repo] = scan_repos(workspace)

    assert [(w.path, w.branch, w.commit, w.missing) for w in repo.worktrees] == [
        ("app/.git/wt/gone", "gone", None, True),
        ("app/.git/wt/present", "present", None, False),
    ]


def test_worktrees_recorded_with_relative_paths_resolve_from_their_admin_dir(workspace):
    make_repo(workspace / "app")
    _container_worktree(workspace, "rel", checkout=True)
    admin = workspace / "app" / ".git" / "worktrees" / "rel"
    (admin / "gitdir").write_text("../../wt/rel/.git\n")  # git >= 2.48 relative paths

    [repo] = scan_repos(workspace)

    assert [(w.path, w.missing) for w in repo.worktrees] == [("app/.git/wt/rel", False)]


def test_host_paths_inside_a_workspace_under_the_container_prefix_are_kept(workspace, monkeypatch):
    # As on a Linux host whose workspace_root is itself /workspace.
    monkeypatch.setattr(workspaces, "CONTAINER_WORKSPACE", str(workspace.parent))
    make_repo(workspace / "app")
    git(workspace / "app", "worktree", "add", "-q", "-b", "feat/x", "../app-x")

    [repo] = scan_repos(workspace)

    assert [(w.path, w.missing) for w in repo.worktrees] == [("app-x", False)]


def test_a_worktree_outside_the_workspace_keeps_its_host_path(workspace, tmp_path):
    sha = make_repo(workspace / "app")
    outside = tmp_path / "scratch"
    git(workspace / "app", "worktree", "add", "-q", "-b", "spike", str(outside))

    [repo] = scan_repos(workspace)

    [worktree] = repo.worktrees
    # Its HEAD and refs live in the repo, inside the workspace, so they are still read.
    assert (Path(worktree.path), worktree.branch, worktree.commit) == (outside, "spike", sha)


# ---------------------------------------------------------------------------
# Untrusted files
# ---------------------------------------------------------------------------


def test_files_linked_outside_the_workspace_are_not_read(workspace, tmp_path):
    make_repo(workspace / "app")
    secret = tmp_path / "secret"
    secret.write_text('[remote "origin"]\n\turl = https://token@example.com/x\n')
    config = workspace / "app" / ".git" / "config"
    config.unlink()
    config.symlink_to(secret)

    [repo] = scan_repos(workspace)

    assert repo.origin is None


def test_a_git_file_pointing_outside_the_workspace_is_not_a_repo(workspace, tmp_path):
    make_repo(tmp_path / "outside")
    (workspace / "app").mkdir()
    (workspace / "app" / ".git").write_text(f"gitdir: {tmp_path / 'outside' / '.git'}\n")

    assert scan_repos(workspace) == []


def test_a_fifo_in_place_of_head_does_not_block(workspace):
    make_repo(workspace / "app")
    head = workspace / "app" / ".git" / "HEAD"
    head.unlink()
    os.mkfifo(head)
    result: list[list[Repo]] = []

    # Opening a FIFO for reading blocks until a writer appears; the scan must not.
    scan = threading.Thread(target=lambda: result.append(scan_repos(workspace)), daemon=True)
    scan.start()
    scan.join(timeout=5)

    assert not scan.is_alive(), "scan blocked on the FIFO"
    [[repo]] = result
    assert (repo.branch, repo.commit) == (None, None)


def test_an_oversized_head_is_ignored(workspace):
    make_repo(workspace / "app")
    (workspace / "app" / ".git" / "HEAD").write_text("ref: refs/heads/" + "x" * 70_000)

    [repo] = scan_repos(workspace)

    assert repo.branch is None


def test_a_ref_that_climbs_out_of_the_git_dir_is_not_followed(workspace):
    make_repo(workspace / "app")
    (workspace / "app" / ".git" / "HEAD").write_text("ref: refs/../../../README\n")
    (workspace / "README").write_text("0" * 40)

    [repo] = scan_repos(workspace)

    assert repo.commit is None


def test_a_loose_ref_linked_outside_the_workspace_is_not_read(workspace, tmp_path):
    make_repo(workspace / "app")
    (tmp_path / "sha").write_text("1" * 40)
    ref = workspace / "app" / ".git" / "refs" / "heads" / "main"
    ref.unlink()
    ref.symlink_to(tmp_path / "sha")

    [repo] = scan_repos(workspace)

    assert (repo.branch, repo.commit) == ("main", None)


@pytest.mark.parametrize(
    ("relative", "content"),
    [(".git", "gitdir: a\x00b\n"), (".git/HEAD", "ref: refs/heads/a\x00b\n")],
)
def test_a_nul_byte_in_a_git_file_does_not_fail_the_scan(workspace, relative, content):
    make_repo(workspace / "app")
    make_repo(workspace / "bad")
    target = workspace / "bad" / relative
    if target.is_dir():
        shutil.rmtree(target)
    target.write_text(content)

    assert [repo.path for repo in scan_repos(workspace)][0] == "app"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_an_unreadable_git_dir_does_not_fail_the_scan(workspace):
    make_repo(workspace / "app")
    make_repo(workspace / "bad")
    locked = workspace / "bad" / ".git"
    locked.chmod(0)
    try:
        repos = scan_repos(workspace)
    finally:
        locked.chmod(0o755)

    assert [repo.path for repo in repos if repo.commit] == ["app"]


def test_a_reftable_repo_has_no_branch_or_commit(workspace):
    (workspace / "app").mkdir()
    try:
        git(workspace / "app", "init", "-q", "-b", "main", "--ref-format=reftable")
    except subprocess.CalledProcessError:
        pytest.skip("git without reftable support")
    git(workspace / "app", "commit", "-q", "--allow-empty", "-m", "init")

    [repo] = scan_repos(workspace)

    assert (repo.branch, repo.commit) == (None, None)


def test_packed_refs_are_searched_only_up_to_their_cap(workspace, monkeypatch):
    sha = make_repo(workspace / "app")
    packed = workspace / "app" / ".git" / "packed-refs"
    filler = "".join(f"{'2' * 40} refs/tags/t{i}\n" for i in range(100))
    git(workspace / "app", "pack-refs", "--all")
    packed.write_text(filler + packed.read_text())

    [repo] = scan_repos(workspace)
    assert repo.commit == sha

    monkeypatch.setattr(workspaces, "MAX_PACKED_REFS_BYTES", len(filler) // 2)
    [repo] = scan_repos(workspace)
    assert (repo.branch, repo.commit) == ("main", None)


def test_repos_and_worktrees_are_capped(workspace, monkeypatch):
    for name in ("a", "b", "c"):
        make_repo(workspace / name)
    git(workspace / "a", "worktree", "add", "-q", "-b", "x", "../a-x")
    git(workspace / "a", "worktree", "add", "-q", "-b", "y", "../a-y")
    monkeypatch.setattr(workspaces, "MAX_REPOS", 2)
    monkeypatch.setattr(workspaces, "MAX_WORKTREES", 1)

    repos = scan_repos(workspace)

    assert [repo.path for repo in repos] == ["a", "b"]
    assert len(repos[0].worktrees) == 1


# ---------------------------------------------------------------------------
# origin_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ('[remote "origin"]\n\turl = git@github.com:a/b.git\n', "git@github.com:a/b.git"),
        ('[remote "upstream"]\n\turl = u\n[remote "origin"]\n\tURL = o\n', "o"),
        ('[remote "origin"]\n\tfetch = +refs/*\n\turl = "x y" # comment\n', "x y"),
        ('[remote "origin"] url = inline\n', "inline"),
        ("[remote.origin]\n\turl = legacy\n", "legacy"),
        ('[remote "Origin"]\n\turl = wrong-case\n', None),
        ('[core]\n\turl = not-a-remote\n[remote "upstream"]\n\turl = u\n', None),
        ('[remote "origin"]\n\turl = https://me:tok@github.com/a/b\n', "https://github.com/a/b"),
        ('[remote "origin"]\n\turl = ssh://git@host:22/a/b\n', "ssh://git@host:22/a/b"),
        ('[remote "origin"]\n\turl = https://tok@github.com/a/b\n', "https://github.com/a/b"),
        ('[remote "origin"]\n\turl = ssh://me:pw@host/a\n', "ssh://me@host/a"),
        ('[remote "origin"]\n\turl = https://h/r.git?access_token=s#f\n', "https://h/r.git"),
        (
            '[remote "origin"]\n\turl = persistent-https::https://t@h/r\n',
            "persistent-https::https://h/r",
        ),
        ('[remote "origin"]\n\turl = ssh://git@[::1]:22/r\n', "ssh://git@[::1]:22/r"),
        ("", None),
    ],
)
def test_origin_url(config, expected):
    assert origin_url(config) == expected


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------


def test_list_workspaces_sorts_by_name_and_skips_hidden_and_escaping_links(tmp_path):
    root = tmp_path / "root"
    for name in ("zeta", "alpha", ".hidden"):
        (root / name).mkdir(parents=True)
    (root / "file.txt").write_text("")
    (tmp_path / "outside").mkdir()
    (root / "escape").symlink_to(tmp_path / "outside")
    (root / "alias").symlink_to(root / "alpha")
    make_repo(root / "alpha" / "app")

    workspaces = list_workspaces(root)

    assert [w.name for w in workspaces] == ["alias", "alpha", "zeta"]
    assert [r.path for r in workspaces[1].repos] == ["app"]


def test_list_workspaces_with_a_missing_root_is_empty(tmp_path):
    assert list_workspaces(tmp_path / "missing") == []


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "missing"])
def test_read_workspace_rejects_names_that_are_not_workspaces(tmp_path, name):
    with pytest.raises(ValueError):
        read_workspace(tmp_path, name)


def test_resolve_workspace_accepts_a_root_of_slash():
    assert resolve_workspace(Path("/"), "tmp") == Path("/tmp").resolve()


def test_resolve_workspace_rejects_a_link_out_of_the_root(tmp_path):
    (tmp_path / "root").mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "root" / "escape").symlink_to(tmp_path / "outside")

    with pytest.raises(ValueError, match="resolves outside workspace_root"):
        resolve_workspace(tmp_path / "root", "escape")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture
def root(tmp_path: Path):
    root = tmp_path / "root"
    (root / "proj").mkdir(parents=True)
    (root / "other").mkdir()
    config = NexusConfig(global_=GlobalConfig(workspace_root=str(root)))
    with patch("archie_orchestrator.api.config_source.get", return_value=config):
        yield root


def no_processes():
    """Fail if the request starts any process: no Docker and no git."""
    return patch("subprocess.Popen", side_effect=AssertionError("process started"))


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _sessions():
    return [
        record("proj-01abc12345", status="working", started_at="2026-07-02T10:00:00+00:00"),
        record("proj-01abc12346", status="idle", started_at="2026-07-01T10:00:00+00:00"),
        record(
            "other-01abc12347",
            status="stopped",
            started_at="2026-07-03T10:00:00+00:00",
            ended_at="2026-07-03T11:00:00+00:00",
        ),
    ]


async def test_get_workspaces_lists_repos_and_session_counts(root):
    sha = make_repo(root / "proj" / "app")
    with serve(app, records=_sessions()), no_processes():
        async with _client() as http:
            response = await http.get("/api/workspaces")

    assert response.status_code == 200
    assert msgspec.json.decode(response.content, type=list[Workspace]) == [
        Workspace(name="other", live_sessions=0, last_session_at="2026-07-03T10:00:00+00:00"),
        Workspace(
            name="proj",
            live_sessions=2,
            last_session_at="2026-07-02T10:00:00+00:00",
            repos=[Repo(path="app", origin=ORIGIN, branch="main", commit=sha)],
        ),
    ]


async def test_get_workspaces_counts_a_live_session_not_yet_in_the_store(root):
    with serve(app) as tracker:
        live = record("proj-01abc12349", status="starting", started_at="2026-08-01T00:00:00+00:00")
        tracker._records[live.session_id] = live
        async with _client() as http:
            response = await http.get("/api/workspaces")

    proj = next(w for w in response.json() if w["name"] == "proj")
    assert (proj["live_sessions"], proj["last_session_at"]) == (1, "2026-08-01T00:00:00+00:00")


async def test_get_workspace_returns_one_workspace(root):
    sha = make_repo(root / "proj" / "app")
    with serve(app, records=_sessions()), no_processes():
        async with _client() as http:
            response = await http.get("/api/workspaces/proj")

    assert response.status_code == 200
    assert response.json() == {
        "name": "proj",
        "live_sessions": 2,
        "last_session_at": "2026-07-02T10:00:00+00:00",
        "repos": [
            {"path": "app", "origin": ORIGIN, "branch": "main", "commit": sha, "worktrees": []}
        ],
    }


@pytest.mark.parametrize("name", ["missing", ".hidden", "escape", "x" * 5000])
async def test_get_workspace_404s_for_names_that_are_not_workspaces(root, tmp_path, name):
    (root / ".hidden").mkdir()
    (tmp_path / "outside").mkdir()
    (root / "escape").symlink_to(tmp_path / "outside")
    with serve(app):
        async with _client() as http:
            response = await http.get(f"/api/workspaces/{name}")

    assert response.status_code == 404


@pytest.mark.parametrize("path", ["/api/workspaces", "/api/workspaces/proj"])
async def test_workspace_routes_with_an_unreadable_store_are_500(root, path):
    with serve(app) as tracker:
        with patch.object(
            tracker.store, "last_started_by_workspace", side_effect=sqlite3.OperationalError
        ):
            async with _client() as http:
                response = await http.get(path)

    assert (response.status_code, response.json()) == (500, {"error": "Session store unavailable"})


async def test_get_workspaces_with_a_bad_config_is_500(root):
    from archie_shared.config import ConfigError

    with (
        serve(app),
        patch("archie_orchestrator.api.config_source.get", side_effect=ConfigError("bad")),
    ):
        async with _client() as http:
            response = await http.get("/api/workspaces")

    assert (response.status_code, response.json()) == (500, {"error": "bad"})

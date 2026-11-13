"""Session locations: repo, worktree and branch from a session's tool calls (084 M3).

The resolution tests use real git repos in ``tmp_path``; the tracker tests drive
``SessionTracker`` with the fakes in ``tests/session_fakes.py``.
"""

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from archie_orchestrator import tracker as tracker_module
from archie_orchestrator.location import (
    GAVE_UP,
    MAX_CALL_PATHS,
    MAX_COMMAND_CHARS,
    MAX_ENTRIES,
    MAX_LOOKUPS,
    MAX_SEGMENTS,
    RANK_EDIT,
    RANK_GIT,
    RANK_OTHER,
    Candidate,
    LocationState,
    candidate,
    scan_log,
    shell_paths,
)
from archie_orchestrator.workspaces import (
    MAX_PATH_CHARS,
    MAX_PATH_COMPONENTS,
    locate,
    location_key,
    refresh_location,
)
from archie_shared.workspaces import SessionLocation

from tests.session_fakes import (
    PORT,
    SID,
    FakeAgent,
    FakeRuntime,
    handshake_frame,
    make_tracker,
    record,
    running,
    status_frame,
)
from tests.test_workspaces import git, make_repo


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """The workspace root; the workspace ``proj`` (the fakes' ``SID``) is inside it."""
    (tmp_path / "root" / "proj").mkdir(parents=True)
    return tmp_path / "root"


@pytest.fixture
def workspace(root: Path) -> Path:
    return root / "proj"


def call(name: str, *, turn: int = 1, scope: str | None = None, **tool_input) -> tuple:
    return name, tool_input, turn, scope


def where(workspace: Path, *calls: tuple) -> SessionLocation | None:
    """The location of a session that made ``calls`` (each with its result), in order."""
    state = LocationState()
    for n, (name, tool_input, turn, scope) in enumerate(calls):
        state.on_call(scope, f"tu{n}", name, tool_input, turn)
        state.on_result(scope, f"tu{n}")
    return state.best(lambda path: locate(workspace, path))[0]


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "tool_input", "expected"),
    [
        ("edit", {"path": "app/a.py"}, Candidate(RANK_EDIT, ("/workspace/app/a.py",))),
        ("write", {"path": "/workspace/b"}, Candidate(RANK_EDIT, ("/workspace/b",))),
        ("read", {"path": "x/../y"}, Candidate(RANK_OTHER, ("/workspace/y",))),
        ("glob", {"path": "lib", "pattern": "*.py"}, Candidate(RANK_OTHER, ("/workspace/lib",))),
        ("code", {"path": "."}, Candidate(RANK_OTHER, ("/workspace",))),
        ("shell", {"command": "ls"}, Candidate(RANK_OTHER, ("/workspace",))),
        (
            "shell",
            {"command": "git status", "cwd": "app"},
            Candidate(RANK_GIT, ("/workspace/app",)),
        ),
        # Untrusted input: wrong types, junk keys, empty dicts, paths outside /workspace.
        ("edit", {}, None),
        ("edit", {"path": 3, "junk": "x"}, None),
        ("read", {"path": "/tmp/x.png"}, None),
        ("read", {"path": "~/notes"}, None),
        ("read", {"path": "../../etc/passwd"}, None),
        ("shell", {"command": ["git"]}, None),
        ("shell", {"command": "ls", "cwd": "/tmp"}, None),
        ("exec", {"source": "open('/workspace/app/a')"}, None),
        (3, {"path": "a"}, None),
        ("edit", "app/a.py", None),
    ],
)
def test_candidates_from_tool_inputs(name, tool_input, expected):
    assert candidate(name, tool_input) == expected


@pytest.mark.parametrize(
    ("command", "cwd", "paths", "runs_git"),
    [
        ("cd app && cd ../lib; git log -1", None, ["/", "/app", "/lib"], True),
        ("cd /workspace/app\ncd src || true", "lib", ["/lib", "/app", "/app/src"], False),
        # A target the log can't resolve ends the walk.
        ("cd app && cd $DIR && cd lib && git status", None, ["/", "/app"], True),
        ("cd ~/x && git status", None, ["/"], True),
        ("cd `git rev-parse --show-toplevel` && ls", None, ["/"], False),
        ("cd", None, ["/"], False),
        # Quoted separators and words; heredoc bodies; comments.
        ('cd "my app" && echo "a; cd b && git x"', None, ["/", "/my app"], False),
        ("cat > f <<'EOF'\ncd app\ngit init\nEOF\ncd lib", None, ["/", "/lib"], False),
        ("cat <<-END\n\tcd app\n\tEND\ngit status", None, ["/"], True),
        ("cat <<<word\ncd app", None, ["/", "/app"], False),  # a here-string has no body
        ("ls # cd app", None, ["/"], False),
        # git -C, chained -C, -c options, prefixes, subshells.
        ("git -C app -C src status", None, ["/", "/app", "/app/src"], True),
        ("git -c core.pager=cat -C /workspace/lib log", None, ["/", "/lib"], True),
        ("GIT_PAGER=cat sudo git -C app diff", None, ["/", "/app"], True),
        ("(cd app && git status)", None, ["/", "/app"], True),
        ("echo git status", None, ["/"], False),
    ],
)
def test_shell_paths_follow_cd_chains_in_order(command, cwd, paths, runs_git):
    found, git_ran = shell_paths(command, cwd)
    assert [p.removeprefix("/workspace") or "/" for p in found] == paths
    assert git_ran is runs_git


def test_a_call_counts_only_once_its_result_is_seen(workspace):
    make_repo(workspace / "app")
    state = LocationState()
    assert state.on_call(None, "tu1", "edit", {"path": "app/a.py"}, 1)
    assert state.best(lambda p: locate(workspace, p)) == (None, False)
    assert not state.on_result(None, "other")
    assert state.on_result(None, "tu1")
    assert state.best(lambda p: locate(workspace, p))[0].repo == "app"
    # A call seen again (a log scan overlapping the stream) isn't counted twice.
    assert not state.on_call(None, "tu1", "edit", {"path": "app/a.py"}, 1)
    assert not state.on_result(None, "tu1")


# ---------------------------------------------------------------------------
# Ranking (D26)
# ---------------------------------------------------------------------------


def test_git_outranks_a_later_edit_in_the_same_turn(workspace):
    make_repo(workspace / "app")
    make_repo(workspace / "lib", branch="feature/x")
    found = where(
        workspace,
        call("shell", command="cd /workspace/lib && git status"),
        call("edit", path="/workspace/app/main.py"),
        call("read", path="app/README.md"),
    )
    assert (found.repo, found.worktree, found.branch) == ("lib", None, "feature/x")


def test_a_later_turns_edit_outranks_an_earlier_turns_git(workspace):
    make_repo(workspace / "app")
    make_repo(workspace / "lib")
    found = where(
        workspace,
        call("shell", command="git -C lib status", turn=1),
        call("edit", path="app/a.py", turn=2),
    )
    assert found.repo == "app"


def test_a_turn_without_a_resolvable_candidate_falls_back_to_earlier_turns(workspace):
    make_repo(workspace / "lib")
    found = where(
        workspace,
        call("edit", path="lib/a.py", turn=1),
        call("edit", path="notes/todo.md", turn=2),  # not in a repo
        call("read", path="/tmp/x", turn=3),  # outside the workspace
    )
    assert found.repo == "lib"


def test_relative_paths_and_cwd_are_relative_to_the_workspace(workspace):
    make_repo(workspace / "app")
    make_repo(workspace / "lib")
    assert where(workspace, call("edit", path="app/src/a.py")).repo == "app"
    assert where(workspace, call("shell", command="git log", cwd="lib")).repo == "lib"
    assert where(workspace, call("shell", command="git log", cwd="/workspace/lib")).repo == "lib"
    assert where(workspace, call("shell", command="cd ../lib && git log", cwd="app")).repo == "lib"


def test_cd_chains_are_followed_in_order(workspace):
    make_repo(workspace / "app")
    make_repo(workspace / "lib")
    found = where(workspace, call("shell", command="cd app && git status; cd ../lib && ls"))
    assert found.repo == "lib"
    stopped = where(workspace, call("shell", command="cd app && cd $OTHER && cd ../lib && ls"))
    assert stopped.repo == "app"


def test_a_bare_git_shell_in_a_workspace_that_is_a_repo_gives_the_workspace(workspace):
    sha = make_repo(workspace)
    found = where(workspace, call("shell", command="git status"))
    assert found == SessionLocation(repo="", branch="main", commit=sha)


def test_paths_outside_the_workspace_are_ignored(workspace, tmp_path):
    make_repo(workspace / "app")
    outside = tmp_path / "elsewhere"
    make_repo(outside)
    (workspace / "link").symlink_to(outside)
    calls = [
        call("read", path="/tmp/x.png"),
        call("shell", command="ls ~"),
        call("shell", command="cd /opt/archie && git log"),
        call("edit", path="../../outside.py"),
        call("edit", path="link/a.py"),  # resolves outside the workspace
    ]
    assert where(workspace, *calls) is None
    assert locate(workspace, "/workspace/link/a.py") is None
    assert locate(workspace, "/opt/archie") is None
    # The workspace directory itself isn't a repo here.
    assert where(workspace) is None


def test_subagent_calls_count_only_when_the_root_has_none(workspace):
    make_repo(workspace / "app")
    make_repo(workspace / "lib")
    only_subagent = where(workspace, call("edit", path="lib/a.py", scope="tc-1"))
    assert only_subagent.repo == "lib"
    both = where(
        workspace,
        call("read", path="app/a.py", turn=1),
        call("shell", command="git -C lib log", turn=2, scope="tc-1"),
    )
    assert both.repo == "app"
    state = LocationState()
    state.on_call("tc-1", "tu1", "edit", {"path": "lib/a.py"}, 1)
    state.on_result("tc-1", "tu1")
    assert state.best(lambda p: locate(workspace, p))[1] is False
    assert not state.from_root


# ---------------------------------------------------------------------------
# Path -> location (D28)
# ---------------------------------------------------------------------------


def test_a_worktree_under_dot_git_gives_its_repo_worktree_and_branch(workspace):
    sha = make_repo(workspace / "app")
    git(workspace / "app", "worktree", "add", "-q", "-b", "plan/079", ".git/build-it-worktrees/x")
    found = where(
        workspace,
        call("shell", command="git -C /workspace/app/.git/build-it-worktrees/x diff"),
    )
    assert found == SessionLocation(
        repo="app", worktree="app/.git/build-it-worktrees/x", branch="plan/079", commit=sha
    )
    deep = locate(workspace, "/workspace/app/.git/build-it-worktrees/x/src/a.py")
    assert deep == found


def test_a_worktree_recorded_with_container_paths(workspace):
    make_repo(workspace / "app")
    admin = workspace / "app" / ".git" / "worktrees" / "x"
    admin.mkdir(parents=True)
    (admin / "gitdir").write_text("/workspace/app/.git/build-it-worktrees/x/.git\n")
    (admin / "commondir").write_text("../..\n")
    (admin / "HEAD").write_text("ref: refs/heads/feat/x\n")
    tree = workspace / "app" / ".git" / "build-it-worktrees" / "x"
    tree.mkdir(parents=True)
    (tree / ".git").write_text("gitdir: /workspace/app/.git/worktrees/x\n")
    found = locate(workspace, "/workspace/app/.git/build-it-worktrees/x")
    assert (found.repo, found.worktree, found.branch, found.missing) == (
        "app",
        "app/.git/build-it-worktrees/x",
        "feat/x",
        False,
    )


def test_a_worktree_beside_its_repo(workspace):
    sha = make_repo(workspace / "app")
    git(workspace / "app", "worktree", "add", "-q", "-b", "feat/y", "../app-y")
    found = locate(workspace, "/workspace/app-y/src")
    assert found == SessionLocation(repo="app", worktree="app-y", branch="feat/y", commit=sha)


def test_a_deleted_worktree_is_missing(workspace):
    sha = make_repo(workspace / "app")
    git(workspace / "app", "worktree", "add", "-q", "-b", "plan/x", ".git/build-it-worktrees/x")
    shutil.rmtree(workspace / "app" / ".git" / "build-it-worktrees" / "x")
    found = where(workspace, call("edit", path="app/.git/build-it-worktrees/x/src/a.py"))
    assert found == SessionLocation(
        repo="app",
        worktree="app/.git/build-it-worktrees/x",
        branch="plan/x",
        commit=sha,
        missing=True,
    )
    # Once git prunes it, the path is just inside the repo's .git.
    git(workspace / "app", "worktree", "prune")
    pruned = locate(workspace, "/workspace/app/.git/build-it-worktrees/x/src/a.py")
    assert pruned == SessionLocation(repo="app", branch="main", commit=sha)


def test_a_detached_head_gives_no_branch_and_the_commit(workspace):
    sha = make_repo(workspace / "app")
    git(workspace / "app", "checkout", "-q", "--detach")
    found = where(workspace, call("edit", path="app/a.py"))
    assert (found.branch, found.commit) == (None, sha)


def test_a_head_symlinked_outside_the_workspace_gives_no_branch(workspace, tmp_path):
    make_repo(workspace / "app")
    secret = tmp_path / "secret-head"
    secret.write_text("ref: refs/heads/secret\n")
    head = workspace / "app" / ".git" / "HEAD"
    head.unlink()
    head.symlink_to(secret)
    found = locate(workspace, "/workspace/app/a.py")
    assert found == SessionLocation(repo="app")


def test_a_git_file_pointing_outside_the_workspace_is_skipped(workspace, tmp_path):
    make_repo(workspace)
    outside = tmp_path / "elsewhere"
    make_repo(outside)
    (workspace / "sub").mkdir()
    (workspace / "sub" / ".git").write_text(f"gitdir: {outside / '.git'}\n")
    # The checkout's own git dir can't be read, so the enclosing repo is used.
    assert locate(workspace, "/workspace/sub/a.py").repo == ""


def test_branch_commits_come_from_packed_refs(workspace):
    sha = make_repo(workspace / "app", branch="feat/p")
    git(workspace / "app", "pack-refs", "--all")
    assert locate(workspace, "/workspace/app") == SessionLocation(
        repo="app", branch="feat/p", commit=sha
    )


def test_refresh_location_rereads_the_head(workspace):
    sha = make_repo(workspace / "app")
    git(workspace / "app", "worktree", "add", "-q", "-b", "wt", ".git/build-it-worktrees/x")
    tree = workspace / "app" / ".git" / "build-it-worktrees" / "x"
    main = SessionLocation(repo="app", branch="main", commit=sha)
    linked = locate(workspace, "/workspace/app/.git/build-it-worktrees/x")
    git(workspace / "app", "checkout", "-q", "-b", "other")
    git(tree, "commit", "-q", "--allow-empty", "-m", "more")
    assert refresh_location(workspace, main).branch == "other"
    assert refresh_location(workspace, linked).commit == git(tree, "rev-parse", "HEAD")
    shutil.rmtree(workspace / "app")
    assert refresh_location(workspace, main) is None
    outside = SessionLocation(repo="/elsewhere/repo", branch="x")
    assert refresh_location(workspace, outside) is outside


# ---------------------------------------------------------------------------
# Bounds on untrusted calls
# ---------------------------------------------------------------------------


def _timed(fn):
    start = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - start


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("cd a;" * 20_000, id="100KB-cd-chain"),
        pytest.param("cd a;" * 100_000, id="500KB-cd-chain"),
        pytest.param('echo "' + "x" * 400_000 + '"', id="400KB-quoted-word"),
        pytest.param('cd "' + "x" * (MAX_COMMAND_CHARS - 10) + '"', id="16KB-quoted-cd"),
        pytest.param("git" + " -C ." * 3_000, id="git-C-chain"),
        pytest.param("<<A" * 350_000, id="1MiB-heredoc-markers"),
        pytest.param("x <<A\n" * 175_000, id="1MiB-heredoc-lines"),
    ],
)
def test_huge_commands_are_parsed_within_a_bound(command):
    (paths, _), elapsed = _timed(lambda: shell_paths(command))
    assert elapsed < 0.1
    assert len(paths) <= MAX_CALL_PATHS
    assert all(len(path) <= MAX_PATH_CHARS for path in paths)


def test_a_call_keeps_its_latest_paths():
    paths, _ = shell_paths("cd a;" * 40)
    assert len(paths) == MAX_CALL_PATHS
    assert paths[-1] == "/workspace/" + "/".join(["a"] * 40)


def test_the_segment_a_truncated_command_ends_in_is_dropped():
    command = "cd app && git status; cd " + "b" * MAX_COMMAND_CHARS
    assert shell_paths(command) == (["/workspace", "/workspace/app"], True)


def test_segments_past_the_cap_are_not_parsed():
    paths, _ = shell_paths("cd .;" * MAX_SEGMENTS + "cd app")
    assert "/workspace/app" not in paths
    assert shell_paths("cd .;" * (MAX_SEGMENTS - 1) + "cd app")[0][-1] == "/workspace/app"


def test_paths_past_the_path_caps_give_no_candidate():
    assert candidate("read", {"path": "a/" * MAX_PATH_COMPONENTS}) is None
    assert candidate("read", {"path": "x" * (MAX_PATH_CHARS + 1)}) is None
    # A cd past the caps ends the walk like an unresolvable target.
    deep = "/".join(["a"] * MAX_PATH_COMPONENTS)
    assert shell_paths(f"cd {deep} && cd /workspace/lib")[0] == ["/workspace"]


def test_locate_rejects_paths_past_the_caps_quickly(workspace):
    make_repo(workspace / "app")
    found, elapsed = _timed(lambda: locate(workspace, "/workspace/app/" + "a/" * 5_000))
    assert found is None
    assert elapsed < 0.1
    assert locate(workspace, "/workspace/app/" + "x" * MAX_PATH_CHARS) is None
    # Under the caps, a deep path still walks up to its repo.
    deep = workspace / "app" / Path(*["d"] * 200)
    deep.mkdir(parents=True)
    assert locate(workspace, "/workspace/app/" + "/".join(["d"] * 200)).repo == "app"
    # A symlink that resolves past the component cap gives None too.
    far = workspace / "far" / Path(*["d"] * MAX_PATH_COMPONENTS)
    far.mkdir(parents=True)
    (workspace / "link").symlink_to(far)
    assert locate(workspace, "/workspace/link") is None


def test_a_session_keeps_its_latest_eligible_calls(workspace):
    make_repo(workspace / "app")
    first = call("edit", path="app/a.py", turn=1)
    others = [call("read", path="notes", turn=2)] * (MAX_ENTRIES - 1)
    assert where(workspace, first, *others).repo == "app"
    # One more call drops the oldest, the only one in a repo: the location is unknown
    # (the session keeps the one it has), not "no repo".
    assert where(workspace, first, *others, call("read", path="notes", turn=2)) is GAVE_UP


def test_a_location_job_resolves_at_most_max_lookups_paths(workspace):
    make_repo(workspace / "app")
    first = call("edit", path="app/a.py", turn=1)

    def best(count: int):
        state = LocationState()
        calls = [first, *(call("read", path=f"n{i}", turn=2) for i in range(count))]
        for n, (name, tool_input, turn, scope) in enumerate(calls):
            state.on_call(scope, f"tu{n}", name, tool_input, turn)
            state.on_result(scope, f"tu{n}")
        resolved = []

        def resolve(path):
            resolved.append(path)
            return locate(workspace, path)

        return state.best(resolve)[0], len(resolved)

    found, lookups = best(MAX_LOOKUPS - 1)
    assert (found.repo, lookups) == ("app", MAX_LOOKUPS)
    assert best(MAX_LOOKUPS) == (GAVE_UP, MAX_LOOKUPS)
    assert where(workspace, call("read", path="notes")) is None  # nothing capped: no repo


def _fed(*calls: tuple) -> LocationState:
    state = LocationState()
    for n, (name, tool_input, turn, scope) in enumerate(calls):
        state.on_call(scope, f"tu{n}", name, tool_input, turn)
        state.on_result(scope, f"tu{n}")
    return state


def test_files_in_one_folder_are_one_lookup(root, workspace):
    make_repo(workspace / "app")
    notes = workspace / "notes"
    notes.mkdir()
    reads = []
    for i in range(MAX_LOOKUPS + 2):
        (notes / f"f{i}.md").write_text("x")
        reads.append(call("read", path=f"notes/f{i}.md", turn=2))
    state = _fed(call("edit", path="app/a.py", turn=1), *reads)
    # Without the directory key the reads use up the lookups; with it they are one.
    assert state.best(lambda p: locate(workspace, p))[0] is GAVE_UP
    found, from_root = tracker_module.locate_session(root, "proj", state)
    assert (found.repo, from_root) == ("app", True)


def test_location_key_maps_only_regular_files_to_their_directory(workspace):
    (workspace / "d").mkdir()
    (workspace / "d" / "f").write_text("x")
    (workspace / "link").symlink_to(workspace / "d" / "f")
    assert location_key(workspace, "/workspace/d/f") == "/workspace/d"
    assert location_key(workspace, "/workspace/d") == "/workspace/d"
    assert location_key(workspace, "/workspace/link") == "/workspace/link"
    assert location_key(workspace, "/workspace/missing") == "/workspace/missing"


def test_a_large_heredoc_keeps_the_cd_and_git_after_it():
    body = "x = 1\n" * (19 * 1024 // 6)
    command = (
        f"cat > /workspace/repo/gen.py <<'EOF'\n{body}EOF\n"
        "cd /workspace/repo && git add gen.py && git commit -m gen"
    )
    paths, runs_git = shell_paths(command)
    assert (paths[-1], runs_git) == ("/workspace/repo", True)
    assert candidate("shell", {"command": command}) == Candidate(
        RANK_GIT, ("/workspace", "/workspace/repo")
    )
    message = "line\n" * (20 * 1024 // 5)
    paths, runs_git = shell_paths(f"cd /workspace/repo && git commit -F - <<'EOF'\n{message}EOF")
    assert (paths, runs_git) == (["/workspace", "/workspace/repo"], True)


def test_many_heredoc_markers_parse_quickly():
    # The marker line is past the parse cut, so only the cd before it counts.
    command = "cd app && cat " + "<<A" * 10_000 + "\n" + "A\n" * 10_000 + "cd lib"
    (paths, _), elapsed = _timed(lambda: shell_paths(command))
    assert elapsed < 0.05
    assert paths == ["/workspace", "/workspace/app"]


def test_a_path_of_git_names_resolves_quickly(workspace):
    path = "/workspace/" + "/".join([".git"] * (MAX_PATH_COMPONENTS - 1))
    found, elapsed = _timed(lambda: locate(workspace, path))
    assert found is None
    assert elapsed < 0.02


# ---------------------------------------------------------------------------
# Log scans
# ---------------------------------------------------------------------------


def _tool_call(tool_use_id: str, name: str, tool_input, *, turn=1, scope=None) -> dict:
    return {
        "type": "tool_call",
        "id": f"c-{tool_use_id}",
        "turn": turn,
        "iteration": 1,
        "scope": scope,
        "request_id": "r",
        "tool_use_id": tool_use_id,
        "name": name,
        "input": tool_input,
    }


def _tool_result(tool_use_id: str, *, turn=1, scope=None) -> dict:
    return {
        "type": "tool_result",
        "id": f"r-{tool_use_id}",
        "turn": turn,
        "iteration": 1,
        "scope": scope,
        "request_id": "r",
        "tool_use_id": tool_use_id,
        "content": "ok",
        "is_error": False,
    }


def _write_log(path: Path, *events) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((e if isinstance(e, str) else json.dumps(e)) + "\n" for e in events))
    return path


def test_scan_log_reads_calls_with_their_results(workspace, tmp_path):
    make_repo(workspace / "app")
    make_repo(workspace / "lib")
    log_path = _write_log(
        tmp_path / "s.jsonl",
        {"type": "user_message", "id": "u", "turn": 1, "scope": None, "content": "tool_call"},
        _tool_call("a", "edit", {"path": "app/a.py"}),
        _tool_result("a"),
        _tool_call("b", "edit", {"path": "lib/b.py"}),  # no result yet: not counted
        "not json tool_call",
        '["tool_call"]',
        _tool_call("c", "edit", {"path": "lib/c.py"}, scope=7),  # malformed scope
        _tool_result("c", scope=7),
    )
    state = scan_log(log_path)
    assert state.best(lambda p: locate(workspace, p))[0].repo == "app"
    with pytest.raises(OSError):
        scan_log(tmp_path / "missing.jsonl")


# ---------------------------------------------------------------------------
# The tracker (D27, D29)
# ---------------------------------------------------------------------------


async def until(predicate, timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def _frame(event: dict) -> str:
    return json.dumps(event)


def _located_tracker(root: Path, **kwargs):
    tracker = make_tracker({SID: PORT}, runtime=kwargs.pop("runtime", FakeRuntime()), **kwargs)
    tracker.workspace_root = lambda: root
    return tracker


async def test_a_root_tool_result_after_an_edit_publishes_the_location(root, workspace):
    sha = make_repo(workspace / "app")
    tracker = _located_tracker(root)
    published = []
    tracker.add_listener(lambda r: published.append(r.location))
    agent = FakeAgent(
        [
            handshake_frame(),
            _frame(_tool_call("t1", "edit", {"path": "app/a.py"})),
            0.1,  # the call alone places nothing
            _frame(_tool_result("t1")),
            5.0,
        ]
    )
    with patch("archie_orchestrator.tracker.websockets.connect", agent.connect()):
        tracker._spawn(SID, tracker._follow(SID))
        await asyncio.sleep(0.05)
        assert tracker.live(SID).location is None
        await until(lambda: tracker.live(SID).location is not None)
        await tracker.close()
    expected = SessionLocation(repo="app", branch="main", commit=sha)
    assert tracker.live(SID).location == expected
    assert [loc for loc in published if loc is not None] == [expected]  # published once
    assert tracker.store.get(SID).location == expected


async def test_a_connect_scans_the_log(root, workspace):
    make_repo(workspace / "lib", branch="feat/scan")
    tracker = _located_tracker(root)
    _write_log(
        tracker.log_path(SID),
        _tool_call("t1", "shell", {"command": "cd lib && git status"}),
        _tool_result("t1"),
    )
    agent = FakeAgent([handshake_frame(), 5.0])
    with patch("archie_orchestrator.tracker.websockets.connect", agent.connect()):
        tracker._spawn(SID, tracker._follow(SID))
        await until(lambda: tracker.live(SID).location is not None)
        await tracker.close()
    assert (tracker.live(SID).location.repo, tracker.live(SID).location.branch) == (
        "lib",
        "feat/scan",
    )


async def test_events_during_a_scan_are_kept(root, workspace, monkeypatch):
    make_repo(workspace / "app")
    make_repo(workspace / "lib")
    tracker = _located_tracker(root)
    _write_log(
        tracker.log_path(SID),
        _tool_call("t1", "edit", {"path": "app/a.py"}, turn=1),
        _tool_result("t1", turn=1),
    )
    real_scan = tracker_module.scan_log
    gate = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_scan(path):
        asyncio.run_coroutine_threadsafe(gate.wait(), loop).result(timeout=3)
        return real_scan(path)

    monkeypatch.setattr(tracker_module, "scan_log", slow_scan)
    tracker.request_location(SID, scan=True)
    await asyncio.sleep(0.05)
    # A later turn's call arrives on the stream while the scan runs.
    await tracker._on_frame(SID, _frame(_tool_call("t2", "edit", {"path": "lib/b.py"}, turn=2)))
    await tracker._on_frame(SID, _frame(_tool_result("t2", turn=2)))
    gate.set()
    await until(lambda: tracker.live(SID).location is not None)
    await until(lambda: not tracker._location_tasks)
    assert tracker.live(SID).location.repo == "lib"
    await tracker.close()


async def test_the_sweep_publishes_a_head_change(root, workspace, monkeypatch):
    sha = make_repo(workspace / "app")
    monkeypatch.setattr(tracker_module, "LOCATION_SWEEP_INTERVAL", 0.02)
    located = record(SID, location=SessionLocation(repo="app", branch="main", commit=sha))
    # Listed without a port, so start() follows nothing; the sweep still runs.
    tracker = make_tracker(records=[located], runtime=FakeRuntime([running(SID, port=None)]))
    tracker.workspace_root = lambda: root
    await tracker.start()
    await asyncio.sleep(0.1)
    assert tracker.live(SID).location.branch == "main"  # unchanged: nothing published
    git(workspace / "app", "checkout", "-q", "-b", "moved")
    await until(lambda: tracker.live(SID).location.branch == "moved")
    await tracker.close()
    assert tracker.store.get(SID).location.branch == "moved"


async def test_a_failing_location_job_is_logged_and_the_stream_still_followed(
    root, workspace, monkeypatch, caplog
):
    make_repo(workspace / "app")

    def boom(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr(tracker_module, "locate_session", boom)
    tracker = _located_tracker(root)
    agent = FakeAgent(
        [
            handshake_frame(),
            _frame(_tool_call("t1", "edit", {"path": "app/a.py"})),
            _frame(_tool_result("t1")),
            0.1,
            status_frame("working"),
            5.0,
        ]
    )
    with (
        caplog.at_level(logging.ERROR, logger="archie_orchestrator.tracker"),
        patch("archie_orchestrator.tracker.websockets.connect", agent.connect()),
    ):
        tracker._spawn(SID, tracker._follow(SID))
        await until(lambda: tracker.live(SID).status == "working")
        await tracker.close()
    assert f"Location job for session {SID} failed" in caplog.text
    assert tracker.live(SID).location is None


async def test_a_workspace_that_no_longer_resolves_clears_the_location(root, workspace):
    make_repo(workspace / "app")
    tracker = _located_tracker(root)
    await tracker._on_frame(SID, _frame(_tool_call("t1", "edit", {"path": "app/a.py"})))
    await tracker._on_frame(SID, _frame(_tool_result("t1")))
    await until(lambda: tracker.live(SID).location is not None)
    shutil.rmtree(workspace)
    await tracker._on_frame(SID, _frame(_tool_call("t2", "read", {"path": "app/b.py"})))
    await tracker._on_frame(SID, _frame(_tool_result("t2")))
    await until(lambda: tracker.live(SID).location is None)
    await tracker.close()


async def test_a_session_placed_earlier_keeps_its_location_when_a_cap_is_hit(root, workspace):
    sha = make_repo(workspace / "app")
    tracker = _located_tracker(root)
    await tracker._on_frame(SID, _frame(_tool_call("t0", "edit", {"path": "app/a.py"})))
    await tracker._on_frame(SID, _frame(_tool_result("t0")))
    await until(lambda: tracker.live(SID).location is not None)
    await until(lambda: not tracker._location_tasks)
    # A later turn reads more files than a job resolves, each in its own non-repo folder.
    for i in range(MAX_LOOKUPS + 2):
        tool_input = {"path": f"n{i}/f.md"}
        await tracker._on_frame(SID, _frame(_tool_call(f"t{i + 1}", "read", tool_input, turn=2)))
        await tracker._on_frame(SID, _frame(_tool_result(f"t{i + 1}", turn=2)))
    await until(lambda: not tracker._location_tasks)
    state = tracker._location_states[SID]
    assert tracker_module.locate_session(root, "proj", state.snapshot())[0] is GAVE_UP
    assert tracker.live(SID).location == SessionLocation(repo="app", branch="main", commit=sha)
    await tracker.close()


async def test_an_unreadable_log_keeps_the_previous_location(root, workspace):
    make_repo(workspace / "app")
    kept = SessionLocation(repo="app", branch="old")
    tracker = make_tracker(records=[record(SID, location=kept)])
    tracker.workspace_root = lambda: root
    tracker.request_location(SID, scan=True)  # there is no log
    await until(lambda: not tracker._location_tasks)
    assert tracker.live(SID).location == kept
    await tracker.close()


async def test_without_a_workspace_root_there_are_no_locations(workspace):
    make_repo(workspace / "app")
    tracker = make_tracker({SID: PORT})
    await tracker._on_frame(SID, _frame(_tool_call("t1", "edit", {"path": "app/a.py"})))
    await tracker._on_frame(SID, _frame(_tool_result("t1")))
    tracker.request_location(SID, scan=True)
    assert not tracker._location_tasks and not tracker._location_states
    await tracker.close()


async def test_an_ended_session_keeps_its_last_location(root, workspace):
    make_repo(workspace / "app")
    tracker = _located_tracker(root)
    await tracker._on_frame(SID, _frame(_tool_call("t1", "edit", {"path": "app/a.py"})))
    await tracker._on_frame(SID, _frame(_tool_result("t1")))
    await until(lambda: tracker.live(SID).location is not None)
    before = tracker.live(SID).location
    await tracker._update(SID, status="exited", ended_at="2026-11-12T10:00:00+00:00")
    shutil.rmtree(workspace / "app")
    tracker.request_location(SID)
    await asyncio.sleep(0.05)
    assert tracker.store.get(SID).location == before
    await tracker.close()


async def test_subagent_results_request_a_location_only_while_the_root_has_none(root, workspace):
    make_repo(workspace / "app")
    make_repo(workspace / "lib")
    tracker = _located_tracker(root)
    scoped = {"scope": "tc-1"}
    await tracker._on_frame(SID, _frame(_tool_call("s1", "edit", {"path": "lib/a.py"}, **scoped)))
    await tracker._on_frame(SID, _frame(_tool_result("s1", **scoped)))
    await until(lambda: tracker.live(SID).location is not None)
    assert tracker.live(SID).location.repo == "lib"
    await tracker._on_frame(SID, _frame(_tool_call("r1", "edit", {"path": "app/a.py"})))
    await tracker._on_frame(SID, _frame(_tool_result("r1")))
    await until(lambda: tracker.live(SID).location.repo == "app")
    await until(lambda: not tracker._location_tasks)
    requests = []
    with patch.object(tracker, "request_location", side_effect=requests.append):
        await tracker._on_frame(SID, _frame(_tool_call("s2", "edit", {"path": "lib/b"}, **scoped)))
        await tracker._on_frame(SID, _frame(_tool_result("s2", **scoped)))
    assert requests == []  # the root placed the session; subagents no longer move it
    await tracker.close()


async def test_the_tracker_places_a_session_after_huge_calls(root, workspace):
    make_repo(workspace / "lib", branch="feat/big")
    tracker = _located_tracker(root)
    flood = _frame(_tool_call("t1", "shell", {"command": "cd a;" * 100_000}))
    big = _frame(
        _tool_call("t2", "shell", {"command": "cd lib && git status\necho " + "x" * 500_000})
    )
    start = time.perf_counter()
    await tracker._on_frame(SID, flood)
    await tracker._on_frame(SID, _frame(_tool_result("t1")))
    await tracker._on_frame(SID, big)
    elapsed = time.perf_counter() - start  # three frames parsed on the event loop
    await tracker._on_frame(SID, _frame(_tool_result("t2")))
    await until(lambda: tracker.live(SID).location is not None)
    assert elapsed < 0.1
    assert (tracker.live(SID).location.repo, tracker.live(SID).location.branch) == (
        "lib",
        "feat/big",
    )
    await tracker.close()

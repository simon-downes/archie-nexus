#!/usr/bin/env python3
"""Create, find, resume, and remove build-it worktrees.

Every subcommand prints one JSON object on stdout. Exit code 0 means success; on
failure the exit code is 1 and the JSON is {"error": "<message>"}.

  start  --repo PATH --slug SLUG [--plans-dir DIR]
      Create branch build-it/<NNN>-<slug> from the repo's HEAD and check it out in a new
      worktree at <git-common-dir>/build-it-worktrees/<NNN>-<slug>. The user's checkout
      and its uncommitted changes are not touched; they are listed in "uncommitted".
      If PATH is not in a git repo, runs `git init` there. If the repo has no commits,
      commits every file not ignored as "chore: initial commit" and lists them in
      "initial_commit", so the worktree starts with the existing files.

  list   --repo PATH
      List build-it branches and the state of the plan on each. A build is "unfinished"
      until its plan is done with a PR outcome and, when there is a PR URL, a CI result.

  resume --repo PATH --branch build-it/<NNN>-<slug>
      Make sure the branch has a worktree (recreating a deleted one) and print the
      same fields as start, read from the plan header.

  remove --repo PATH --worktree PATH [--leftover REL_PATH ...]
      Remove the worktree without --force. Untracked files named with --leftover are
      deleted first; any other uncommitted file makes it refuse. The branch is kept.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BRANCH_PREFIX = "build-it/"
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NUMBER_RE = re.compile(r"^(\d{3})-")
PLAN_RE = re.compile(r"(?:^|/)(\d{3})-build-[a-z0-9-]+\.md$")
HEADER_RE = {
    "status": re.compile(r"^Status:\s*(\S+)", re.M),
    "branch_line": re.compile(r"^Branch:\s*(\S+)\s*\(base:\s*(.+?)\s*@\s*([0-9a-f]+)\)", re.M),
    "pr": re.compile(r"^PR:\s*(.+)$", re.M),
    "ci": re.compile(r"^CI:\s*(.+)$", re.M),
}


class ScriptError(Exception):
    pass


def git(*args: str, cwd: Path, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise ScriptError(f"git {' '.join(args)} failed: {detail}")
    return proc.stdout.rstrip("\n")


def repo_root(path: Path) -> Path:
    if not path.is_dir():
        raise ScriptError(f"not a directory: {path}")
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise ScriptError(f"not a git repository: {path}")
    return Path(proc.stdout.strip())


def has_commits(root: Path) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=root, capture_output=True
        ).returncode
        == 0
    )


def ensure_repo(path: Path) -> tuple[Path, list[str]]:
    """Return the repo root, creating the repo and its first commit if needed.

    The second value lists the files committed by a created first commit, so the
    caller can report them; it is empty when the repo already had commits.
    """
    if not path.is_dir():
        raise ScriptError(f"not a directory: {path}")
    inside = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True
    )
    root = Path(inside.stdout.strip()) if inside.returncode == 0 else path
    if inside.returncode != 0:
        git("init", cwd=root)
    if has_commits(root):
        return root, []
    git("add", "-A", cwd=root)
    committed = git("diff", "--cached", "--name-only", cwd=root).splitlines()
    git("commit", "--allow-empty", "-m", "chore: initial commit", cwd=root)
    return root, committed


def common_dir(root: Path) -> Path:
    return Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=root))


def build_branches(root: Path) -> list[str]:
    out = git("for-each-ref", "--format=%(refname:short)", f"refs/heads/{BRANCH_PREFIX}", cwd=root)
    return [line for line in out.splitlines() if line]


def plan_on_branch(root: Path, branch: str) -> str | None:
    files = git("ls-tree", "-r", "--name-only", branch, cwd=root).splitlines()
    suffix = branch[len(BRANCH_PREFIX) :]
    number, _, slug = suffix.partition("-")
    wanted = f"{number}-build-{slug}.md"
    matches = [f for f in files if f == wanted or f.endswith("/" + wanted)]
    return matches[0] if matches else None


def read_header(text: str) -> dict:
    header: dict = {}
    for key, pattern in HEADER_RE.items():
        match = pattern.search(text)
        if key == "branch_line":
            if match:
                header["base_branch"] = match.group(2)
                header["base_sha"] = match.group(3)
        else:
            header[key] = match.group(1).strip() if match else None
    return header


def worktrees(root: Path) -> dict[str, Path]:
    """Map branch name to worktree path for every registered worktree."""
    out = git("worktree", "list", "--porcelain", cwd=root)
    result: dict[str, Path] = {}
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :])
        elif line.startswith("branch refs/heads/") and path is not None:
            result[line[len("branch refs/heads/") :]] = path
    return result


def pick_plans_dir(root: Path, explicit: str | None) -> str:
    if explicit:
        rel = Path(explicit)
        if rel.is_absolute() or ".." in rel.parts:
            raise ScriptError(f"--plans-dir must be relative to the repo root: {explicit}")
        return rel.as_posix()
    for candidate in ("docs/plans", "plans"):
        if (root / candidate).is_dir():
            return candidate
    return "plans"


def next_number(root: Path, plans_dir: str) -> str:
    seen = [0]
    for directory in (root / plans_dir, root / plans_dir / "done"):
        if directory.is_dir():
            for entry in directory.iterdir():
                match = NUMBER_RE.match(entry.name)
                if match:
                    seen.append(int(match.group(1)))
    for branch in build_branches(root):
        match = NUMBER_RE.match(branch[len(BRANCH_PREFIX) :])
        if match:
            seen.append(int(match.group(1)))
    number = max(seen) + 1
    if number > 999:
        raise ScriptError("plan numbers are exhausted (999)")
    return f"{number:03d}"


def cmd_start(args: argparse.Namespace) -> dict:
    if not SLUG_RE.match(args.slug) or len(args.slug) > 40:
        raise ScriptError("slug must be lowercase words joined by hyphens, at most 40 characters")
    root, initial_commit = ensure_repo(Path(args.repo).resolve())
    plans_dir = pick_plans_dir(root, args.plans_dir)
    number = next_number(root, plans_dir)
    name = f"{number}-{args.slug}"
    branch = BRANCH_PREFIX + name
    worktree = common_dir(root) / "build-it-worktrees" / name
    if worktree.exists():
        raise ScriptError(f"worktree path already exists: {worktree}")
    base_branch = (
        git("symbolic-ref", "--short", "-q", "HEAD", cwd=root, check=False) or "(detached)"
    )
    base_sha = git("rev-parse", "HEAD", cwd=root)
    uncommitted = git("status", "--porcelain", cwd=root).splitlines()
    git("worktree", "add", "-b", branch, str(worktree), base_sha, cwd=root)
    return {
        "repo": str(root),
        "number": number,
        "slug": args.slug,
        "branch": branch,
        "worktree": str(worktree),
        "base_branch": base_branch,
        "base_sha": base_sha,
        "base_short_sha": base_sha[:7],
        "plans_dir": plans_dir,
        "plan_path": f"{plans_dir}/{number}-build-{args.slug}.md",
        "uncommitted": uncommitted,
        "initial_commit": initial_commit,
    }


def cmd_list(args: argparse.Namespace) -> dict:
    root = repo_root(Path(args.repo).resolve())
    registered = worktrees(root)
    builds = []
    for branch in build_branches(root):
        plan = plan_on_branch(root, branch)
        entry: dict = {"branch": branch, "plan_path": plan}
        if plan:
            entry.update(read_header(git("show", f"{branch}:{plan}", cwd=root)))
            pr, ci = entry.get("pr"), entry.get("ci")
            awaiting_ci = bool(pr and pr.startswith("http") and ci in (None, "", "pending"))
            entry["unfinished"] = entry.get("status") != "done" or pr == "pending" or awaiting_ci
        else:
            entry["unfinished"] = True
        path = registered.get(branch)
        entry["worktree"] = str(path) if path and path.exists() else None
        builds.append(entry)
    return {"repo": str(root), "builds": builds}


def cmd_resume(args: argparse.Namespace) -> dict:
    root = repo_root(Path(args.repo).resolve())
    branch = args.branch
    if not branch.startswith(BRANCH_PREFIX) or branch not in build_branches(root):
        raise ScriptError(f"no such build-it branch: {branch}")
    path = worktrees(root).get(branch)
    recreated = False
    if path is None or not path.exists():
        git("worktree", "prune", cwd=root)
        path = common_dir(root) / "build-it-worktrees" / branch[len(BRANCH_PREFIX) :]
        if path.exists():
            raise ScriptError(f"worktree path exists but is not registered: {path}")
        git("worktree", "add", str(path), branch, cwd=root)
        recreated = True
    plan = plan_on_branch(root, branch)
    header = read_header((path / plan).read_text(encoding="utf-8")) if plan else {}
    suffix = branch[len(BRANCH_PREFIX) :]
    number, _, slug = suffix.partition("-")
    return {
        "repo": str(root),
        "number": number,
        "slug": slug,
        "branch": branch,
        "worktree": str(path),
        "recreated": recreated,
        "plan_path": plan,
        **header,
    }


def cmd_remove(args: argparse.Namespace) -> dict:
    root = repo_root(Path(args.repo).resolve())
    worktree = Path(args.worktree).resolve()
    if worktree not in {p.resolve() for p in worktrees(root).values()}:
        raise ScriptError(f"not a registered worktree of {root}: {worktree}")
    status = git("status", "--porcelain", "--untracked-files=all", cwd=worktree).splitlines()
    leftovers = {Path(p).as_posix() for p in args.leftover}
    entries = [(line[:2], line[3:]) for line in status]
    blocking = [f"{code} {rel}" for code, rel in entries if not (code == "??" and rel in leftovers)]
    if blocking:
        raise ScriptError("worktree has uncommitted files; kept it: " + "; ".join(blocking))
    deleted = []
    for _, rel in entries:
        (worktree / rel).unlink()
        deleted.append(rel)
    git("worktree", "remove", str(worktree), cwd=root)
    return {"removed": str(worktree), "deleted_leftovers": deleted}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--repo", required=True)
    start.add_argument("--slug", required=True)
    start.add_argument("--plans-dir")
    listing = sub.add_parser("list")
    listing.add_argument("--repo", required=True)
    resume = sub.add_parser("resume")
    resume.add_argument("--repo", required=True)
    resume.add_argument("--branch", required=True)
    remove = sub.add_parser("remove")
    remove.add_argument("--repo", required=True)
    remove.add_argument("--worktree", required=True)
    remove.add_argument("--leftover", action="append", default=[])
    args = parser.parse_args()
    handlers = {"start": cmd_start, "list": cmd_list, "resume": cmd_resume, "remove": cmd_remove}
    try:
        result = handlers[args.command](args)
    except ScriptError as error:
        print(json.dumps({"error": str(error)}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")
    sys.exit(main())

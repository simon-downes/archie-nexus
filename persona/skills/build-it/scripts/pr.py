#!/usr/bin/env python3
"""Push a build-it branch and open or update its draft pull request, or wait for CI.

Every subcommand prints one JSON object on stdout. Exit code 0 means success; on
failure the exit code is 1 and the JSON is {"error": "<message>", ...} with whatever
was already done (for example "pushed": true).

  open --worktree PATH --title TITLE --body-file FILE [--attach FILE[#alt] ...]
      Run from a build-it worktree. Picks the remote (the base branch's upstream
      remote, then origin, then the only remote), pushes the current build-it branch
      (never force), and opens a draft PR against the base branch recorded in the
      plan header, or the remote's default branch when the base is detached or not on
      the remote. If the branch already has an open PR, updates its title and body
      and leaves its draft state alone. Prints the URL and a check of what GitHub
      recorded. Outcomes: "created", "updated", "no remote", "no change".

  push --worktree PATH
      Push the current build-it branch again (for commits made after "open").

  ci --worktree PATH --url URL [--timeout SECONDS]
      Wait for the PR's checks (default 1200 seconds). Prints "result": one of
      "passed", "none", "timeout", "failed" and, when failed, the failing checks with
      their links.

Standard library only. Needs git and an authenticated gh.
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
BASE_RE = re.compile(r"^Branch:\s*(\S+)\s*\(base:\s*(.+?)\s*@\s*([0-9a-f]+)\)", re.M)
REMOTE_URL_RE = re.compile(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$")


class ScriptError(Exception):
    def __init__(self, message: str, **done):
        super().__init__(message)
        self.done = done


def run(cmd: list[str], cwd: Path, check: bool = True, timeout: float | None = None):
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise ScriptError(f"{cmd[0]} is not installed") from None
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise ScriptError(f"{' '.join(cmd[:3])} failed: {detail}")
    return proc


def git(*args: str, cwd: Path, check: bool = True) -> str:
    return run(["git", *args], cwd, check).stdout.strip()


def current_branch(wt: Path) -> str:
    branch = git("symbolic-ref", "--short", "-q", "HEAD", cwd=wt, check=False)
    if not branch.startswith(BRANCH_PREFIX):
        raise ScriptError(f"not on a build-it branch in {wt}: {branch or '(detached)'}")
    return branch


def plan_base(wt: Path, branch: str) -> tuple[str, str]:
    number, _, slug = branch[len(BRANCH_PREFIX) :].partition("-")
    wanted = f"{number}-build-{slug}.md"
    files = git("ls-files", cwd=wt).splitlines()
    plans = [f for f in files if f == wanted or f.endswith("/" + wanted)]
    if not plans:
        raise ScriptError(f"plan {wanted} is not committed on {branch}")
    match = BASE_RE.search((wt / plans[0]).read_text(encoding="utf-8"))
    if not match:
        raise ScriptError(
            f"plan header has no 'Branch: ... (base: <branch> @ <sha>)' line: {plans[0]}"
        )
    return match.group(2), git("rev-parse", match.group(3), cwd=wt)


def pick_remote(wt: Path, base_branch: str) -> str | None:
    remotes = git("remote", cwd=wt).splitlines()
    if not remotes:
        return None
    if base_branch != "(detached)":
        upstream = git("config", "--get", f"branch.{base_branch}.remote", cwd=wt, check=False)
        if upstream in remotes:
            return upstream
    if "origin" in remotes:
        return "origin"
    if len(remotes) == 1:
        return remotes[0]
    raise ScriptError("ambiguous remote: several remotes and none is origin or the base's upstream")


def github_repo(wt: Path, remote: str) -> str:
    url = git("remote", "get-url", remote, cwd=wt)
    match = REMOTE_URL_RE.search(url)
    if not match:
        raise ScriptError(f"remote {remote} is not a GitHub repository: {url}")
    return f"{match.group(1)}/{match.group(2)}"


def gh_json(args: list[str], wt: Path):
    out = run(["gh", *args], wt).stdout
    return json.loads(out) if out.strip() else None


def push(wt: Path, remote: str, branch: str) -> None:
    run(["git", "push", "-u", remote, f"{branch}:{branch}"], wt)


def resolve(wt: Path) -> dict:
    branch = current_branch(wt)
    base_branch, base_sha = plan_base(wt, branch)
    number, _, slug = branch[len(BRANCH_PREFIX) :].partition("-")
    return {
        "branch": branch,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "plan_name": f"{number}-build-{slug}.md",
        "remote": pick_remote(wt, base_branch),
    }


def cmd_open(args: argparse.Namespace) -> dict:
    wt = Path(args.worktree).resolve()
    body = Path(args.body_file)
    if not body.is_file():
        raise ScriptError(f"body file not found: {body}")
    if not args.title.strip() or len(args.title) > 100:
        raise ScriptError("title must be 1-100 characters")
    for item in args.attach:
        if not Path(item.split("#", 1)[0]).is_file():
            raise ScriptError(f"attachment not found: {item}")
    if git("status", "--porcelain", "--untracked-files=no", cwd=wt):
        raise ScriptError("worktree has uncommitted changes to tracked files; commit them first")
    info = resolve(wt)
    if info["remote"] is None:
        return {**info, "outcome": "no remote"}
    changed = git("diff", "--name-only", f"{info['base_sha']}...HEAD", cwd=wt).splitlines()
    if all(Path(f).name == info["plan_name"] for f in changed):
        return {**info, "outcome": "no change"}

    remote = info["remote"]
    repo = github_repo(wt, remote)
    info["repo"] = repo
    run(["gh", "auth", "status"], wt)

    default = gh_json(["repo", "view", repo, "--json", "defaultBranchRef"], wt)["defaultBranchRef"][
        "name"
    ]
    base = info["base_branch"]
    warnings = []
    on_remote = (
        base != "(detached)"
        and run(
            ["git", "ls-remote", "--exit-code", "--heads", remote, base], wt, check=False
        ).returncode
        == 0
    )
    if not on_remote:
        base = default
        git("fetch", "--quiet", remote, default, cwd=wt)
        if (
            run(
                ["git", "merge-base", "--is-ancestor", info["base_sha"], "FETCH_HEAD"],
                wt,
                check=False,
            ).returncode
            != 0
        ):
            warnings.append(
                f"base sha is not on {remote}/{default}: the PR includes commits "
                "that build-it did not review"
            )
    info["pr_base"] = base

    try:
        push(wt, remote, info["branch"])
    except ScriptError as error:
        raise ScriptError(str(error), **info, pushed=False) from error
    info["pushed"] = True

    try:
        existing = (
            gh_json(
                [
                    "pr",
                    "list",
                    "--repo",
                    repo,
                    "--head",
                    info["branch"],
                    "--state",
                    "open",
                    "--json",
                    "url",
                ],
                wt,
            )
            or []
        )
        if existing:
            url = existing[0]["url"]
            run(
                [
                    "gh",
                    "pr",
                    "edit",
                    url,
                    "--repo",
                    repo,
                    "--title",
                    args.title,
                    "--body-file",
                    str(body),
                ],
                wt,
            )
            for item in args.attach:
                run(
                    [
                        "gh",
                        "pr",
                        "comment",
                        url,
                        "--repo",
                        repo,
                        "--body",
                        "Evidence",
                        "--attach",
                        item,
                    ],
                    wt,
                )
            outcome = "updated"
        else:
            cmd = [
                "gh",
                "pr",
                "create",
                "--draft",
                "--repo",
                repo,
                "--base",
                base,
                "--head",
                info["branch"],
                "--title",
                args.title,
                "--body-file",
                str(body),
            ]
            for item in args.attach:
                cmd += ["--attach", item]
            url = run(cmd, wt).stdout.strip().splitlines()[-1]
            outcome = "created"

        pr = gh_json(
            [
                "pr",
                "view",
                url,
                "--repo",
                repo,
                "--json",
                "headRefName,baseRefName,isDraft,title,files",
            ],
            wt,
        )
    except ScriptError as error:
        raise ScriptError(str(error), **info) from error

    pr_files = {f["path"] for f in pr.get("files", [])}
    mismatches = []
    if pr["headRefName"] != info["branch"]:
        mismatches.append(f"head is {pr['headRefName']}")
    if pr["baseRefName"] != base:
        mismatches.append(f"base is {pr['baseRefName']}")
    if pr["title"] != args.title:
        mismatches.append("title differs")
    if outcome == "created" and not pr["isDraft"]:
        mismatches.append("not a draft")
    if pr_files != set(changed):
        mismatches.append(
            "file list differs from git diff "
            f"(+{sorted(pr_files - set(changed))} -{sorted(set(changed) - pr_files)})"
        )
    return {
        **info,
        "outcome": outcome,
        "url": url,
        "draft": pr["isDraft"],
        "warnings": warnings,
        "mismatches": mismatches,
    }


def cmd_push(args: argparse.Namespace) -> dict:
    wt = Path(args.worktree).resolve()
    info = resolve(wt)
    if info["remote"] is None:
        raise ScriptError("no remote")
    push(wt, info["remote"], info["branch"])
    return {**info, "pushed": True}


def cmd_ci(args: argparse.Namespace) -> dict:
    wt = Path(args.worktree).resolve()
    info = resolve(wt)
    if info["remote"] is None:
        raise ScriptError("no remote")
    repo = github_repo(wt, info["remote"])
    try:
        proc = run(
            ["gh", "pr", "checks", args.url, "--repo", repo, "--watch", "--fail-fast"],
            wt,
            check=False,
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        return {"url": args.url, "result": "timeout", "waited_seconds": args.timeout}
    text = (proc.stdout + proc.stderr).lower()
    if "no checks reported" in text:
        return {"url": args.url, "result": "none"}
    checks = (
        gh_json(["pr", "checks", args.url, "--repo", repo, "--json", "name,state,bucket,link"], wt)
        or []
    )
    failed = [c for c in checks if c.get("bucket") in ("fail", "cancel")]
    if failed:
        return {"url": args.url, "result": "failed", "failed": failed}
    if proc.returncode != 0:
        raise ScriptError(f"gh pr checks failed: {(proc.stderr or proc.stdout).strip()}")
    return {"url": args.url, "result": "passed", "checks": len(checks)}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    opener = sub.add_parser("open")
    opener.add_argument("--worktree", required=True)
    opener.add_argument("--title", required=True)
    opener.add_argument("--body-file", required=True)
    opener.add_argument("--attach", action="append", default=[])
    pusher = sub.add_parser("push")
    pusher.add_argument("--worktree", required=True)
    ci = sub.add_parser("ci")
    ci.add_argument("--worktree", required=True)
    ci.add_argument("--url", required=True)
    ci.add_argument("--timeout", type=float, default=1200)
    args = parser.parse_args()
    handlers = {"open": cmd_open, "push": cmd_push, "ci": cmd_ci}
    try:
        result = handlers[args.command](args)
    except ScriptError as error:
        print(json.dumps({"error": str(error), **error.done}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")
    os.environ.setdefault("GH_PROMPT_DISABLED", "1")
    sys.exit(main())

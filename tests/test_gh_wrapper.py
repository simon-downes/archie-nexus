"""Tests for the container gh wrapper (container/bin/gh).

The wrapper is run as a subprocess with a fake ``gh-base`` first on PATH. The fake
prints the token environment, its argv, and its stdin as JSON, then exits with the
code given in ``FAKE_EXIT``.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "container" / "bin" / "gh"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "git", "yq")),
    reason="gh wrapper tests need bash, git and yq",
)

FAKE_GH_BASE = f"""#!{sys.executable}
import json, os, sys
print(json.dumps({{
    "GH_TOKEN": os.environ.get("GH_TOKEN"),
    "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN"),
    "argv": sys.argv[1:],
    "stdin": "" if sys.stdin.isatty() else sys.stdin.read(),
}}))
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""

CREDENTIALS = "github:\n  personal_token: personal-secret\n  work_token: work-secret\n"
CONFIG = "plugins:\n  github:\n    settings:\n      work_orgs: [tillo]\n"


@pytest.fixture
def env(tmp_path):
    """A clean environment with a fake gh-base and a Nexus home holding both tokens."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gh-base"
    fake.write_text(FAKE_GH_BASE)
    fake.chmod(0o755)
    home = tmp_path / "nexus"
    home.mkdir()
    (home / "credentials.yaml").write_text(CREDENTIALS)
    (home / "config.yaml").write_text(CONFIG)
    base = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path / "userhome"),
        "ARCHIE_HOME_DIR": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return base


def make_repo(path: Path, **remotes: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for name, url in remotes.items():
        subprocess.run(["git", "-C", str(path), "remote", "add", name, url], check=True)
    return path


def run_gh(env, *args, cwd, stdin=None, extra=None):
    full_env = {**env, **(extra or {})}
    proc = subprocess.run(
        ["bash", str(WRAPPER), *args],
        cwd=cwd,
        env=full_env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
    )
    data = json.loads(proc.stdout) if proc.stdout.strip() else None
    return proc, data


# --- M1: token selection from the origin remote ---


def test_work_org_origin_uses_work_token(env, tmp_path):
    repo = make_repo(tmp_path / "r", origin="git@github.com:tillo/service.git")
    _, data = run_gh(env, "pr", "list", cwd=repo)
    assert data["GH_TOKEN"] == "work-secret"


def test_other_origin_uses_personal_token(env, tmp_path):
    repo = make_repo(tmp_path / "r", origin="git@github.com:simon-downes/archie-nexus.git")
    _, data = run_gh(env, "pr", "list", cwd=repo)
    assert data["GH_TOKEN"] == "personal-secret"


def test_https_origin_is_parsed(env, tmp_path):
    repo = make_repo(tmp_path / "r", origin="https://github.com/tillo/service")
    _, data = run_gh(env, "pr", "list", cwd=repo)
    assert data["GH_TOKEN"] == "work-secret"


def test_outside_a_repo_uses_personal_token(env, tmp_path):
    outside = tmp_path / "plain"
    outside.mkdir()
    _, data = run_gh(env, "auth", "status", cwd=outside)
    assert data["GH_TOKEN"] == "personal-secret"


def test_arguments_stdin_and_exit_code_pass_through(env, tmp_path):
    repo = make_repo(tmp_path / "r", origin="git@github.com:tillo/service.git")
    args = ["pr", "create", "--title", "a b  c", "--body", "", "--label", "x;$(id)"]
    proc, data = run_gh(env, *args, cwd=repo, stdin="hello\n", extra={"FAKE_EXIT": "3"})
    assert data["argv"] == args
    assert data["stdin"] == "hello\n"
    assert proc.returncode == 3


def test_credentials_are_read_on_every_call(env, tmp_path):
    repo = make_repo(tmp_path / "r", origin="git@github.com:simon-downes/x.git")
    _, first = run_gh(env, "pr", "list", cwd=repo)
    creds = Path(env["ARCHIE_HOME_DIR"]) / "credentials.yaml"
    creds.write_text(CREDENTIALS.replace("personal-secret", "rotated"))
    _, second = run_gh(env, "pr", "list", cwd=repo)
    assert (first["GH_TOKEN"], second["GH_TOKEN"]) == ("personal-secret", "rotated")


def test_default_home_is_dot_nexus(env, tmp_path):
    userhome = Path(env["HOME"]) / ".nexus"
    userhome.mkdir(parents=True)
    (userhome / "credentials.yaml").write_text(CREDENTIALS.replace("personal-secret", "from-home"))
    env = {k: v for k, v in env.items() if k != "ARCHIE_HOME_DIR"}
    repo = make_repo(tmp_path / "r", origin="git@github.com:simon-downes/x.git")
    _, data = run_gh(env, "pr", "list", cwd=repo)
    assert data["GH_TOKEN"] == "from-home"


def test_missing_config_means_no_work_orgs(env, tmp_path):
    (Path(env["ARCHIE_HOME_DIR"]) / "config.yaml").unlink()
    repo = make_repo(tmp_path / "r", origin="git@github.com:tillo/service.git")
    _, data = run_gh(env, "pr", "list", cwd=repo)
    assert data["GH_TOKEN"] == "personal-secret"


def test_missing_token_warns_and_runs_without_token(env, tmp_path):
    creds = Path(env["ARCHIE_HOME_DIR"]) / "credentials.yaml"
    creds.write_text("github:\n  personal_token: personal-secret\n")
    repo = make_repo(tmp_path / "r", origin="git@github.com:tillo/service.git")
    proc, data = run_gh(env, "pr", "list", cwd=repo, extra={"GH_TOKEN": ""})
    assert data["GH_TOKEN"] is None
    assert f"gh: github.work_token is not set in {creds}" in proc.stderr


def test_malformed_credentials_warn_and_run_without_token(env, tmp_path):
    creds = Path(env["ARCHIE_HOME_DIR"]) / "credentials.yaml"
    creds.write_text("github: [unclosed\n")
    repo = make_repo(tmp_path / "r", origin="git@github.com:simon-downes/x.git")
    proc, data = run_gh(env, "pr", "list", cwd=repo)
    assert data["GH_TOKEN"] is None
    assert f"gh: could not read {creds}" in proc.stderr


def test_caller_gh_token_is_left_alone(env, tmp_path):
    repo = make_repo(tmp_path / "r", origin="git@github.com:tillo/service.git")
    _, data = run_gh(env, "pr", "list", cwd=repo, extra={"GH_TOKEN": "caller"})
    assert data["GH_TOKEN"] == "caller"


def test_caller_github_token_is_left_alone(env, tmp_path):
    repo = make_repo(tmp_path / "r", origin="git@github.com:tillo/service.git")
    _, data = run_gh(env, "pr", "list", cwd=repo, extra={"GITHUB_TOKEN": "caller"})
    assert data["GITHUB_TOKEN"] == "caller"
    assert data["GH_TOKEN"] is None


def test_token_never_appears_in_argv(env, tmp_path):
    repo = make_repo(tmp_path / "r", origin="git@github.com:tillo/service.git")
    _, data = run_gh(env, "pr", "list", cwd=repo)
    assert "work-secret" not in json.dumps(data["argv"])


# --- M2: explicit repo targets and gh's remote order ---


@pytest.fixture
def personal_repo(tmp_path):
    return make_repo(tmp_path / "p", origin="git@github.com:simon-downes/archie-nexus.git")


@pytest.mark.parametrize(
    "args",
    [
        ["issue", "list", "-R", "tillo/service"],
        ["issue", "list", "-Rtillo/service"],
        ["issue", "list", "--repo", "tillo/service"],
        ["issue", "list", "--repo=tillo/service"],
        ["issue", "list", "-R", "github.com/tillo/service"],
        ["issue", "list", "-R", "https://github.com/tillo/service"],
        ["issue", "list", "-R", "TILLO/service"],
        ["api", "repos/tillo/service/pulls"],
        ["api", "/repos/tillo/service/pulls"],
        ["api", "-X", "GET", "-H", "Accept: x", "orgs/tillo/members"],
        ["api", "users/tillo"],
        ["repo", "view", "tillo/service"],
        ["repo", "view", "--json", "name", "tillo/service"],
        ["repo", "clone", "https://github.com/tillo/service"],
    ],
)
def test_explicit_target_overrides_remote(env, personal_repo, args):
    _, data = run_gh(env, *args, cwd=personal_repo)
    assert data["GH_TOKEN"] == "work-secret"
    assert data["argv"] == args


def test_gh_repo_env_overrides_remote(env, personal_repo):
    _, data = run_gh(env, "pr", "list", cwd=personal_repo, extra={"GH_REPO": "tillo/service"})
    assert data["GH_TOKEN"] == "work-secret"


def test_repo_flag_beats_gh_repo_env(env, personal_repo):
    _, data = run_gh(
        env,
        "pr",
        "list",
        "-R",
        "simon-downes/x",
        cwd=personal_repo,
        extra={"GH_REPO": "tillo/service"},
    )
    assert data["GH_TOKEN"] == "personal-secret"


def test_flags_after_double_dash_are_ignored(env, personal_repo):
    _, data = run_gh(
        env, "pr", "comment", "--body-file", "-", "--", "-R", "tillo/x", cwd=personal_repo
    )
    assert data["GH_TOKEN"] == "personal-secret"


def test_trailing_repo_flag_without_value_falls_back_to_remote(env, personal_repo):
    _, data = run_gh(env, "issue", "list", "-R", cwd=personal_repo)
    assert data["GH_TOKEN"] == "personal-secret"


def test_api_placeholder_falls_back_to_remote(env, tmp_path):
    repo = make_repo(tmp_path / "w", origin="git@github.com:tillo/service.git")
    _, data = run_gh(env, "api", "repos/{owner}/{repo}/pulls", cwd=repo)
    assert data["GH_TOKEN"] == "work-secret"


def test_owner_match_is_exact(env, tmp_path):
    repo = make_repo(tmp_path / "r", origin="git@github.com:tillo-labs/service.git")
    _, data = run_gh(env, "pr", "list", cwd=repo)
    assert data["GH_TOKEN"] == "personal-secret"


def test_upstream_remote_beats_origin(env, tmp_path):
    repo = make_repo(
        tmp_path / "r",
        origin="git@github.com:simon-downes/service.git",
        upstream="git@github.com:tillo/service.git",
    )
    _, data = run_gh(env, "pr", "list", cwd=repo)
    assert data["GH_TOKEN"] == "work-secret"


def test_gh_resolved_base_remote_beats_upstream(env, tmp_path):
    repo = make_repo(
        tmp_path / "r",
        origin="git@github.com:tillo/service.git",
        upstream="git@github.com:tillo/other.git",
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "remote.origin.gh-resolved", "base"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "set-url",
            "upstream",
            "git@github.com:simon-downes/o.git",
        ],
        check=True,
    )
    _, data = run_gh(env, "pr", "list", cwd=repo)
    assert data["GH_TOKEN"] == "work-secret"


def test_github_remote_beats_origin(env, tmp_path):
    repo = make_repo(
        tmp_path / "r",
        origin="git@github.com:simon-downes/service.git",
        github="ssh://git@github.com/tillo/service.git",
    )
    _, data = run_gh(env, "pr", "list", cwd=repo)
    assert data["GH_TOKEN"] == "work-secret"


def test_first_other_remote_when_no_known_names(env, tmp_path):
    repo = make_repo(tmp_path / "r", work="git@github.com:tillo/service.git")
    _, data = run_gh(env, "pr", "list", cwd=repo)
    assert data["GH_TOKEN"] == "work-secret"


# --- Review fixes ---


@pytest.fixture
def work_repo(tmp_path):
    return make_repo(tmp_path / "w", origin="git@github.com:tillo/service.git")


@pytest.mark.parametrize(
    "args",
    [
        ["repo", "view", "-b", "feature/foo"],
        ["repo", "sync", "--branch", "release/1.0"],
        ["repo", "deploy-key", "add", "/home/archie/.ssh/id.pub"],
    ],
)
def test_repo_flag_values_do_not_override_remote(env, work_repo, args):
    _, data = run_gh(env, *args, cwd=work_repo)
    assert data["GH_TOKEN"] == "work-secret"


def test_repo_create_uses_target_not_template(env, personal_repo):
    args = ["repo", "create", "--template", "simon-downes/t", "tillo/new"]
    _, data = run_gh(env, *args, cwd=personal_repo)
    assert data["GH_TOKEN"] == "work-secret"


@pytest.mark.parametrize(
    "args",
    [
        ["repo", "list", "tillo"],
        ["repo", "list", "--limit", "5", "tillo"],
        ["search", "repos", "--owner", "tillo"],
        ["search", "repos", "--owner=tillo"],
        ["api", "https://api.github.com/repos/tillo/service"],
    ],
)
def test_owner_arguments_select_work_token(env, personal_repo, args):
    _, data = run_gh(env, *args, cwd=personal_repo)
    assert data["GH_TOKEN"] == "work-secret"


def test_repo_list_without_owner_uses_remote(env, work_repo):
    _, data = run_gh(env, "repo", "list", cwd=work_repo)
    assert data["GH_TOKEN"] == "work-secret"


def test_non_github_upstream_is_ignored(env, tmp_path):
    repo = make_repo(
        tmp_path / "r",
        origin="git@github.com:tillo/service.git",
        upstream="git@gitlab.com:someone/mirror.git",
    )
    _, data = run_gh(env, "pr", "list", cwd=repo)
    assert data["GH_TOKEN"] == "work-secret"


def test_local_path_upstream_is_ignored(env, tmp_path):
    repo = make_repo(
        tmp_path / "r",
        origin="https://github.com/tillo/service.git",
        upstream=str(tmp_path / "elsewhere"),
    )
    _, data = run_gh(env, "pr", "list", cwd=repo)
    assert data["GH_TOKEN"] == "work-secret"


def test_gh_resolved_with_other_value_is_used(env, tmp_path):
    repo = make_repo(
        tmp_path / "r",
        origin="git@github.com:simon-downes/service.git",
        upstream="git@github.com:simon-downes/other.git",
    )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "fork", "git@github.com:tillo/service.git"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "remote.fork.gh-resolved", "tillo/service"], check=True
    )
    _, data = run_gh(env, "pr", "list", cwd=repo)
    assert data["GH_TOKEN"] == "work-secret"


def test_malformed_config_warns_and_uses_personal(env, work_repo):
    config = Path(env["ARCHIE_HOME_DIR"]) / "config.yaml"
    config.write_text("plugins: [unclosed\n")
    proc, data = run_gh(env, "pr", "list", cwd=work_repo)
    assert data["GH_TOKEN"] == "personal-secret"
    assert f"gh: could not read {config}" in proc.stderr


def test_non_string_token_is_treated_as_missing(env, personal_repo):
    creds = Path(env["ARCHIE_HOME_DIR"]) / "credentials.yaml"
    creds.write_text("github:\n  personal_token: [a, b]\n  work_token: w\n")
    proc, data = run_gh(env, "pr", "list", cwd=personal_repo)
    assert data["GH_TOKEN"] is None
    assert "github.personal_token is not set" in proc.stderr


def test_runs_when_home_and_archie_home_are_unset(env, tmp_path):
    bare = {k: v for k, v in env.items() if k not in {"HOME", "ARCHIE_HOME_DIR"}}
    outside = tmp_path / "plain"
    outside.mkdir()
    proc, data = run_gh(bare, "--version", cwd=outside)
    assert proc.returncode == 0
    assert data["argv"] == ["--version"]

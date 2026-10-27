"""Shell and exec subprocesses get credential/settings variables from the current files."""

import os

import pytest
from archie_agent.exec.tool import run_exec
from archie_agent.exec.tools import _runtime_env
from archie_agent.exec.tools._subprocess import run_shell
from archie_shared.config import home_dir
from archie_shared.credentials.store import replace_credential

SCALR = "plugins:\n  scalr:\n    settings: {hostname: %s, account: acct}\n"


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(_runtime_env, "_cache", None)
    monkeypatch.setattr(_runtime_env, "_last_good", None)
    monkeypatch.delenv("SCALR_TOKEN", raising=False)
    monkeypatch.delenv("SCALR_HOSTNAME", raising=False)


def _write_config(text: str) -> None:
    path = home_dir() / "config.yaml"
    path.write_text(text)
    st = path.stat()  # bump mtime so back-to-back writes are never seen as unchanged
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


async def test_shell_sees_login_and_settings_changes_without_restart():
    _write_config(SCALR % "one.example")
    replace_credential("scalr", {"token": "first"})
    result = await run_shell('printf "%s %s" "$SCALR_TOKEN" "$SCALR_HOSTNAME"')
    assert result.stdout == "first one.example"

    _write_config(SCALR % "two.example")
    replace_credential("scalr", {"token": "second"})
    result = await run_shell('printf "%s %s" "$SCALR_TOKEN" "$SCALR_HOSTNAME"')
    assert result.stdout == "second two.example"


async def test_runtime_env_is_cached_until_files_change(monkeypatch):
    _write_config(SCALR % "one.example")
    calls = []
    real = _runtime_env._compute

    def counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(_runtime_env, "_compute", counting)
    _runtime_env.runtime_env()
    _runtime_env.runtime_env()
    assert len(calls) == 1
    _write_config(SCALR % "two.example")
    assert _runtime_env.runtime_env()[0]["SCALR_HOSTNAME"] == "two.example"
    assert len(calls) == 2


async def test_broken_config_runs_command_with_warning():
    _write_config("plugins:\n  nosuchplugin: {}\n")
    result = await run_shell("echo ran")
    assert result.returncode == 0
    assert result.stdout.strip() == "ran"
    assert result.stderr.startswith("[archie warning: credentials and plugin settings")
    assert "nosuchplugin" in result.stderr


async def test_broken_config_keeps_last_good_values():
    _write_config(SCALR % "one.example")
    replace_credential("scalr", {"token": "kept"})
    await run_shell("true")
    _write_config("plugins: [unclosed\n")
    result = await run_shell('printf "%s" "$SCALR_TOKEN"')
    assert result.stdout == "kept"
    assert result.stderr == ""


async def test_store_error_warning_does_not_quote_the_store():
    (home_dir() / "credentials.yaml").write_text("scalr: {token: s3cret\n")
    (home_dir() / "credentials.yaml").chmod(0o600)
    result = await run_shell("true")
    assert "[archie warning:" in result.stderr
    assert "s3cret" not in result.stderr


async def test_exec_runner_sees_current_credentials(tmp_path):
    _write_config(SCALR % "one.example")
    replace_credential("scalr", {"token": "exec-token"})
    source = "import os\nasync def main():\n    return os.environ.get('SCALR_TOKEN')\n"
    envelope = await run_exec(source, run_root=tmp_path, python=_python())
    assert envelope.ok, envelope.error
    assert envelope.return_value == "exec-token"


def _python() -> str:
    import sys

    return sys.executable

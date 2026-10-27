"""`archie auth login` prompts for missing credential fields and required settings."""

from unittest.mock import MagicMock, patch

import pytest
import yaml
from archie_cli.auth import _missing_settings, _prompt_settings, _set_static_from_login, login
from archie_cli.config_edit import ConfigEditError, set_plugin_settings
from archie_shared.config import home_dir
from archie_shared.schemas import load_nexus_config
from click.testing import CliRunner

COMMENTED = """\
# top comment
agent:
  turn_cost_limit: 20.0   # inline comment
plugins:
  scalr:
    # keep me
    settings:
      hostname: scalr.example
"""


def _response():
    response = MagicMock()
    response.raise_for_status.return_value = None
    return response


def _tty(monkeypatch):
    monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: True))


def test_mapped_env_missing_prompts_hidden_on_terminal(monkeypatch):
    monkeypatch.delenv("SCALR_TOKEN", raising=False)
    _tty(monkeypatch)
    prompts = []

    def prompt(text, **kwargs):
        prompts.append((text, kwargs.get("hide_input")))
        return "typed-token"

    with (
        patch("archie_cli.auth.click.prompt", side_effect=prompt),
        patch("archie_cli.auth._orchestrator_url", return_value="http://test"),
        patch("archie_cli.auth.httpx.put", return_value=_response()) as put,
    ):
        _set_static_from_login("scalr")
    assert prompts == [("scalr token", True)]
    assert put.call_args.kwargs["json"] == {"token": "typed-token"}


def test_mapped_env_set_skips_prompt(monkeypatch):
    monkeypatch.setenv("SCALR_TOKEN", "env-token")
    _tty(monkeypatch)
    with (
        patch("archie_cli.auth.click.prompt") as prompt,
        patch("archie_cli.auth._orchestrator_url", return_value="http://test"),
        patch("archie_cli.auth.httpx.put", return_value=_response()) as put,
    ):
        _set_static_from_login("scalr")
    prompt.assert_not_called()
    assert put.call_args.kwargs["json"] == {"token": "env-token"}


def test_missing_values_without_terminal_or_stdin_fail_with_env_names(monkeypatch):
    monkeypatch.delenv("SCALR_TOKEN", raising=False)
    monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: False, read=lambda: ""))
    with patch("archie_cli.auth.httpx.put") as put, pytest.raises(Exception, match="SCALR_TOKEN"):
        _set_static_from_login("scalr")
    put.assert_not_called()


def test_missing_settings_lists_only_unset_scalars_without_default():
    (home_dir() / "config.yaml").write_text(COMMENTED)
    config = load_nexus_config()
    assert _missing_settings("scalr", config) == {"account": "str"}
    assert _missing_settings("datadog", config) == {}  # site has a default
    assert _missing_settings("github", config) == {}  # work_orgs is a list
    assert _missing_settings("bedrock", config) == {}  # core provider, no manifest


def test_prompt_settings_writes_config_and_keeps_comments(monkeypatch):
    path = home_dir() / "config.yaml"
    path.write_text(COMMENTED)
    _tty(monkeypatch)
    with patch("archie_cli.auth.click.prompt", return_value="acct-1"):
        _prompt_settings("scalr")
    text = path.read_text()
    for comment in ("# top comment", "# inline comment", "# keep me"):
        assert comment in text
    assert yaml.safe_load(text)["plugins"]["scalr"]["settings"] == {
        "hostname": "scalr.example",
        "account": "acct-1",
    }


def test_prompt_settings_blank_answer_leaves_config_alone(monkeypatch):
    path = home_dir() / "config.yaml"
    path.write_text(COMMENTED)
    _tty(monkeypatch)
    with patch("archie_cli.auth.click.prompt", return_value=""):
        _prompt_settings("scalr")
    assert path.read_text() == COMMENTED


def test_login_without_terminal_reports_missing_settings(monkeypatch):
    monkeypatch.setenv("SCALR_TOKEN", "env-token")
    with (
        patch("archie_cli.auth._orchestrator_url", return_value="http://test"),
        patch("archie_cli.auth.httpx.put", return_value=_response()),
    ):
        result = CliRunner().invoke(login, ["scalr"])
    assert result.exit_code == 0, result.output
    assert "plugins.scalr.settings.hostname" in result.output
    assert "plugins.scalr.settings.account" in result.output
    assert not (home_dir() / "config.yaml").exists()


def test_set_plugin_settings_creates_file_with_private_mode():
    path = home_dir() / "config.yaml"
    set_plugin_settings(path, "scalr", {"hostname": "h"})
    assert yaml.safe_load(path.read_text()) == {
        "plugins": {"scalr": {"settings": {"hostname": "h"}}}
    }
    assert path.stat().st_mode & 0o777 == 0o600


def test_set_plugin_settings_leaves_untouched_lines_byte_identical():
    path = home_dir() / "config.yaml"
    long_id = "492115156889-" + "a" * 60 + ".apps.googleusercontent.com"
    original = (
        "# header\n"
        "plugins:\n"
        "  google:\n"
        "    auth:\n"
        f"      client_id: {long_id}\n"
        "  scalr:\n"
        "    settings:\n"
        "      hostname: 'quoted.example'  # note\n"
    )
    path.write_text(original)
    set_plugin_settings(path, "scalr", {"account": "acct"})
    assert path.read_text() == original + "      account: acct\n"


def test_set_plugin_settings_quotes_strings_yaml11_would_misread():
    path = home_dir() / "config.yaml"
    set_plugin_settings(path, "scalr", {"hostname": "yes", "account": "1:20"})
    assert yaml.safe_load(path.read_text())["plugins"]["scalr"]["settings"] == {
        "hostname": "yes",
        "account": "1:20",
    }


def test_set_plugin_settings_writes_through_symlink(tmp_path):
    real = tmp_path / "real.yaml"
    real.write_text(COMMENTED)
    link = home_dir() / "config.yaml"
    link.symlink_to(real)
    set_plugin_settings(link, "scalr", {"account": "acct"})
    assert link.is_symlink()
    assert yaml.safe_load(real.read_text())["plugins"]["scalr"]["settings"]["account"] == "acct"


def test_prompt_settings_reprompts_on_invalid_typed_value(monkeypatch):
    from archie_cli import auth

    _tty(monkeypatch)
    monkeypatch.setattr(auth, "_missing_settings", lambda service, config: {"port": "int"})
    answers = iter(["not-a-number", "8080"])
    with (
        patch("archie_cli.auth.click.prompt", side_effect=lambda *a, **k: next(answers)),
        patch("archie_cli.config_edit.set_plugin_settings") as write,
    ):
        _prompt_settings("scalr")
    write.assert_called_once()
    assert write.call_args.args[1:] == ("scalr", {"port": 8080})


def test_set_plugin_settings_rejects_invalid_result_and_keeps_file():
    path = home_dir() / "config.yaml"
    path.write_text(COMMENTED)
    with pytest.raises(ConfigEditError, match="config.yaml"):
        set_plugin_settings(path, "scalr", {"nosuchsetting": "x"})
    assert path.read_text() == COMMENTED
    assert [p.name for p in path.parent.iterdir()] == ["config.yaml"]

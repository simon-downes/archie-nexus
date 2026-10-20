"""Tests for ``migrate_config()`` and ``archie config migrate`` (070 M4, D1, D19)."""

import os
import stat
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from archie_cli.cli import main
from archie_shared.plugins.migrate import MigrationError, MigrationResult, migrate_config
from archie_shared.schemas import load_nexus_config
from click.testing import CliRunner

REALISTIC = """\
global:
  model: "bedrock-openai-gpt-5-6-luna"
  region: "eu-west-1"
  workspace_root: "~/dev"
mounts:
  - ~/.gitconfig*
  - [~/.ssh/id_ed25519*, ~/.ssh:ro]
auth:
  providers:
    slack:
      client_id: "111.222"
    google:
      client_id: "abc.apps.googleusercontent.com"
    github:
      work_orgs: [TilloTech]
tools:
  notion: {read: {enabled: true}, write: {enabled: true}}
  slack:
    read: {enabled: true, scope: {deny: []}}
    write: {enabled: true, scope: {deny: []}}
  google: {read: {enabled: true}, write: {enabled: true}}
  jira: {read: {enabled: true}, write: {enabled: true}}
"""

RW = {"read": {"enabled": True}, "write": {"enabled": True}}


# --- migrate_config ---------------------------------------------------------------


def test_no_old_sections_is_unchanged():
    raw = {"global": {"model": "x"}, "plugins": {"jira": {"enabled": False}}}
    result = migrate_config(raw)
    assert result == MigrationResult(data=raw, changed=False, notes=[])


def test_empty_config_is_unchanged():
    assert migrate_config({}).changed is False


def test_result_is_frozen():
    result = migrate_config({})
    with pytest.raises(AttributeError):
        result.changed = True  # type: ignore[misc]


def test_tools_become_policy():
    result = migrate_config({"tools": {"jira": {"read": {"enabled": True, "scope": ["A"]}}}})
    assert result.changed
    assert result.data == {
        "plugins": {"jira": {"policy": {"read": {"enabled": True, "scope": ["A"]}}}}
    }


def test_github_work_orgs_becomes_setting():
    result = migrate_config({"auth": {"providers": {"github": {"work_orgs": ["acme"]}}}})
    assert result.data == {"plugins": {"github": {"settings": {"work_orgs": ["acme"]}}}}


def test_datadog_site_becomes_setting():
    result = migrate_config({"auth": {"providers": {"datadog": {"site": "datadoghq.com"}}}})
    assert result.data == {"plugins": {"datadog": {"settings": {"site": "datadoghq.com"}}}}


def test_site_for_other_provider_is_error():
    with pytest.raises(MigrationError, match=r"auth\.providers\.jira\.site"):
        migrate_config({"auth": {"providers": {"jira": {"site": "x"}}}})


def test_other_provider_fields_become_auth():
    raw = {"auth": {"providers": {"google": {"client_id": "c", "scopes": ["s"]}}}}
    result = migrate_config(raw)
    assert result.data == {"plugins": {"google": {"auth": {"client_id": "c", "scopes": ["s"]}}}}


def test_linear_dropped_with_note():
    raw = {
        "tools": {"linear": {"read": {"enabled": True}}},
        "auth": {"providers": {"linear": {"client_id": "x"}}},
    }
    result = migrate_config(raw)
    assert result.changed
    assert result.data == {"plugins": {}}
    assert result.notes == ["dropped linear (provider removed)"]


def test_bedrock_is_error():
    with pytest.raises(MigrationError) as exc:
        migrate_config({"auth": {"providers": {"bedrock": {"env": {}}}}})
    assert str(exc.value) == (
        "auth.providers.bedrock: bedrock has no config overrides any more; remove it"
    )


def test_unknown_auth_key_is_error():
    with pytest.raises(MigrationError, match=r"auth\.flows"):
        migrate_config({"auth": {"flows": {}}})


def test_merges_with_existing_plugins():
    raw = {
        "plugins": {"jira": {"enabled": False}, "slack": {"policy": RW}},
        "tools": {"jira": RW, "slack": RW},
    }
    result = migrate_config(raw)
    assert result.data == {
        "plugins": {"jira": {"enabled": False, "policy": RW}, "slack": {"policy": RW}}
    }
    # the input is not mutated
    assert raw["plugins"]["jira"] == {"enabled": False}


def test_conflict_with_existing_plugins_names_key():
    raw = {
        "plugins": {"github": {"settings": {"work_orgs": ["a"]}}},
        "auth": {"providers": {"github": {"work_orgs": ["b"]}}},
    }
    with pytest.raises(MigrationError, match=r"plugins\.github\.settings\.work_orgs"):
        migrate_config(raw)


def test_plugins_placed_where_auth_was():
    raw = {"global": {}, "auth": {"providers": {}}, "mounts": [], "tools": {}, "agent": {}}
    assert list(migrate_config(raw).data) == ["global", "plugins", "mounts", "agent"]


def test_plugins_at_end_without_auth():
    raw = {"tools": {}, "global": {}, "mounts": []}
    assert list(migrate_config(raw).data) == ["global", "mounts", "plugins"]


def test_existing_plugins_keeps_position():
    raw = {"plugins": {}, "global": {}, "auth": {"providers": {}}}
    assert list(migrate_config(raw).data) == ["plugins", "global"]


def test_realistic_config(tmp_path):
    raw = yaml.safe_load(REALISTIC)
    result = migrate_config(raw)
    assert result.changed
    assert result.notes == []
    assert list(result.data) == ["global", "mounts", "plugins"]
    assert result.data["global"] == raw["global"]
    assert result.data["mounts"] == raw["mounts"]
    deny = {"enabled": True, "scope": {"deny": []}}
    assert result.data["plugins"] == {
        "slack": {"auth": {"client_id": "111.222"}, "policy": {"read": deny, "write": deny}},
        "google": {"auth": {"client_id": "abc.apps.googleusercontent.com"}, "policy": RW},
        "github": {"settings": {"work_orgs": ["TilloTech"]}},
        "notion": {"policy": RW},
        "jira": {"policy": RW},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(result.data, sort_keys=False, default_flow_style=False))
    config = load_nexus_config(path)
    assert config.plugins["github"].settings == {"work_orgs": ["TilloTech"]}
    assert config.plugins["slack"].auth.client_id == "111.222"


# --- archie config migrate --------------------------------------------------------


def _run(path: Path):
    return CliRunner().invoke(main, ["config", "migrate", "--path", str(path)])


def _backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.bak-*"))


def test_cli_migrates_and_backs_up(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(REALISTIC)
    original = path.read_bytes()

    result = _run(path)
    assert result.exit_code == 0, result.output
    backups = _backups(path)
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert f"Migrated {path}; backup at {backups[0]}." in result.output
    assert yaml.safe_load(path.read_text()) == migrate_config(yaml.safe_load(REALISTIC)).data
    load_nexus_config(path)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(["config.yaml", backups[0].name])

    migrated = path.read_bytes()
    again = _run(path)
    assert again.exit_code == 0, again.output
    assert f"{path} is already migrated." in again.output
    assert _backups(path) == backups
    assert path.read_bytes() == migrated


def test_cli_prints_notes(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("tools:\n  linear: {read: {enabled: true}}\n")
    result = _run(path)
    assert result.exit_code == 0, result.output
    assert "dropped linear (provider removed)" in result.output
    assert yaml.safe_load(path.read_text()) == {"plugins": {}}


def test_cli_backup_name_collision(tmp_path, monkeypatch):
    import archie_cli.config_cmd as config_cmd

    class FixedDatetime:
        @staticmethod
        def now(tz):
            return datetime(2026, 1, 2, 3, 4, 5, tzinfo=tz)

    monkeypatch.setattr(config_cmd, "datetime", FixedDatetime)
    path = tmp_path / "config.yaml"
    path.write_text("tools: {}\n")
    (tmp_path / "config.yaml.bak-20260102T030405Z").write_text("old")
    (tmp_path / "config.yaml.bak-20260102T030405Z-1").write_text("old")
    result = _run(path)
    assert result.exit_code == 0, result.output
    backup = tmp_path / "config.yaml.bak-20260102T030405Z-2"
    assert backup.read_text() == "tools: {}\n"
    assert f"backup at {backup}." in result.output


def test_cli_conflict_writes_nothing(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "plugins:\n  github:\n    settings: {work_orgs: [a]}\n"
        "auth:\n  providers:\n    github: {work_orgs: [b]}\n"
    )
    original = path.read_bytes()
    result = _run(path)
    assert result.exit_code == 1
    assert "plugins.github.settings.work_orgs" in result.output
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_cli_validation_failure_writes_nothing(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("tools:\n  unknownplugin: {read: {enabled: true}}\n")
    original = path.read_bytes()
    result = _run(path)
    assert result.exit_code == 1
    assert "unknownplugin" in result.output
    assert f"Migrated config for {path} is invalid, nothing written" in result.output
    assert ".tmp" not in result.output
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_cli_missing_file(tmp_path):
    path = tmp_path / "config.yaml"
    result = _run(path)
    assert result.exit_code == 0
    assert f"No config file at {path}; nothing to migrate." in result.output
    assert not path.exists()


def test_cli_default_path_uses_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    result = CliRunner().invoke(main, ["config", "migrate"])
    assert result.exit_code == 0
    assert f"No config file at {tmp_path / 'config.yaml'}" in result.output


def test_cli_non_mapping_is_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("- a\n- b\n")
    result = _run(path)
    assert result.exit_code == 1
    assert list(tmp_path.iterdir()) == [path]


def test_cli_empty_file_is_already_migrated(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("")
    result = _run(path)
    assert result.exit_code == 0
    assert "is already migrated" in result.output


def test_cli_preserves_mode(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(REALISTIC)
    os.chmod(path, 0o600)
    result = _run(path)
    assert result.exit_code == 0, result.output
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(_backups(path)[0].stat().st_mode) == 0o600

"""Tests for the NexusConfig application schema (schemas.py)."""

from pathlib import Path

import pytest
from archie_shared.config import ConfigError
from archie_shared.schemas import NexusConfig, expand_workspace_root, load_nexus_config

# --- Tests: load_nexus_config with defaults ---


def test_load_nexus_config_no_file_returns_defaults(monkeypatch, tmp_path):
    """No config file at default path → NexusConfig with all defaults."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    config = load_nexus_config()
    assert config.global_.model == "bedrock-openai-gpt-5-6-luna"
    assert config.global_.workspace_root == "~/dev"
    assert config.global_.region == "eu-west-1"
    assert config.agent.turn_cost_limit == 50.0


def test_load_nexus_config_empty_file(monkeypatch, tmp_path):
    """Empty config file → all defaults."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    (tmp_path / "config.yaml").write_text("")
    config = load_nexus_config()
    assert config.global_.model == "bedrock-openai-gpt-5-6-luna"
    assert config.global_.region == "eu-west-1"


# --- Tests: load_nexus_config with content ---


def test_compaction_defaults_and_custom_values(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "agent:\n  compaction:\n    model: compact\n    percent: 90\n    tokens: 70000\n    cold_cost_threshold: 1.25\n"
    )
    config = load_nexus_config(path=cfg)
    assert config.agent.compaction.model == "compact"
    assert config.agent.compaction.percent == 90
    assert config.agent.compaction.tokens == 70000
    assert config.agent.compaction.cold_cost_threshold == 1.25


@pytest.mark.parametrize(
    "field, value",
    [("percent", 49), ("percent", 96), ("tokens", 63999), ("cold_cost_threshold", -1)],
)
def test_compaction_ranges_rejected(tmp_path, field, value):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"agent:\n  compaction:\n    {field}: {value}\n")
    with pytest.raises(ConfigError, match="Validation error"):
        load_nexus_config(path=cfg)


def test_compaction_unknown_field_rejected(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("agent:\n  compaction:\n    typo: true\n")
    with pytest.raises(ConfigError, match="Validation error"):
        load_nexus_config(path=cfg)


def test_load_nexus_config_turn_cost_limit(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("agent:\n  turn_cost_limit: 12.5\n")
    config = load_nexus_config(path=cfg)
    assert config.agent.turn_cost_limit == 12.5


def test_load_nexus_config_full(tmp_path):
    """Full config with all sections."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "global:\n"
        "  model: bedrock-claude-sonnet-5-5\n"
        "  workspace_root: ~/projects\n"
        "  region: us-east-1\n"
        "cli: {}\n"
        "agent: {}\n"
        "web: {}\n"
    )
    config = load_nexus_config(path=cfg)
    assert config.global_.model == "bedrock-claude-sonnet-5-5"
    assert config.global_.workspace_root == "~/projects"
    assert config.global_.region == "us-east-1"


def test_load_nexus_config_partial_global_only(tmp_path):
    """Only global section present → other sections default."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("global:\n  model: bedrock-claude-haiku-4-5\n")
    config = load_nexus_config(path=cfg)
    assert config.global_.model == "bedrock-claude-haiku-4-5"
    assert config.global_.region == "eu-west-1"  # default


def test_load_nexus_config_partial_global_fields(tmp_path):
    """Partial global section → unspecified fields get defaults."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("global:\n  region: ap-southeast-1\n")
    config = load_nexus_config(path=cfg)
    assert config.global_.model == "bedrock-openai-gpt-5-6-luna"  # default
    assert config.global_.region == "ap-southeast-1"


def test_load_nexus_config_mounts(tmp_path):
    """Mount entries decode as source strings and source/destination pairs."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("mounts:\n  - ~/.gitconfig*\n  - [~/.ssh/id_ed25519*, ~/.ssh:ro]\n")
    config = load_nexus_config(path=cfg)
    assert config.mounts == ("~/.gitconfig*", ("~/.ssh/id_ed25519*", "~/.ssh:ro"))


# --- Tests: load_nexus_config error cases ---


def test_load_nexus_config_explicit_path_missing(tmp_path):
    """Explicit path that doesn't exist → ConfigError."""
    missing = tmp_path / "nonexistent.yaml"
    with pytest.raises(ConfigError, match="Config file not found"):
        load_nexus_config(path=missing)


def test_load_nexus_config_unknown_field_rejected(tmp_path):
    """Unknown fields in config → ConfigError (forbid_unknown_fields)."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("global:\n  model: test\n  typo_field: oops\n")
    with pytest.raises(ConfigError, match="Validation error"):
        load_nexus_config(path=cfg)


def test_load_nexus_config_unknown_top_level_section(tmp_path):
    """Unknown top-level section → ConfigError."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("global:\n  model: test\nunknown_section:\n  key: val\n")
    with pytest.raises(ConfigError, match="Validation error"):
        load_nexus_config(path=cfg)


# --- Tests: env var integration ---


def test_load_nexus_config_respects_archie_home_dir(monkeypatch, tmp_path):
    """ARCHIE_HOME_DIR determines where config.yaml is loaded from."""
    custom_dir = tmp_path / "custom_home"
    custom_dir.mkdir()
    (custom_dir / "config.yaml").write_text("global:\n  region: eu-central-1\n")
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(custom_dir))
    config = load_nexus_config()
    assert config.global_.region == "eu-central-1"


# --- Tests: OrchestratorConfig / OrchestratorProfile ---


def test_orchestrator_config_defaults(monkeypatch, tmp_path):
    """Config without orchestrator key → empty profiles dict."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    config = load_nexus_config()
    assert config.orchestrator.profiles == {}


def test_get_profile_default_fallback(monkeypatch, tmp_path):
    """get_profile with empty profiles → OrchestratorProfile() defaults."""
    from archie_shared.schemas import get_profile

    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    config = load_nexus_config()
    profile = get_profile(config.orchestrator)
    assert profile.host == "127.0.0.1"
    assert profile.port == 7600


def test_get_profile_named(tmp_path):
    """Named profile loaded from config is returned by get_profile."""
    from archie_shared.schemas import get_profile

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "orchestrator:\n  profiles:\n    gpu-box:\n      host: 192.168.1.50\n      port: 7601\n"
    )
    config = load_nexus_config(path=cfg)
    profile = get_profile(config.orchestrator, "gpu-box")
    assert profile.host == "192.168.1.50"
    assert profile.port == 7601


def test_get_profile_unknown_explicit_name_raises(tmp_path):
    """Requesting an unknown *explicit* profile name raises KeyError.

    Silently returning localhost defaults for a typo'd name would be a footgun.
    Only the implicit 'default' name falls back to defaults.
    """
    import pytest
    from archie_shared.schemas import get_profile

    cfg = tmp_path / "config.yaml"
    cfg.write_text("orchestrator:\n  profiles:\n    other:\n      host: 10.0.0.1\n")
    config = load_nexus_config(path=cfg)
    with pytest.raises(KeyError):
        get_profile(config.orchestrator, "nonexistent")


def test_orchestrator_config_unknown_key_rejected(tmp_path):
    """Unknown key under orchestrator raises ConfigError."""
    from archie_shared.config import ConfigError

    cfg = tmp_path / "config.yaml"
    cfg.write_text("orchestrator:\n  unknown_key: oops\n")
    with pytest.raises(ConfigError, match="Validation error"):
        load_nexus_config(path=cfg)


# --- Tests: expand_workspace_root ---


def test_expand_workspace_root_tilde():
    """Tilde in workspace_root is expanded."""
    config = NexusConfig()
    result = expand_workspace_root(config)
    assert result == Path.home() / "dev"


def test_expand_workspace_root_absolute(tmp_path):
    """Absolute path is unchanged."""
    from archie_shared.schemas import GlobalConfig

    config = NexusConfig(global_=GlobalConfig(workspace_root=str(tmp_path / "projects")))
    result = expand_workspace_root(config)
    assert result == tmp_path / "projects"


# --- Tests: plugins section ---


def _load(tmp_path, text):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(text)
    return load_nexus_config(path=cfg)


PLUGINS_YAML = """\
plugins:
  github:
    settings:
      work_orgs: [tillo, Tillo-Labs]
  datadog:
    settings: {site: us5.datadoghq.com}
  jira:
    policy:
      read: {enabled: true, scope: [PLAT]}
      write: {enabled: true, scope: [PLAT]}
  notion:
    policy:
      write: {enabled: true, scope: {pages: [abc]}}
  slack:
    auth: {client_id: client}
    policy:
      read: {scope: {deny: ["#hr"]}}
  scalr:
    enabled: false
"""


def test_plugins_section_parses(tmp_path):
    config = _load(tmp_path, PLUGINS_YAML)
    assert config.plugins["github"].settings == {"work_orgs": ["tillo", "Tillo-Labs"]}
    assert config.plugins["jira"].policy.write.enabled is True
    assert config.plugins["jira"].policy.write.scope == ["PLAT"]
    assert config.plugins["slack"].auth.client_id == "client"
    assert config.plugins["scalr"].enabled is False
    assert config.plugins["notion"].policy.read is None


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("tools:\n  notion:\n    write: {enabled: true}\n", "'tools' section was"),
        ("auth:\n  providers:\n    github: {work_orgs: [tillo]}\n", "'auth' section was"),
        ("tools: {}\nauth: {}\n", "'tools' and 'auth' sections were"),
    ],
)
def test_removed_sections_name_migrate(tmp_path, text, names):
    with pytest.raises(ConfigError, match="archie config migrate") as exc:
        _load(tmp_path, text)
    assert names in str(exc.value)
    assert str(tmp_path / "config.yaml") in str(exc.value)


def test_removed_section_checked_before_other_errors(tmp_path):
    with pytest.raises(ConfigError, match="archie config migrate"):
        _load(tmp_path, "tools: {}\nbogus_top_level: 1\n")


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("plugins:\n  linear: {}\n", r"plugins\.linear: unknown plugin 'linear'"),
        ("plugins:\n  bedrock: {}\n", r"plugins\.bedrock: unknown plugin"),
        (
            "plugins:\n  datadog:\n    settings: {region: x}\n",
            r"plugins\.datadog\.settings.*region",
        ),
        ("plugins:\n  datadog:\n    settings: {site: 5}\n", r"plugins\.datadog\.settings.*str"),
        (
            "plugins:\n  github:\n    settings: {work_orgs: tillo}\n",
            r"plugins\.github\.settings.*work_orgs",
        ),
        (
            "plugins:\n  incident_io:\n    policy:\n      write: {enabled: true}\n",
            r"plugins\.incident_io\.policy\.write: plugin 'incident_io' has no write tools",
        ),
        (
            "plugins:\n  datadog:\n    policy:\n      read: {scope: [x]}\n",
            r"plugins\.datadog\.policy\.read\.scope: plugin 'datadog' has no scope",
        ),
        (
            "plugins:\n  jira:\n    policy:\n      read: {scope: PLAT}\n",
            r"plugins\.jira\.policy\.read\.scope",
        ),
        (
            "plugins:\n  jira:\n    policy:\n      read: {scope: [1]}\n",
            r"plugins\.jira\.policy\.read\.scope",
        ),
        (
            "plugins:\n  slack:\n    policy:\n      read: {scope: {allow: [x]}}\n",
            r"plugins\.slack\.policy\.read\.scope.*allow",
        ),
        (
            "plugins:\n  github:\n    policy:\n      read: {enabled: true}\n",
            r"plugins\.github\.policy: plugin 'github' has no tool policy",
        ),
        (
            "plugins:\n  datadog:\n    policy:\n      read: {enabled: true, bogus: 1}\n",
            "bogus",
        ),
        ("plugins:\n  github:\n    auth: {site: x}\n", "site"),
        ("plugins:\n  github:\n    auth: {work_orgs: [tillo]}\n", "work_orgs"),
        (
            "plugins:\n  slack:\n    auth: {set_env: true}\n",
            r"plugins\.slack\.auth: set_env not allowed for an oauth credential",
        ),
        (
            "plugins:\n  datadog:\n    auth: {client_id: x}\n",
            r"plugins\.datadog\.auth: client_id not allowed for a static credential",
        ),
        (
            "plugins:\n  scalr:\n    auth:\n      token_endpoint: https://x\n"
            "      scopes: [a]\n      extra_params: {a: b}\n      set_env: true\n",
            r"plugins\.scalr\.auth: token_endpoint, scopes, extra_params not allowed for a "
            r"static credential",
        ),
    ],
)
def test_plugins_validation_errors(tmp_path, text, match):
    with pytest.raises(ConfigError, match=match):
        _load(tmp_path, text)


def test_auth_for_plugin_without_credential(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    (plugins / "demo").mkdir(parents=True)
    (plugins / "demo" / "plugin.yaml").write_text("name: demo\n")
    monkeypatch.setenv("ARCHIE_PLUGINS_DIR", str(plugins))
    with pytest.raises(ConfigError, match=r"plugins\.demo\.auth: plugin 'demo' declares no"):
        _load(tmp_path, "plugins:\n  demo:\n    auth: {client_id: x}\n")


def test_empty_scope_allowed_without_scope_schema(tmp_path):
    config = _load(tmp_path, "plugins:\n  datadog:\n    policy:\n      read: {scope: []}\n")
    assert config.plugins["datadog"].policy.read.scope == []


def test_policy_snapshot_defaults_and_overrides(tmp_path):
    from archie_shared.plugins.config import policy_snapshot
    from archie_shared.plugins.manifest import load_manifests

    config = _load(tmp_path, PLUGINS_YAML)
    snapshot = policy_snapshot(config.plugins, load_manifests())
    assert set(snapshot) == {"datadog", "google", "incident_io", "jira", "notion", "slack"}
    assert snapshot["jira"] == {
        "read": {"enabled": True, "scope": ["PLAT"]},
        "write": {"enabled": True, "scope": ["PLAT"]},
    }
    assert snapshot["notion"]["read"] == {"enabled": True, "scope": None}
    assert snapshot["notion"]["write"] == {"enabled": True, "scope": {"pages": ["abc"]}}
    assert snapshot["slack"]["read"] == {"enabled": True, "scope": {"deny": ["#hr"]}}
    assert snapshot["incident_io"]["write"] == {"enabled": False, "scope": None}
    assert snapshot["datadog"] == {
        "read": {"enabled": True, "scope": None},
        "write": {"enabled": False, "scope": None},
    }


def test_policy_snapshot_disables_both_blocks_for_disabled_plugins(tmp_path):
    from archie_shared.plugins.config import policy_snapshot
    from archie_shared.plugins.manifest import load_manifests

    config = _load(tmp_path, "plugins:\n  datadog: {enabled: false}\n")
    off = {"enabled": False, "scope": None}
    assert policy_snapshot(config.plugins, load_manifests())["datadog"] == {
        "read": off,
        "write": off,
    }


def test_resolved_settings_merges_defaults(tmp_path):
    from archie_shared.plugins.config import resolved_settings
    from archie_shared.plugins.manifest import load_manifests

    manifests = load_manifests()
    assert resolved_settings("datadog", {}, manifests) == {"site": "app.datadoghq.eu"}
    config = _load(tmp_path, PLUGINS_YAML)
    assert resolved_settings("datadog", config.plugins, manifests) == {"site": "us5.datadoghq.com"}
    assert resolved_settings("github", {}, manifests) == {"work_orgs": None}
    assert resolved_settings("aws", {}, manifests) == {"region": "eu-west-1", "profiles": None}

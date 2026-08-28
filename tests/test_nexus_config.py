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


def test_load_nexus_config_empty_file(monkeypatch, tmp_path):
    """Empty config file → all defaults."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    (tmp_path / "config.yaml").write_text("")
    config = load_nexus_config()
    assert config.global_.model == "bedrock-openai-gpt-5-6-luna"
    assert config.global_.region == "eu-west-1"


# --- Tests: load_nexus_config with content ---


def test_load_nexus_config_full(tmp_path):
    """Full config with all sections."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "global:\n"
        "  model: bedrock-claude-opus-4-6\n"
        "  workspace_root: ~/projects\n"
        "  region: us-east-1\n"
        "cli: {}\n"
        "agent: {}\n"
        "web: {}\n"
    )
    config = load_nexus_config(path=cfg)
    assert config.global_.model == "bedrock-claude-opus-4-6"
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
        "orchestrator:\n"
        "  profiles:\n"
        "    gpu-box:\n"
        "      host: 192.168.1.50\n"
        "      port: 7601\n"
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
    cfg.write_text(
        "orchestrator:\n"
        "  profiles:\n"
        "    other:\n"
        "      host: 10.0.0.1\n"
    )
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

"""Tests for the generic config loader (config.py)."""

from pathlib import Path

import msgspec
import pytest
from archie_shared.config import ConfigError, home_dir, load_config

# --- Test schemas ---


class NestedStruct(msgspec.Struct):
    value: int = 10
    label: str = "default"


class SimpleSchema(msgspec.Struct, forbid_unknown_fields=True):
    name: str = "test"
    count: int = 0
    ratio: float = 1.0


class NestedSchema(msgspec.Struct, forbid_unknown_fields=True):
    title: str = "untitled"
    nested: NestedStruct = msgspec.field(default_factory=NestedStruct)


class DictSchema(msgspec.Struct):
    """Schema that uses dict[str, NestedStruct]."""


# --- Tests: home_dir ---


def test_home_dir_default(monkeypatch):
    """Default home dir is ~/.nexus."""
    monkeypatch.delenv("ARCHIE_HOME_DIR", raising=False)
    result = home_dir()
    assert result == Path.home() / ".nexus"


def test_home_dir_from_env(monkeypatch, tmp_path):
    """ARCHIE_HOME_DIR env var overrides default."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path / "custom"))
    result = home_dir()
    assert result == tmp_path / "custom"


def test_home_dir_expands_tilde(monkeypatch):
    """Tilde in ARCHIE_HOME_DIR is expanded."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", "~/my-nexus")
    result = home_dir()
    assert result == Path.home() / "my-nexus"


# --- Tests: load_config valid cases ---


def test_load_config_full(tmp_path):
    """Load a fully populated config file."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("name: hello\ncount: 5\nratio: 2.5\n")
    result = load_config(cfg, SimpleSchema)
    assert result.name == "hello"
    assert result.count == 5
    assert result.ratio == 2.5


def test_load_config_defaults(tmp_path):
    """Empty file → all schema defaults apply."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("")
    result = load_config(cfg, SimpleSchema)
    assert result.name == "test"
    assert result.count == 0
    assert result.ratio == 1.0


def test_load_config_partial(tmp_path):
    """Partial config → specified values + defaults for the rest."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("name: partial\n")
    result = load_config(cfg, SimpleSchema)
    assert result.name == "partial"
    assert result.count == 0


def test_load_config_nested_struct(tmp_path):
    """Nested struct fields are loaded correctly."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("title: my-project\nnested:\n  value: 42\n  label: custom\n")
    result = load_config(cfg, NestedSchema)
    assert result.title == "my-project"
    assert result.nested.value == 42
    assert result.nested.label == "custom"


def test_load_config_nested_defaults(tmp_path):
    """Nested struct uses defaults when not specified."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("title: minimal\n")
    result = load_config(cfg, NestedSchema)
    assert result.nested.value == 10
    assert result.nested.label == "default"


def test_load_config_dict_schema(tmp_path):
    """dict[str, Struct] schema loads dynamic keys."""
    cfg = tmp_path / "models.yaml"
    cfg.write_text("alpha:\n  value: 1\n  label: first\nbeta:\n  value: 2\n")
    result = load_config(cfg, dict[str, NestedStruct])
    assert "alpha" in result
    assert result["alpha"].value == 1
    assert result["alpha"].label == "first"
    assert "beta" in result
    assert result["beta"].value == 2
    assert result["beta"].label == "default"


def test_load_config_dict_schema_empty_file(tmp_path):
    """dict schema with empty file → empty dict."""
    cfg = tmp_path / "models.yaml"
    cfg.write_text("")
    result = load_config(cfg, dict[str, NestedStruct])
    assert result == {}


def test_load_config_int_to_float_coercion(tmp_path):
    """strict=False allows int → float coercion."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("name: coerce\nratio: 5\n")
    result = load_config(cfg, SimpleSchema)
    assert result.ratio == 5.0
    assert isinstance(result.ratio, float)


# --- Tests: load_config error cases ---


def test_load_config_missing_file(tmp_path):
    """Missing file raises ConfigError with path in message."""
    path = tmp_path / "nonexistent.yaml"
    with pytest.raises(ConfigError, match="Config file not found"):
        load_config(path, SimpleSchema)
    with pytest.raises(ConfigError, match=str(path)):
        load_config(path, SimpleSchema)


def test_load_config_bad_yaml(tmp_path):
    """Malformed YAML raises ConfigError."""
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(":\n  - [invalid yaml{{\n")
    with pytest.raises(ConfigError, match="Invalid YAML"):
        load_config(cfg, SimpleSchema)


def test_load_config_validation_error(tmp_path):
    """Schema validation failure raises ConfigError."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("count: not_a_number\n")
    with pytest.raises(ConfigError, match="Validation error"):
        load_config(cfg, SimpleSchema)


def test_load_config_unknown_field_rejected(tmp_path):
    """Unknown fields are rejected with forbid_unknown_fields=True."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("name: hello\nunknown_key: surprise\n")
    with pytest.raises(ConfigError, match="Validation error"):
        load_config(cfg, SimpleSchema)


def test_load_config_non_mapping_root(tmp_path):
    """Non-mapping root (e.g. a list) raises ConfigError when schema expects Struct."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("- item1\n- item2\n")
    with pytest.raises(ConfigError, match="Validation error"):
        load_config(cfg, SimpleSchema)

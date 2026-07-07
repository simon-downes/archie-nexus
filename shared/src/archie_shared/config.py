"""Configuration framework — generic YAML loader with msgspec validation.

Provides:
- home_dir(): resolve ARCHIE_HOME_DIR (default ~/.nexus)
- load_config(): load YAML file and validate against a msgspec Struct schema
- ConfigError: custom exception for all config-related failures
"""

import os
from pathlib import Path

import msgspec
import yaml


class ConfigError(Exception):
    """Raised on config load failures (missing file, bad YAML, validation)."""


def home_dir() -> Path:
    """Resolve the archie home directory.

    Reads ARCHIE_HOME_DIR env var, defaults to ~/.nexus. Expands user home.
    """
    raw = os.environ.get("ARCHIE_HOME_DIR", "~/.nexus")
    return Path(raw).expanduser()


def load_config[T](path: str | Path, schema: type[T]) -> T:
    """Load a YAML file and validate against a msgspec Struct schema.

    Args:
        path: Path to the YAML file.
        schema: A msgspec Struct type (or dict type) to validate against.

    Returns:
        A validated instance of the schema type.

    Raises:
        ConfigError: On missing file, malformed YAML, or validation failure.
    """
    path = Path(path)

    try:
        raw_text = path.read_text()
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}") from None
    except OSError as e:
        raise ConfigError(f"Cannot read config file {path}: {e}") from None

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from None

    # Empty file (YAML loads as None) → treat as empty mapping
    if data is None:
        data = {}

    try:
        return msgspec.convert(data, schema, strict=False)
    except msgspec.ValidationError as e:
        raise ConfigError(f"Validation error in {path}: {e}") from None

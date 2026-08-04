"""Configuration framework — generic YAML loader with msgspec validation.

Provides:
- home_dir(): resolve ARCHIE_HOME_DIR (default ~/.nexus)
- persona_dir(): resolve ARCHIE_PERSONA_DIR (repo-tracked skills + prompts)
- load_config(): load YAML file and validate against a msgspec Struct schema
- ConfigError: custom exception for all config-related failures
"""

import os
from pathlib import Path

import msgspec
import yaml

# Repo root, computed relative to this file:
# shared/src/archie_shared/config.py -> parents[3] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]


class ConfigError(Exception):
    """Raised on config load failures (missing file, bad YAML, validation)."""


def home_dir() -> Path:
    """Resolve the archie home directory (user data: config, creds, sessions, brain).

    Reads ARCHIE_HOME_DIR env var, defaults to ~/.nexus. Expands user home.
    """
    raw = os.environ.get("ARCHIE_HOME_DIR", "~/.nexus")
    return Path(raw).expanduser()


def persona_dir() -> Path:
    """Resolve the persona directory (repo-tracked skills + prompt files).

    Reads ARCHIE_PERSONA_DIR env var. On the host this defaults to
    ``<repo_root>/persona``; inside the container the orchestrator sets the env
    var to the mounted path (``/opt/archie/persona``). The directory is mounted
    read-write so agent sessions can evolve skills and prompts, with git as the
    review/rollback mechanism. Distinct from home_dir() — persona is shipped,
    evolvable content, not per-user data.
    """
    raw = os.environ.get("ARCHIE_PERSONA_DIR")
    if raw:
        return Path(raw).expanduser()
    return _REPO_ROOT / "persona"


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

"""Configuration loading from ~/.archie/nexus.yaml (or ARCHIE_CONFIG env var).

Config is minimal — just the things a user might want to change:
- model: which Bedrock inference profile to use
- region: AWS region for API calls
- project_root: base directory for project detection

Model properties (pricing, context limits) are NOT config — they're constants
in models.py because users can't change them.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from archie_shared.models import get_model_info

# Filesystem paths for archie's data.
ARCHIE_DIR = Path.home() / ".archie"
DEFAULT_CONFIG_PATH = ARCHIE_DIR / "nexus.yaml"

# Written to disk on first run if no config exists.
DEFAULT_CONFIG = """\
model: "eu.anthropic.claude-sonnet-4-6"
region: "eu-west-1"
project_root: "~/dev"
"""


@dataclass(frozen=True)
class Config:
    """Immutable application configuration."""

    model: str
    region: str
    project_root: Path = field(default_factory=lambda: Path.home() / "dev")


def get_config_path() -> Path:
    """Determine config file path from ARCHIE_CONFIG env var or default."""
    env_path = os.environ.get("ARCHIE_CONFIG")
    if env_path:
        return Path(env_path)
    return DEFAULT_CONFIG_PATH


def ensure_default_config() -> Path:
    """Create default config file if it doesn't exist. Returns the path."""
    config_path = get_config_path()
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(DEFAULT_CONFIG)
    return config_path


def load_config() -> Config:
    """Load config from nexus.yaml.

    Uses ARCHIE_CONFIG env var if set, otherwise ~/.archie/nexus.yaml.
    Creates a default config on first run.

    Raises:
        ValueError: If config file is malformed or missing required fields.
        KeyError: If the configured model ID isn't in the known models registry.
    """
    config_path = ensure_default_config()

    raw = yaml.safe_load(config_path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid config format in {config_path}")

    model = raw.get("model")
    if not model:
        raise ValueError(f"'model' is required in {config_path}")

    # Validate that the model ID exists in our registry.
    get_model_info(model)

    region = raw.get("region", "eu-west-1")
    project_root = Path(raw.get("project_root", "~/dev")).expanduser()

    return Config(
        model=model,
        region=region,
        project_root=project_root,
    )

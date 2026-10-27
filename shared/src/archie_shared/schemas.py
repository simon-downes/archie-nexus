"""Application config schema — NexusConfig with sectioned YAML format.

Defines the typed configuration schema for archie-nexus. All sections are
optional; a missing or empty config file yields all defaults.

Config file location: <ARCHIE_HOME_DIR>/config.yaml
"""

from pathlib import Path
from typing import Annotated

import msgspec

from archie_shared.config import ConfigError, home_dir, load_config
from archie_shared.plugins.config import PluginConfig, validate_plugins
from archie_shared.plugins.errors import PluginManifestError
from archie_shared.plugins.manifest import load_manifests

# Sections replaced by ``plugins:`` (070 M4); ``archie config migrate`` converts them.
_REMOVED_SECTIONS = ("tools", "auth")


class GlobalConfig(msgspec.Struct, forbid_unknown_fields=True):
    """Global settings shared across all modes.

    Attributes:
        model: Active model key from the catalog.
        workspace_root: Base directory containing workspace projects.
        region: Session default AWS region (fallback for geo-inference models).
    """

    model: str = "bedrock-openai-gpt-5-6-luna"
    workspace_root: str = "~/dev"
    region: str = "eu-west-1"


class SubagentsConfig(msgspec.Struct, forbid_unknown_fields=True):
    """Limits specific to concurrent child-agent delegation."""

    max_concurrent: int = 5


class CompactionConfig(msgspec.Struct, forbid_unknown_fields=True):
    """Settings controlling transcript compaction."""

    model: str = "bedrock-openai-gpt-6-luna"
    percent: Annotated[int, msgspec.Meta(ge=50, le=95)] = 80
    tokens: Annotated[int, msgspec.Meta(ge=64_000)] | None = None
    cold_cost_threshold: Annotated[float, msgspec.Meta(ge=0)] = 0.5


class AgentConfig(msgspec.Struct, forbid_unknown_fields=True):
    """Agent-specific settings."""

    subagents: SubagentsConfig = msgspec.field(default_factory=SubagentsConfig)
    turn_cost_limit: float = 50.0
    compaction: CompactionConfig = msgspec.field(default_factory=CompactionConfig)


class CliConfig(msgspec.Struct, forbid_unknown_fields=True):
    """CLI-specific settings (placeholder for future fields)."""


class WebConfig(msgspec.Struct, forbid_unknown_fields=True):
    """Web UI-specific settings (placeholder for future fields)."""


class OrchestratorProfile(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """A single orchestrator target (host + port)."""

    host: str = "127.0.0.1"
    port: int = 7600
    public_url: str | None = None


class OrchestratorConfig(msgspec.Struct, forbid_unknown_fields=True):
    """Orchestrator configuration — profiles keyed by name.

    Named profiles are resolved by the CLI for multi-host addressing.
    The 'default' profile (or OrchestratorProfile() if absent) is used
    for commands that don't specify a profile.
    """

    profiles: dict[str, OrchestratorProfile] = msgspec.field(default_factory=dict)


def get_profile(config: OrchestratorConfig, name: str = "default") -> OrchestratorProfile:
    """Get a named profile.

    The implicit ``default`` profile falls back to hardcoded localhost defaults
    when no ``default`` profile is configured. Any *explicitly requested* profile
    name that does not exist raises KeyError — silently returning localhost for a
    typo'd name would be a footgun.
    """
    profile = config.profiles.get(name)
    if profile is not None:
        return profile
    if name == "default":
        return OrchestratorProfile()
    raise KeyError(name)


KIRO_EFFORT_LEVELS = ("none", "low", "medium", "high", "xhigh", "max")
KIRO_DEFAULT_MODELS = (
    "claude-opus-5.5",
    "claude-sonnet-5.5",
    "claude-haiku-5.5",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
)


class KiroCreditsConfig(msgspec.Struct, forbid_unknown_fields=True):
    """Kiro subscription credits used to price each Kiro turn."""

    included: Annotated[float, msgspec.Meta(ge=0)] = 2000.0
    resets_on: Annotated[int, msgspec.Meta(ge=1, le=31)] = 1
    rate_usd: Annotated[float, msgspec.Meta(ge=0)] = 0.02
    overage_rate_usd: Annotated[float, msgspec.Meta(ge=0)] = 0.04


class KiroConfig(msgspec.Struct, forbid_unknown_fields=True):
    """Settings for sessions started with ``archie start --kiro``."""

    default_model: str = "gpt-5.6-luna"
    models: tuple[str, ...] = KIRO_DEFAULT_MODELS
    effort: str | None = None
    credits: KiroCreditsConfig = msgspec.field(default_factory=KiroCreditsConfig)

    def __post_init__(self) -> None:
        if not self.models:
            raise ValueError("kiro.models must not be empty")
        if any(not isinstance(m, str) or not m.strip() for m in self.models):
            raise ValueError("kiro.models entries must be non-empty strings")
        if self.default_model not in self.models:
            raise ValueError(f"kiro.default_model '{self.default_model}' is not in kiro.models")
        if self.effort is not None and self.effort not in KIRO_EFFORT_LEVELS:
            raise ValueError(
                f"kiro.effort '{self.effort}' must be one of {', '.join(KIRO_EFFORT_LEVELS)}"
            )


MountConfig = str | tuple[str, str]


class NexusConfig(msgspec.Struct, forbid_unknown_fields=True):
    """Top-level application configuration.

    Maps to config.yaml with sections: global, cli, agent, web, orchestrator.
    The `global` key is renamed to `global_` in Python (reserved keyword).
    """

    global_: GlobalConfig = msgspec.field(default_factory=GlobalConfig, name="global")
    cli: CliConfig = msgspec.field(default_factory=CliConfig)
    agent: AgentConfig = msgspec.field(default_factory=AgentConfig)
    web: WebConfig = msgspec.field(default_factory=WebConfig)
    orchestrator: OrchestratorConfig = msgspec.field(default_factory=OrchestratorConfig)
    plugins: dict[str, PluginConfig] = msgspec.field(default_factory=dict)
    mounts: tuple[MountConfig, ...] = ()
    kiro: KiroConfig = msgspec.field(default_factory=KiroConfig)


def _reject_removed_sections(path: Path, data: object) -> None:
    if not isinstance(data, dict):
        return
    present = [name for name in _REMOVED_SECTIONS if name in data]
    if present:
        names = " and ".join(f"'{name}'" for name in present)
        noun = "sections were" if len(present) > 1 else "section was"
        raise ConfigError(
            f"{path}: the {names} {noun} replaced by 'plugins'. "
            "Run `archie config migrate` to convert this file."
        )


def _load(path: Path) -> NexusConfig:
    config = load_config(
        path, NexusConfig, raw_check=lambda data: _reject_removed_sections(path, data)
    )
    try:
        validate_plugins(config.plugins, load_manifests())
    except (ValueError, PluginManifestError) as e:
        raise ConfigError(f"Validation error in {path}: {e}") from None
    return config


def load_nexus_config(path: Path | None = None) -> NexusConfig:
    """Load the application config.

    Args:
        path: Explicit config file path. If given and missing, raises ConfigError.
            If None, resolves to <ARCHIE_HOME_DIR>/config.yaml. If that file
            doesn't exist, returns all defaults (no error).

    Returns:
        Validated NexusConfig instance.

    Raises:
        ConfigError: On explicit path missing, malformed YAML, validation failure,
            or a removed ``tools``/``auth`` section.
    """
    if path is not None:
        return _load(Path(path))

    default_path = home_dir() / "config.yaml"
    if not default_path.exists():
        return NexusConfig()

    return _load(default_path)


def expand_workspace_root(config: NexusConfig) -> Path:
    """Expand the workspace_root path (tilde expansion)."""
    return Path(config.global_.workspace_root).expanduser()

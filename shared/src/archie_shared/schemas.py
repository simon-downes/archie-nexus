"""Application config schema — NexusConfig with sectioned YAML format.

Defines the typed configuration schema for archie-nexus. All sections are
optional; a missing or empty config file yields all defaults.

Config file location: <ARCHIE_HOME_DIR>/config.yaml
"""

from pathlib import Path

import msgspec

from archie_shared.config import home_dir, load_config


class GlobalConfig(msgspec.Struct, forbid_unknown_fields=True):
    """Global settings shared across all modes.

    Attributes:
        model: Active model key from the catalog.
        workspace_root: Base directory containing workspace projects.
        region: Session default AWS region (fallback for geo-inference models).
    """

    model: str = "bedrock-claude-sonnet-4-6"
    workspace_root: str = "~/dev"
    region: str = "eu-west-1"


class SubagentsConfig(msgspec.Struct, forbid_unknown_fields=True):
    """Limits specific to concurrent child-agent delegation."""

    max_concurrent: int = 3


class AgentConfig(msgspec.Struct, forbid_unknown_fields=True):
    """Agent-specific settings."""

    subagents: SubagentsConfig = msgspec.field(default_factory=SubagentsConfig)


class CliConfig(msgspec.Struct, forbid_unknown_fields=True):
    """CLI-specific settings (placeholder for future fields)."""


class WebConfig(msgspec.Struct, forbid_unknown_fields=True):
    """Web UI-specific settings (placeholder for future fields)."""


class OrchestratorProfile(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """A single orchestrator target (host + port)."""

    host: str = "127.0.0.1"
    port: int = 7600


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


def load_nexus_config(path: Path | None = None) -> NexusConfig:
    """Load the application config.

    Args:
        path: Explicit config file path. If given and missing, raises ConfigError.
            If None, resolves to <ARCHIE_HOME_DIR>/config.yaml. If that file
            doesn't exist, returns all defaults (no error).

    Returns:
        Validated NexusConfig instance.

    Raises:
        ConfigError: On explicit path missing, malformed YAML, or validation failure.
    """
    if path is not None:
        return load_config(path, NexusConfig)

    default_path = home_dir() / "config.yaml"
    if not default_path.exists():
        return NexusConfig()

    return load_config(default_path, NexusConfig)


def expand_workspace_root(config: NexusConfig) -> Path:
    """Expand the workspace_root path (tilde expansion)."""
    return Path(config.global_.workspace_root).expanduser()

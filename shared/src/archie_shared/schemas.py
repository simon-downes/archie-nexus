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
        project_root: Base directory for project detection.
        region: Session default AWS region (fallback for geo-inference models).
    """

    model: str = "bedrock-claude-sonnet-4-6"
    project_root: str = "~/dev"
    region: str = "eu-west-1"


class AgentConfig(msgspec.Struct, forbid_unknown_fields=True):
    """Agent-specific settings (placeholder for future fields)."""


class CliConfig(msgspec.Struct, forbid_unknown_fields=True):
    """CLI-specific settings (placeholder for future fields)."""


class WebConfig(msgspec.Struct, forbid_unknown_fields=True):
    """Web UI-specific settings (placeholder for future fields)."""


class OrchestratorConfig(msgspec.Struct, forbid_unknown_fields=True):
    """Orchestrator-specific settings.

    Attributes:
        host: Bind/connect host for the orchestrator HTTP server.
        port: Bind/connect port for the orchestrator HTTP server.
    """

    host: str = "127.0.0.1"
    port: int = 7600


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


def expand_project_root(config: NexusConfig) -> Path:
    """Expand the project_root path (tilde expansion)."""
    return Path(config.global_.project_root).expanduser()

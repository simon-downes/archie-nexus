"""archie-shared — shared types, events, and config for archie-nexus."""

from archie_shared.commands import (
    ClientCommand,
    InterruptCommand,
    MessageCommand,
    SwitchModelCommand,
    decode_command,
    encode_command,
)
from archie_shared.config import ConfigError, home_dir, load_config, persona_dir
from archie_shared.models import (
    BedrockOpenAIProvider,
    BedrockProvider,
    CostConfig,
    ModelEntry,
    OllamaProvider,
    ProviderConfig,
    calculate_cost,
    get_model,
    load_models,
    provider_name,
)
from archie_shared.protocol import PROTOCOL_VERSION
from archie_shared.schemas import (
    AgentConfig,
    CliConfig,
    GlobalConfig,
    NexusConfig,
    OrchestratorConfig,
    OrchestratorProfile,
    WebConfig,
    expand_workspace_root,
    get_profile,
    load_nexus_config,
)
from archie_shared.types import ContentBlock, TextBlock, ToolResultBlock, ToolUseBlock
from archie_shared.version import __version__

__all__ = [
    "AgentConfig",
    "BedrockOpenAIProvider",
    "BedrockProvider",
    "ClientCommand",
    "CliConfig",
    "ConfigError",
    "ContentBlock",
    "CostConfig",
    "GlobalConfig",
    "InterruptCommand",
    "MessageCommand",
    "ModelEntry",
    "NexusConfig",
    "OllamaProvider",
    "OrchestratorConfig",
    "OrchestratorProfile",
    "PROTOCOL_VERSION",
    "ProviderConfig",
    "SwitchModelCommand",
    "TextBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "WebConfig",
    "__version__",
    "calculate_cost",
    "decode_command",
    "expand_workspace_root",
    "get_model",
    "get_profile",
    "home_dir",
    "load_config",
    "load_models",
    "load_nexus_config",
    "persona_dir",
    "provider_name",
    "encode_command",
]

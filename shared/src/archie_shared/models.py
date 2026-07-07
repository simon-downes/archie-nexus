"""Model catalog — code defaults with user file overrides.

Models are defined as msgspec Structs with typed cost and provider config.
Default models are defined in code; users can override or add models via
<ARCHIE_HOME_DIR>/models.yaml.

Key format: provider-model (e.g. bedrock-claude-sonnet-4-6).
The actual inference profile ID lives in provider.endpoint.
"""

import msgspec

from archie_shared.config import home_dir, load_config


class CostConfig(msgspec.Struct):
    """Per-million-token pricing in USD."""

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0


class ProviderConfig(msgspec.Struct):
    """LLM provider configuration.

    Attributes:
        name: Provider backend ("bedrock" or "ollama").
        endpoint: Bedrock inference profile ID, or ollama host:port.
        region: Bedrock region override. None means use session default.
    """

    name: str
    endpoint: str
    region: str | None = None


class ModelEntry(msgspec.Struct):
    """A model in the catalog.

    Attributes:
        name: Human-readable display name.
        context: Maximum input context window in tokens.
        provider: Provider configuration.
        can_cache: Whether this model supports Bedrock prompt caching.
        cost: Per-million-token pricing.
        max_output_tokens: Maximum tokens the model can generate.
        context_warning_threshold: Fraction (0-1) at which to warn about context usage.
    """

    name: str
    context: int
    provider: ProviderConfig
    can_cache: bool = False
    cost: CostConfig = msgspec.field(default_factory=CostConfig)
    max_output_tokens: int = 32_768
    context_warning_threshold: float = 0.8


# --- Default model catalog ---

DEFAULT_MODELS: dict[str, ModelEntry] = {
    "bedrock-claude-fable-5": ModelEntry(
        name="Claude Fable 5",
        context=1_000_000,
        provider=ProviderConfig(name="bedrock", endpoint="eu.anthropic.claude-fable-5"),
        can_cache=True,
        cost=CostConfig(input=11.0, output=55.0, cache_read=1.10, cache_write=13.75),
    ),
    "bedrock-claude-sonnet-4-6": ModelEntry(
        name="Claude Sonnet 4.6",
        context=1_000_000,
        provider=ProviderConfig(name="bedrock", endpoint="eu.anthropic.claude-sonnet-4-6"),
        can_cache=True,
        cost=CostConfig(input=3.3, output=16.5, cache_read=0.33, cache_write=4.125),
    ),
    "bedrock-claude-haiku-4-5": ModelEntry(
        name="Claude Haiku",
        context=200_000,
        provider=ProviderConfig(
            name="bedrock", endpoint="eu.anthropic.claude-haiku-4-5-20251001-v1:0"
        ),
        can_cache=True,
        cost=CostConfig(input=1.1, output=5.5, cache_read=0.11, cache_write=1.375),
    ),
    "bedrock-claude-opus-4-6": ModelEntry(
        name="Claude Opus 4.6",
        context=1_000_000,
        provider=ProviderConfig(name="bedrock", endpoint="eu.anthropic.claude-opus-4-6-v1"),
        can_cache=True,
        cost=CostConfig(input=5.5, output=27.5, cache_read=0.55, cache_write=6.875),
    ),
    "bedrock-claude-opus-4-8": ModelEntry(
        name="Claude Opus 4.8",
        context=1_000_000,
        provider=ProviderConfig(name="bedrock", endpoint="eu.anthropic.claude-opus-4-8"),
        can_cache=True,
        cost=CostConfig(input=5.5, output=27.5, cache_read=0.55, cache_write=6.875),
    ),
    # --- Non-Anthropic Bedrock models ---
    "bedrock-glm-5": ModelEntry(
        name="GLM 5",
        context=200_000,
        provider=ProviderConfig(name="bedrock", endpoint="zai.glm-5", region="eu-west-2"),
        max_output_tokens=128_000,
        cost=CostConfig(input=1.55, output=4.96),
    ),
    "bedrock-qwen3-coder-next": ModelEntry(
        name="Qwen3 Coder Next",
        context=256_000,
        provider=ProviderConfig(
            name="bedrock", endpoint="qwen.qwen3-coder-next", region="eu-west-2"
        ),
        max_output_tokens=16_000,
        cost=CostConfig(input=0.60, output=1.44),
    ),
    "bedrock-qwen3-coder-480b": ModelEntry(
        name="Qwen3 Coder 480B A35B",
        context=128_000,
        provider=ProviderConfig(
            name="bedrock", endpoint="qwen.qwen3-coder-480b-a35b-v1:0", region="eu-west-2"
        ),
        max_output_tokens=16_000,
        cost=CostConfig(input=1.225, output=4.8825),
    ),
    "bedrock-kimi-k2-5": ModelEntry(
        name="Kimi K2.5",
        context=256_000,
        provider=ProviderConfig(
            name="bedrock", endpoint="moonshotai.kimi-k2.5", region="eu-west-2"
        ),
        max_output_tokens=16_000,
        cost=CostConfig(input=0.72, output=3.60),
    ),
    # --- Ollama local models ---
    "ollama-qwen3-6-35b": ModelEntry(
        name="Qwen 3.6 35B",
        context=128_000,
        provider=ProviderConfig(name="ollama", endpoint="localhost:11434"),
        max_output_tokens=16_000,
    ),
    "ollama-gemma4-31b": ModelEntry(
        name="Gemma 4 31B",
        context=128_000,
        provider=ProviderConfig(name="ollama", endpoint="localhost:11434"),
        max_output_tokens=16_000,
    ),
}


def load_models(overrides_path=None) -> dict[str, ModelEntry]:
    """Load the model catalog: code defaults + optional user overrides.

    Args:
        overrides_path: Path to user overrides YAML file. If None, uses
            <ARCHIE_HOME_DIR>/models.yaml. Missing file is not an error
            (returns defaults only).

    Returns:
        Merged model catalog dict.
    """
    catalog = dict(DEFAULT_MODELS)

    if overrides_path is None:
        overrides_path = home_dir() / "models.yaml"

    if not overrides_path.exists():
        return catalog

    overrides = load_config(overrides_path, dict[str, ModelEntry])
    catalog.update(overrides)
    return catalog


def get_model(catalog: dict[str, ModelEntry], key: str) -> ModelEntry:
    """Look up a model by key in the catalog.

    Raises:
        KeyError: If the key is not found, with available keys listed.
    """
    if key not in catalog:
        available = ", ".join(sorted(catalog.keys()))
        raise KeyError(f"Unknown model '{key}'. Available: {available}")
    return catalog[key]


def calculate_cost(
    cost: CostConfig,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Calculate USD cost from CostConfig rates (per million tokens)."""
    return (
        input_tokens * cost.input / 1_000_000
        + output_tokens * cost.output / 1_000_000
        + cache_read_tokens * cost.cache_read / 1_000_000
        + cache_write_tokens * cost.cache_write / 1_000_000
    )

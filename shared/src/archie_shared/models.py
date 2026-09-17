"""Model catalog and billable token accounting."""

import msgspec

from archie_shared.config import home_dir, load_config


class CostConfig(msgspec.Struct):
    """Per-million-token pricing in USD."""

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0


class BedrockProvider(msgspec.Struct, tag="bedrock"):
    model_id: str
    region: str | None = None


class BedrockOpenAIProvider(msgspec.Struct, tag="bedrock-openai"):
    model_id: str
    region: str | None = None


class OllamaProvider(msgspec.Struct, tag="ollama"):
    model_id: str
    endpoint: str = "host.docker.internal:11434"


ProviderConfig = BedrockProvider | BedrockOpenAIProvider | OllamaProvider


def provider_name(provider: ProviderConfig) -> str:
    """Return the provider backend name."""
    return provider.__struct_config__.tag


class ModelEntry(msgspec.Struct):
    """A model in the catalog."""

    name: str
    context: int
    provider: ProviderConfig
    can_cache: bool = False
    cost: CostConfig = msgspec.field(default_factory=CostConfig)
    max_output_tokens: int = 32_768
    context_warning_threshold: float = 0.8


DEFAULT_MODELS: dict[str, ModelEntry] = {
    "bedrock-claude-fable-5": ModelEntry(
        name="Claude Fable 5",
        context=1_000_000,
        provider=BedrockProvider(model_id="eu.anthropic.claude-fable-5"),
        can_cache=True,
        cost=CostConfig(input=11.0, output=55.0, cache_read=1.10, cache_write=13.75),
    ),
    "bedrock-claude-sonnet-4-6": ModelEntry(
        name="Claude Sonnet 4.6",
        context=1_000_000,
        provider=BedrockProvider(model_id="eu.anthropic.claude-sonnet-4-6"),
        can_cache=True,
        cost=CostConfig(input=3.3, output=16.5, cache_read=0.33, cache_write=4.125),
    ),
    "bedrock-claude-haiku-4-5": ModelEntry(
        name="Claude Haiku",
        context=200_000,
        provider=BedrockProvider(model_id="eu.anthropic.claude-haiku-4-5-20251001-v1:0"),
        can_cache=True,
        cost=CostConfig(input=1.1, output=5.5, cache_read=0.11, cache_write=1.375),
    ),
    "bedrock-claude-opus-4-6": ModelEntry(
        name="Claude Opus 4.6",
        context=1_000_000,
        provider=BedrockProvider(model_id="eu.anthropic.claude-opus-4-6-v1"),
        can_cache=True,
        cost=CostConfig(input=5.5, output=27.5, cache_read=0.55, cache_write=6.875),
    ),
    "bedrock-claude-opus-4-8": ModelEntry(
        name="Claude Opus 4.8",
        context=1_000_000,
        provider=BedrockProvider(model_id="eu.anthropic.claude-opus-4-8"),
        can_cache=True,
        cost=CostConfig(input=5.5, output=27.5, cache_read=0.55, cache_write=6.875),
    ),
    "bedrock-openai-gpt-5-6-luna": ModelEntry(
        name="GPT-5.6 Luna",
        context=1_000_000,
        provider=BedrockOpenAIProvider(model_id="openai.gpt-5.6-luna", region="us-east-1"),
        can_cache=True,
        cost=CostConfig(input=0.40, output=1.80, cache_read=0.04, cache_write=0.5),
    ),
    "bedrock-openai-gpt-5-6-terra": ModelEntry(
        name="GPT-5.6 Terra",
        context=1_000_000,
        provider=BedrockOpenAIProvider(model_id="openai.gpt-5.6-terra", region="us-east-1"),
        can_cache=True,
        cost=CostConfig(input=4.40, output=19.80, cache_read=0.44, cache_write=5.50),
    ),
    "bedrock-openai-gpt-5-6-sol": ModelEntry(
        name="GPT-5.6 Sol",
        context=1_000_000,
        provider=BedrockOpenAIProvider(model_id="openai.gpt-5.6-sol", region="us-east-1"),
        can_cache=True,
        cost=CostConfig(input=4.40, output=33.00, cache_read=0.88, cache_write=11.00),
    ),
    "bedrock-glm-5": ModelEntry(
        name="GLM 5",
        context=200_000,
        provider=BedrockProvider(model_id="zai.glm-5", region="eu-west-2"),
        max_output_tokens=128_000,
        cost=CostConfig(input=1.55, output=4.96),
    ),
    "bedrock-qwen3-coder-next": ModelEntry(
        name="Qwen3 Coder Next",
        context=256_000,
        provider=BedrockProvider(model_id="qwen.qwen3-coder-next", region="eu-west-2"),
        max_output_tokens=16_000,
        cost=CostConfig(input=0.60, output=1.44),
    ),
    "bedrock-qwen3-coder-480b": ModelEntry(
        name="Qwen3 Coder 480B A35B",
        context=128_000,
        provider=BedrockProvider(model_id="qwen.qwen3-coder-480b-a35b-v1:0", region="eu-west-2"),
        max_output_tokens=16_000,
        cost=CostConfig(input=1.225, output=4.8825),
    ),
    "bedrock-kimi-k2-5": ModelEntry(
        name="Kimi K2.5",
        context=256_000,
        provider=BedrockProvider(model_id="moonshotai.kimi-k2.5", region="eu-west-2"),
        max_output_tokens=16_000,
        cost=CostConfig(input=0.72, output=3.60),
    ),
    "ollama-qwen3-6-35b": ModelEntry(
        name="Qwen 3.6 35B",
        context=128_000,
        provider=OllamaProvider(model_id="qwen3.6:35b"),
        max_output_tokens=16_000,
    ),
    "ollama-gemma4-31b": ModelEntry(
        name="Gemma 4 31B",
        context=128_000,
        provider=OllamaProvider(model_id="gemma4:31b"),
        max_output_tokens=16_000,
    ),
}


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def sanitize_billable_usage(
    input_tokens: object,
    output_tokens: object,
    cache_read_tokens: object = 0,
    cache_write_tokens: object = 0,
) -> tuple[int, int, int, int]:
    """Sanitize already-normalized billable token categories."""
    input_count = _integer(input_tokens)
    output_count = _integer(output_tokens)
    cache_read = _integer(cache_read_tokens)
    cache_write = _integer(cache_write_tokens)
    return input_count, output_count, cache_read, cache_write


def normalize_responses_usage(
    raw_input_tokens: object,
    output_tokens: object,
    cached_tokens: object = 0,
    cache_write_tokens: object = 0,
) -> tuple[int, int, int, int]:
    """Convert Responses raw total input into billable categories."""
    raw_input, output, cached, written = sanitize_billable_usage(
        raw_input_tokens, output_tokens, cached_tokens, cache_write_tokens
    )
    if cached + written > raw_input:
        return raw_input, output, 0, 0
    return raw_input - cached - written, output, cached, written


def load_models(overrides_path=None) -> dict[str, ModelEntry]:
    """Load code defaults merged with optional user overrides."""
    catalog = dict(DEFAULT_MODELS)
    if overrides_path is None:
        overrides_path = home_dir() / "models.yaml"
    if not overrides_path.exists():
        return catalog
    catalog.update(load_config(overrides_path, dict[str, ModelEntry]))
    return catalog


def get_model(catalog: dict[str, ModelEntry], key: str) -> ModelEntry:
    """Look up a model by catalog key."""
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
    """Calculate cost from four billable token categories."""
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens = sanitize_billable_usage(
        input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
    )
    return (
        input_tokens * cost.input / 1_000_000
        + output_tokens * cost.output / 1_000_000
        + cache_read_tokens * cost.cache_read / 1_000_000
        + cache_write_tokens * cost.cache_write / 1_000_000
    )

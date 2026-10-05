"""Model catalog and billable token accounting."""

import math

import msgspec


class Rate(msgspec.Struct):
    """Per-million-token pricing for one request-level rate."""

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    criteria: tuple[str, str, int] | None = None


class CostConfig(msgspec.Struct):
    """Request-level pricing rates in USD per million tokens."""

    rates: list[Rate] = msgspec.field(default_factory=lambda: [Rate()])

    def validate(self) -> None:
        """Validate the supported rate configuration."""
        if not self.rates:
            raise ValueError("CostConfig.rates must contain at least one rate")
        thresholds: set[int] = set()
        fallback_seen = False
        for rate in self.rates:
            for name in ("input", "output", "cache_read", "cache_write"):
                value = getattr(rate, name)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise ValueError(f"Rate.{name} must be a finite non-negative number")
            criteria = rate.criteria
            if criteria is None:
                if fallback_seen:
                    raise ValueError("CostConfig cannot contain duplicate fallback rates")
                fallback_seen = True
                continue
            if not isinstance(criteria, tuple) or len(criteria) != 3:
                raise ValueError("Rate.criteria must be (field, operation, threshold)")
            field, operation, threshold = criteria
            if field != "total_input":
                raise ValueError(f"Unsupported rate criteria field: {field!r}")
            if operation != "min":
                raise ValueError(f"Unsupported rate criteria operation: {operation!r}")
            if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
                raise ValueError("Rate criteria threshold must be a non-negative integer")
            if threshold in thresholds:
                raise ValueError(f"Duplicate rate criteria threshold: {threshold}")
            thresholds.add(threshold)

    def get_rate(
        self,
        input_tokens: object,
        output_tokens: object,
        cache_read_tokens: object = 0,
        cache_write_tokens: object = 0,
    ) -> Rate:
        """Select the highest matching request-level rate."""
        self.validate()
        input_count, _, cache_read, cache_write = sanitize_billable_usage(
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
        )
        total_input = input_count + cache_read + cache_write
        fallback: Rate | None = None
        selected: Rate | None = None
        selected_threshold = -1
        for rate in self.rates:
            if rate.criteria is None:
                fallback = rate
            elif rate.criteria[2] <= total_input and rate.criteria[2] > selected_threshold:
                selected = rate
                selected_threshold = rate.criteria[2]
        if selected is not None:
            return selected
        if fallback is not None:
            return fallback
        raise ValueError(f"No rate matches total_input={total_input} and no fallback rate exists")

    def get_cost(
        self,
        input_tokens: object,
        output_tokens: object,
        cache_read_tokens: object = 0,
        cache_write_tokens: object = 0,
    ) -> float:
        """Calculate USD using the selected rate and four billable categories."""
        input_count, output_count, cache_read, cache_write = sanitize_billable_usage(
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
        )
        rate = self.get_rate(input_count, output_count, cache_read, cache_write)
        return (
            input_count * rate.input / 1_000_000
            + output_count * rate.output / 1_000_000
            + cache_read * rate.cache_read / 1_000_000
            + cache_write * rate.cache_write / 1_000_000
        )


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
    # Model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-fable-5-1.html
    # Requires AWS Bedrock Data Retention API opt-in with mode ``aws_review``.
    "bedrock-claude-fable-5-1": ModelEntry(
        name="Claude Fable 5.1",
        context=1_000_000,
        provider=BedrockProvider(model_id="global.anthropic.claude-fable-5-1"),
        can_cache=True,
        max_output_tokens=128_000,
        # Global price assumed at 10% below the published Geo/In-region rate (11/55/0.275/13.75);
        # AWS does not yet list a global row for Fable 5.1. Pending confirmation.
        cost=CostConfig(rates=[Rate(input=10.0, output=50.0, cache_read=0.25, cache_write=12.50)]),
    ),
    # Model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-5-5.html
    "bedrock-claude-sonnet-5-5": ModelEntry(
        name="Claude Sonnet 5.5",
        context=1_000_000,
        provider=BedrockProvider(model_id="global.anthropic.claude-sonnet-5-5"),
        can_cache=True,
        max_output_tokens=128_000,
        cost=CostConfig(rates=[Rate(input=2.0, output=10.0, cache_read=0.20, cache_write=2.50)]),
    ),
    # Model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-4-6.html
    "bedrock-claude-sonnet-4-6": ModelEntry(
        name="Claude Sonnet 4.6",
        context=1_000_000,
        provider=BedrockProvider(model_id="global.anthropic.claude-sonnet-4-6"),
        can_cache=True,
        cost=CostConfig(rates=[Rate(input=3, output=15, cache_read=0.3, cache_write=3.75)]),
    ),
    # Model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-4-5.html
    "bedrock-claude-haiku-4-5": ModelEntry(
        name="Claude Haiku",
        context=200_000,
        provider=BedrockProvider(model_id="global.anthropic.claude-haiku-4-5-20251001-v1:0"),
        can_cache=True,
        cost=CostConfig(rates=[Rate(input=1, output=5, cache_read=0.1, cache_write=1.25)]),
    ),
    # Model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-opus-5-5.html
    "bedrock-claude-opus-5-5": ModelEntry(
        name="Claude Opus 5.5",
        context=1_000_000,
        provider=BedrockProvider(model_id="global.anthropic.claude-opus-5-5"),
        can_cache=True,
        max_output_tokens=128_000,
        cost=CostConfig(rates=[Rate(input=4.0, output=20.0, cache_read=0.20, cache_write=5.0)]),
    ),
    # Model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-56-luna.html
    "bedrock-openai-gpt-5-6-luna": ModelEntry(
        name="GPT-5.6 Luna",
        context=1_000_000,
        provider=BedrockOpenAIProvider(model_id="global.openai.gpt-5.6-luna", region="us-east-1"),
        can_cache=True,
        cost=CostConfig(
            rates=[
                Rate(input=0.20, output=1.20, cache_read=0.02, cache_write=0.25, criteria=("total_input", "min", 0)),
                Rate(input=0.40, output=1.80, cache_read=0.04, cache_write=0.50, criteria=("total_input", "min", 272_001)),
            ]
        ),
    ),
    # Model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-luna.html
    "bedrock-openai-gpt-6-luna": ModelEntry(
        name="GPT-6 Luna",
        context=1_000_000,
        provider=BedrockOpenAIProvider(model_id="global.openai.gpt-6-luna", region="us-east-1"),
        can_cache=True,
        cost=CostConfig(
            rates=[
                Rate(input=0.10, output=0.50, cache_read=0.01, cache_write=0.125, criteria=("total_input", "min", 0)),
                Rate(input=0.20, output=0.75, cache_read=0.02, cache_write=0.25, criteria=("total_input", "min", 272_001)),
            ]
        ),
    ),

    # Model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-1-sol.html
    "bedrock-openai-gpt-6-1-sol": ModelEntry(
        name="GPT-6.1 Sol",
        context=1_000_000,
        provider=BedrockOpenAIProvider(model_id="global.openai.gpt-6.1-sol", region="us-east-1"),
        can_cache=True,
        max_output_tokens=131_072,
        cost=CostConfig(
            rates=[
                Rate(input=2.00, output=10.00, cache_read=0.10, cache_write=2.50, criteria=("total_input", "min", 0)),
                Rate(input=4.00, output=15.00, cache_read=0.20, cache_write=5.00, criteria=("total_input", "min", 272_001)),
            ]
        ),
    ),
    # Model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-56-terra.html
    # GPT-5.6 Terra is the latest Terra on Bedrock; GPT-6 Terra has no model card or pricing.
    "bedrock-openai-gpt-5-6-terra": ModelEntry(
        name="GPT-5.6 Terra",
        context=1_050_000,
        provider=BedrockOpenAIProvider(model_id="global.openai.gpt-5.6-terra", region="us-east-1"),
        can_cache=True,
        max_output_tokens=128_000,
        cost=CostConfig(
            rates=[
                Rate(input=2.00, output=12.00, cache_read=0.20, cache_write=2.50, criteria=("total_input", "min", 0)),
                Rate(input=4.00, output=18.00, cache_read=0.40, cache_write=5.00, criteria=("total_input", "min", 272_001)),
            ]
        ),
    ),
    # Model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-zai-glm-5.html
    "bedrock-glm-5": ModelEntry(
        name="GLM 5",
        context=200_000,
        provider=BedrockProvider(model_id="zai.glm-5", region="eu-west-2"),
        max_output_tokens=128_000,
        cost=CostConfig(rates=[Rate(input=1.55, output=4.96)]),
    ),
    # Model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-qwen-qwen3-coder-next.html
    "bedrock-qwen3-coder-next": ModelEntry(
        name="Qwen3 Coder Next",
        context=256_000,
        provider=BedrockProvider(model_id="qwen.qwen3-coder-next", region="eu-west-2"),
        max_output_tokens=16_000,
        cost=CostConfig(rates=[Rate(input=0.78, output=1.86)]),
    ),
    # Model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-qwen-qwen3-coder-480b-a35b-instruct.html
    "bedrock-qwen3-coder-480b": ModelEntry(
        name="Qwen3 Coder 480B A35B",
        context=128_000,
        provider=BedrockProvider(model_id="qwen.qwen3-coder-480b-a35b-v1:0", region="eu-west-2"),
        max_output_tokens=16_000,
        cost=CostConfig(rates=[Rate(input=1.225, output=4.8825)]),
    ),
    # Model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-moonshot-ai-kimi-k3.html
    "bedrock-kimi-k3": ModelEntry(
        name="Kimi K3",
        context=1_000_000,
        provider=BedrockProvider(model_id="global.moonshotai.kimi-k3"),
        can_cache=True,
        max_output_tokens=16_000,
        cost=CostConfig(rates=[Rate(input=3.0, output=15.0, cache_read=0.30, cache_write=3.75)]),
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
    """Return the validated built-in model catalog.

    ``overrides_path`` remains accepted for caller compatibility but is ignored.
    """
    del overrides_path
    catalog = dict(DEFAULT_MODELS)
    for model in catalog.values():
        model.cost.validate()
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
    return cost.get_cost(input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)

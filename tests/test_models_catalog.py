"""Tests for the model catalog and billable accounting."""

import pytest
from archie_shared.models import (
    DEFAULT_MODELS,
    BedrockOpenAIProvider,
    BedrockProvider,
    CostConfig,
    OllamaProvider,
    Rate,
    calculate_cost,
    get_model,
    load_models,
    normalize_responses_usage,
    sanitize_billable_usage,
)


def test_default_models_not_empty():
    assert len(DEFAULT_MODELS) > 0


def test_default_models_all_have_required_fields():
    for key, model in DEFAULT_MODELS.items():
        assert model.name, f"{key} missing name"
        assert model.context > 0, f"{key} has invalid context"
        assert isinstance(model.provider, (BedrockProvider, BedrockOpenAIProvider, OllamaProvider))
        assert model.provider.model_id


def test_default_model_key_format():
    for key in DEFAULT_MODELS:
        assert key.startswith("bedrock-") or key.startswith("ollama-")


def test_gpt_56_luna_catalog_entry():
    model = DEFAULT_MODELS["bedrock-openai-gpt-5-6-luna"]
    assert isinstance(model.provider, BedrockOpenAIProvider)
    assert model.provider.model_id == "global.openai.gpt-5.6-luna"
    assert model.provider.region == "us-east-1"
    assert model.context == 1_000_000
    assert model.can_cache
    assert model.cost.rates == [
        Rate(input=0.20, output=1.20, cache_read=0.02, cache_write=0.25, criteria=("total_input", "min", 0)),
        Rate(input=0.40, output=1.80, cache_read=0.04, cache_write=0.50, criteria=("total_input", "min", 272_001)),
    ]


def test_load_models_returns_built_in_catalog_and_ignores_models_yaml(tmp_path):
    models_file = tmp_path / "models.yaml"
    models_file.write_text("custom:\n  name: Should be ignored\n")
    assert load_models(overrides_path=models_file) == DEFAULT_MODELS


def test_replacement_catalog_membership_and_metadata():
    expected = {
        "bedrock-claude-opus-5-5",
        "bedrock-claude-sonnet-5-5",
        "bedrock-claude-fable-5-1",
        "bedrock-kimi-k3",
        "bedrock-openai-gpt-6-1-sol",
        "bedrock-openai-gpt-5-6-terra",
        "bedrock-openai-gpt-5-6-luna",
    }
    removed = {
        "bedrock-claude-fable-5",
        "bedrock-claude-opus-4-6",
        "bedrock-claude-opus-4-8",
        "bedrock-kimi-k2-5",
        "bedrock-openai-gpt-5-6-sol",
        "bedrock-openai-gpt-6-sol",
        "bedrock-openai-gpt-6-terra",
    }
    assert expected <= DEFAULT_MODELS.keys()
    assert not removed.intersection(DEFAULT_MODELS)

    assert DEFAULT_MODELS["bedrock-openai-gpt-6-1-sol"].can_cache is True
    assert DEFAULT_MODELS["bedrock-openai-gpt-6-1-sol"].max_output_tokens == 131_072
    assert DEFAULT_MODELS["bedrock-openai-gpt-5-6-terra"].context == 1_050_000
    assert DEFAULT_MODELS["bedrock-openai-gpt-5-6-terra"].max_output_tokens == 128_000
    assert DEFAULT_MODELS["bedrock-kimi-k3"].max_output_tokens == 16_000
    assert all(DEFAULT_MODELS[key].max_output_tokens == 128_000 for key in (
        "bedrock-claude-opus-5-5", "bedrock-claude-sonnet-5-5", "bedrock-claude-fable-5-1"
    ))


def test_provider_ids_use_best_available_inference_scope():
    """Global profiles wherever AWS offers one; in-Region only where it does not."""
    expected = {
        "bedrock-claude-opus-5-5": ("global.anthropic.claude-opus-5-5", None),
        "bedrock-claude-sonnet-5-5": ("global.anthropic.claude-sonnet-5-5", None),
        "bedrock-claude-fable-5-1": ("global.anthropic.claude-fable-5-1", None),
        "bedrock-claude-sonnet-4-6": ("global.anthropic.claude-sonnet-4-6", None),
        "bedrock-claude-haiku-4-5": ("global.anthropic.claude-haiku-4-5-20251001-v1:0", None),
        "bedrock-kimi-k3": ("global.moonshotai.kimi-k3", None),
        "bedrock-openai-gpt-5-6-luna": ("global.openai.gpt-5.6-luna", "us-east-1"),
        "bedrock-openai-gpt-6-luna": ("global.openai.gpt-6-luna", "us-east-1"),
        "bedrock-openai-gpt-6-1-sol": ("global.openai.gpt-6.1-sol", "us-east-1"),
        "bedrock-openai-gpt-5-6-terra": ("global.openai.gpt-5.6-terra", "us-east-1"),
        # No geo or global profiles exist for these models.
        "bedrock-glm-5": ("zai.glm-5", "eu-west-2"),
        "bedrock-qwen3-coder-next": ("qwen.qwen3-coder-next", "eu-west-2"),
        "bedrock-qwen3-coder-480b": ("qwen.qwen3-coder-480b-a35b-v1:0", "eu-west-2"),
    }
    actual = {
        key: (model.provider.model_id, model.provider.region)
        for key, model in DEFAULT_MODELS.items()
        if isinstance(model.provider, (BedrockProvider, BedrockOpenAIProvider))
    }
    assert actual == expected


def _tiered(short, long):
    return [
        Rate(*short, criteria=("total_input", "min", 0)),
        Rate(*long, criteria=("total_input", "min", 272_001)),
    ]


def test_openai_tiered_global_rates():
    # (input, output, cache_read, cache_write) — Global CRIS, from the Bedrock model cards.
    expected = {
        "bedrock-openai-gpt-5-6-luna": _tiered((0.20, 1.20, 0.02, 0.25), (0.40, 1.80, 0.04, 0.50)),
        "bedrock-openai-gpt-6-luna": _tiered((0.10, 0.50, 0.01, 0.125), (0.20, 0.75, 0.02, 0.25)),
        "bedrock-openai-gpt-6-1-sol": _tiered((2.00, 10.00, 0.10, 2.50), (4.00, 15.00, 0.20, 5.00)),
        "bedrock-openai-gpt-5-6-terra": _tiered((2.00, 12.00, 0.20, 2.50), (4.00, 18.00, 0.40, 5.00)),
    }
    for key, rates in expected.items():
        assert DEFAULT_MODELS[key].cost.rates == rates, key


def test_gpt_6_1_sol_tier_boundary_applies_to_all_categories():
    cost = DEFAULT_MODELS["bedrock-openai-gpt-6-1-sol"].cost
    assert cost.get_rate(272_000, 0).input == 2.00
    assert cost.get_rate(272_001, 0).input == 4.00
    # Cache tokens count toward the threshold.
    assert cost.get_rate(200_000, 0, 72_000, 0).input == 2.00
    assert cost.get_rate(200_000, 0, 72_000, 1).input == 4.00
    # Long rate applies to every category once the threshold is crossed.
    assert calculate_cost(cost, 200_000, 100_000, 72_000, 1) == pytest.approx(
        200_000 * 4.00 / 1e6 + 100_000 * 15.00 / 1e6 + 72_000 * 0.20 / 1e6 + 1 * 5.00 / 1e6
    )


def test_single_rate_global_prices():
    expected = {
        "bedrock-claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
        "bedrock-claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.50),
        # Assumed 10% below published Geo/In-region; pending AWS global row.
        "bedrock-claude-fable-5-1": (10.0, 50.0, 0.25, 12.50),
        "bedrock-claude-sonnet-4-6": (3.0, 15.0, 0.30, 3.75),
        "bedrock-claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25),
        "bedrock-kimi-k3": (3.0, 15.0, 0.30, 3.75),
    }
    for key, values in expected.items():
        rates = DEFAULT_MODELS[key].cost.rates
        assert len(rates) == 1 and rates[0].criteria is None, key
        assert (rates[0].input, rates[0].output, rates[0].cache_read, rates[0].cache_write) == values, key


def test_in_region_eu_west_2_prices():
    expected = {
        "bedrock-glm-5": (1.55, 4.96),
        "bedrock-qwen3-coder-next": (0.78, 1.86),
        "bedrock-qwen3-coder-480b": (1.225, 4.8825),
    }
    for key, (inp, out) in expected.items():
        rate = DEFAULT_MODELS[key].cost.rates[0]
        assert (rate.input, rate.output) == (inp, out), key


def test_every_bedrock_model_is_priced():
    for key, model in DEFAULT_MODELS.items():
        if isinstance(model.provider, (BedrockProvider, BedrockOpenAIProvider)):
            assert all(rate.input > 0 and rate.output > 0 for rate in model.cost.rates), key


def test_get_model_found():
    model = get_model(DEFAULT_MODELS, "bedrock-claude-sonnet-4-6")
    assert model.name == "Claude Sonnet 4.6"
    assert model.context == 1_000_000


def test_get_model_not_found():
    with pytest.raises(KeyError, match="Unknown model 'nonexistent'"):
        get_model(DEFAULT_MODELS, "nonexistent")
    with pytest.raises(KeyError, match="Available:"):
        get_model(DEFAULT_MODELS, "nonexistent")


def test_rate_selection_includes_cache_tokens_and_uses_highest_threshold():
    cost = CostConfig(
        rates=[
            Rate(input=1.0, output=2.0, criteria=("total_input", "min", 0)),
            Rate(input=3.0, output=4.0, cache_read=5.0, criteria=("total_input", "min", 272_001)),
        ]
    )
    assert cost.get_rate(200_000, 0, 72_000).input == 1.0
    assert cost.get_rate(200_000, 0, 72_001).input == 3.0


def test_rate_configuration_rejects_invalid_values():
    with pytest.raises(ValueError, match="at least one"):
        CostConfig(rates=[]).get_rate(0, 0)
    with pytest.raises(ValueError, match="Unsupported rate criteria field"):
        CostConfig(rates=[Rate(criteria=("output", "min", 0))]).get_rate(0, 0)
    with pytest.raises(ValueError, match="Duplicate"):
        CostConfig(rates=[Rate(criteria=("total_input", "min", 0)), Rate(criteria=("total_input", "min", 0))]).get_rate(0, 0)
    with pytest.raises(ValueError, match="no fallback"):
        CostConfig(rates=[Rate(criteria=("total_input", "min", 1))]).get_rate(0, 0)


def test_calculate_cost_basic():
    cost = CostConfig(rates=[Rate(input=3.0, output=15.0)])
    assert calculate_cost(cost, input_tokens=1_000_000, output_tokens=500_000) == pytest.approx(
        10.5
    )


def test_calculate_cost_with_billable_cache_categories():
    cost = CostConfig(rates=[Rate(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75)])
    result = calculate_cost(
        cost,
        input_tokens=100_000,
        output_tokens=50_000,
        cache_read_tokens=20_000,
        cache_write_tokens=10_000,
    )
    expected = (100_000 * 3.0 + 50_000 * 15.0 + 20_000 * 0.3 + 10_000 * 3.75) / 1_000_000
    assert result == pytest.approx(expected)


def test_invalid_cache_categories_are_zeroed():
    assert sanitize_billable_usage(0, 20, 90, 20) == (0, 20, 90, 20)
    assert normalize_responses_usage(100, 20, 30, 5) == (65, 20, 30, 5)
    assert normalize_responses_usage(20, 5, 30, 5) == (20, 5, 0, 0)


def test_calculate_cost_zero():
    assert calculate_cost(CostConfig(rates=[Rate(input=5.0, output=10.0)]), 0, 0) == 0.0


def test_calculate_cost_uses_selected_rate_for_all_categories():
    cost = CostConfig(
        rates=[
            Rate(input=1.0, output=2.0, cache_read=3.0, cache_write=4.0, criteria=("total_input", "min", 0)),
            Rate(input=10.0, output=20.0, cache_read=30.0, cache_write=40.0, criteria=("total_input", "min", 272_001)),
        ]
    )
    assert calculate_cost(cost, 200_000, 100_000, 72_001, 0) == pytest.approx(6.16003)


def test_calculate_cost_free_model():
    assert calculate_cost(CostConfig(), input_tokens=1_000_000, output_tokens=500_000) == 0.0


def test_usage_sanitization_zeroes_negative_and_non_integer_values():
    assert sanitize_billable_usage(-1, 2.5, "3", True) == (0, 0, 0, 0)
    assert normalize_responses_usage(-1, "bad", -3, 4) == (0, 0, 0, 0)

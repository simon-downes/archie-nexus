"""Tests for the model catalog and billable accounting."""

import pytest
from archie_shared.models import (
    DEFAULT_MODELS,
    BedrockOpenAIProvider,
    BedrockProvider,
    CostConfig,
    OllamaProvider,
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
    assert model.provider.model_id == "openai.gpt-5.6-luna"
    assert model.provider.region == "us-east-1"
    assert model.context == 1_000_000
    assert model.can_cache
    assert model.cost.input == pytest.approx(0.20)
    assert model.cost.output == pytest.approx(1.20)
    assert model.cost.cache_read == pytest.approx(0.02)
    assert model.cost.cache_write == pytest.approx(0.25)


def test_load_models_defaults_only(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    assert load_models() == DEFAULT_MODELS


def test_load_models_with_override(tmp_path):
    models_file = tmp_path / "models.yaml"
    models_file.write_text(
        "bedrock-claude-sonnet-4-6:\n  name: Custom Sonnet\n  context: 500000\n  provider:\n    type: bedrock\n    model_id: eu.anthropic.claude-sonnet-4-6\n  can_cache: true\n"
    )
    assert (
        load_models(overrides_path=models_file)["bedrock-claude-sonnet-4-6"].name == "Custom Sonnet"
    )


def test_load_models_adds_new_key(tmp_path):
    models_file = tmp_path / "models.yaml"
    models_file.write_text(
        "bedrock-custom-model:\n  name: Custom Model\n  context: 64000\n  provider:\n    type: bedrock\n    model_id: custom.model-id\n    region: us-east-1\n"
    )
    assert "bedrock-custom-model" in load_models(overrides_path=models_file)


def test_load_models_missing_file_no_error(tmp_path):
    assert load_models(overrides_path=tmp_path / "nonexistent.yaml") == DEFAULT_MODELS


def test_load_models_explicit_path(tmp_path):
    custom_path = tmp_path / "custom_models.yaml"
    custom_path.write_text(
        "ollama-custom:\n  name: Custom Ollama\n  context: 32000\n  provider:\n    type: ollama\n    model_id: custom:latest\n    endpoint: localhost:11434\n"
    )
    assert "ollama-custom" in load_models(overrides_path=custom_path)


def test_get_model_found():
    model = get_model(DEFAULT_MODELS, "bedrock-claude-sonnet-4-6")
    assert model.name == "Claude Sonnet 4.6"
    assert model.context == 1_000_000


def test_get_model_not_found():
    with pytest.raises(KeyError, match="Unknown model 'nonexistent'"):
        get_model(DEFAULT_MODELS, "nonexistent")
    with pytest.raises(KeyError, match="Available:"):
        get_model(DEFAULT_MODELS, "nonexistent")


def test_calculate_cost_basic():
    cost = CostConfig(input=3.0, output=15.0)
    assert calculate_cost(cost, input_tokens=1_000_000, output_tokens=500_000) == pytest.approx(
        10.5
    )


def test_calculate_cost_with_billable_cache_categories():
    cost = CostConfig(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75)
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
    assert calculate_cost(CostConfig(input=5.0, output=10.0), 0, 0) == 0.0


def test_calculate_cost_free_model():
    assert calculate_cost(CostConfig(), input_tokens=1_000_000, output_tokens=500_000) == 0.0


def test_usage_sanitization_zeroes_negative_and_non_integer_values():
    assert sanitize_billable_usage(-1, 2.5, "3", True) == (0, 0, 0, 0)
    assert normalize_responses_usage(-1, "bad", -3, 4) == (0, 0, 0, 0)

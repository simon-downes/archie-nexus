"""Tests for the model catalog (models.py)."""

import pytest
from archie_shared.models import (
    DEFAULT_MODELS,
    BedrockProvider,
    CostConfig,
    OllamaProvider,
    calculate_cost,
    get_model,
    load_models,
)

# --- Tests: DEFAULT_MODELS integrity ---


def test_default_models_not_empty():
    """Default catalog has entries."""
    assert len(DEFAULT_MODELS) > 0


def test_default_models_all_have_required_fields():
    """Every default model has name, context, and provider."""
    for key, model in DEFAULT_MODELS.items():
        assert model.name, f"{key} missing name"
        assert model.context > 0, f"{key} has invalid context"
        assert isinstance(model.provider, (BedrockProvider, OllamaProvider)), (
            f"{key} has unknown provider type"
        )
        assert model.provider.model_id, f"{key} missing model_id"


def test_default_model_key_format():
    """All keys follow provider-model format."""
    for key in DEFAULT_MODELS:
        assert key.startswith("bedrock-") or key.startswith("ollama-"), (
            f"Key '{key}' doesn't follow provider-model format"
        )


# --- Tests: load_models ---


def test_load_models_defaults_only(monkeypatch, tmp_path):
    """No overrides file → returns defaults."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    catalog = load_models()
    assert catalog == DEFAULT_MODELS


def test_load_models_with_override(monkeypatch, tmp_path):
    """User file overrides an existing key."""
    models_file = tmp_path / "models.yaml"
    models_file.write_text(
        "bedrock-claude-sonnet-4-6:\n"
        "  name: Custom Sonnet\n"
        "  context: 500000\n"
        "  provider:\n"
        "    type: bedrock\n"
        "    model_id: eu.anthropic.claude-sonnet-4-6\n"
        "  can_cache: true\n"
    )
    catalog = load_models(overrides_path=models_file)
    model = catalog["bedrock-claude-sonnet-4-6"]
    assert model.name == "Custom Sonnet"
    assert model.context == 500_000


def test_load_models_adds_new_key(monkeypatch, tmp_path):
    """User file adds a new model key."""
    models_file = tmp_path / "models.yaml"
    models_file.write_text(
        "bedrock-custom-model:\n"
        "  name: Custom Model\n"
        "  context: 64000\n"
        "  provider:\n"
        "    type: bedrock\n"
        "    model_id: custom.model-id\n"
        "    region: us-east-1\n"
    )
    catalog = load_models(overrides_path=models_file)
    assert "bedrock-custom-model" in catalog
    assert catalog["bedrock-custom-model"].name == "Custom Model"
    assert catalog["bedrock-custom-model"].provider.region == "us-east-1"
    # Defaults still present
    assert "bedrock-claude-sonnet-4-6" in catalog


def test_load_models_missing_file_no_error(tmp_path):
    """Missing overrides file → defaults only, no error."""
    catalog = load_models(overrides_path=tmp_path / "nonexistent.yaml")
    assert catalog == DEFAULT_MODELS


def test_load_models_explicit_path(tmp_path):
    """Explicit overrides_path is used instead of default location."""
    custom_path = tmp_path / "custom_models.yaml"
    custom_path.write_text(
        "ollama-custom:\n"
        "  name: Custom Ollama\n"
        "  context: 32000\n"
        "  provider:\n"
        "    type: ollama\n"
        "    model_id: custom:latest\n"
        "    endpoint: localhost:11434\n"
    )
    catalog = load_models(overrides_path=custom_path)
    assert "ollama-custom" in catalog


# --- Tests: get_model ---


def test_get_model_found():
    """get_model returns the correct entry."""
    model = get_model(DEFAULT_MODELS, "bedrock-claude-sonnet-4-6")
    assert model.name == "Claude Sonnet 4.6"
    assert model.context == 1_000_000


def test_get_model_not_found():
    """get_model raises KeyError with available keys."""
    with pytest.raises(KeyError, match="Unknown model 'nonexistent'"):
        get_model(DEFAULT_MODELS, "nonexistent")
    with pytest.raises(KeyError, match="Available:"):
        get_model(DEFAULT_MODELS, "nonexistent")


# --- Tests: calculate_cost ---


def test_calculate_cost_basic():
    """Basic cost calculation with input and output only."""
    cost = CostConfig(input=3.0, output=15.0)
    result = calculate_cost(cost, input_tokens=1_000_000, output_tokens=500_000)
    assert result == pytest.approx(3.0 + 7.5)


def test_calculate_cost_with_cache():
    """Cost calculation including cache tokens."""
    cost = CostConfig(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75)
    result = calculate_cost(
        cost,
        input_tokens=100_000,
        output_tokens=50_000,
        cache_read_tokens=200_000,
        cache_write_tokens=100_000,
    )
    expected = (
        100_000 * 3.0 / 1_000_000
        + 50_000 * 15.0 / 1_000_000
        + 200_000 * 0.3 / 1_000_000
        + 100_000 * 3.75 / 1_000_000
    )
    assert result == pytest.approx(expected)


def test_calculate_cost_zero():
    """Zero tokens → zero cost."""
    cost = CostConfig(input=5.0, output=10.0)
    assert calculate_cost(cost, 0, 0) == 0.0


def test_calculate_cost_free_model():
    """Ollama-style free model (all rates zero)."""
    cost = CostConfig()
    result = calculate_cost(cost, input_tokens=1_000_000, output_tokens=500_000)
    assert result == 0.0

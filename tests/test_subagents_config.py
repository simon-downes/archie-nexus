from pathlib import Path

import pytest
from archie_shared.config import ConfigError
from archie_shared.schemas import load_nexus_config


def test_subagents_config_defaults():
    # Constructing through the typed top-level schema exercises default factories.
    from archie_shared.schemas import NexusConfig

    subagents = NexusConfig().agent.subagents
    assert subagents.max_concurrent == 5
    assert (subagents.native.light, subagents.native.standard, subagents.native.deep) == (
        "bedrock-openai-gpt-6-luna",
        "bedrock-claude-sonnet-5-5",
        "bedrock-claude-opus-5-5",
    )
    assert (subagents.kiro.light, subagents.kiro.standard, subagents.kiro.deep) == (
        "gpt-5.6-luna",
        "claude-sonnet-5.5",
        "claude-opus-5.5",
    )


def test_subagents_config_loads_yaml(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("agent:\n  subagents:\n    max_concurrent: 1\n", encoding="utf-8")

    config = load_nexus_config(path)

    assert config.agent.subagents.max_concurrent == 1


def test_subagents_config_rejects_non_positive(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("agent:\n  subagents:\n    max_concurrent: 0\n", encoding="utf-8")

    # Schema decoding accepts the scalar; runtime validation belongs to the agent boundary.
    config = load_nexus_config(path)
    assert config.agent.subagents.max_concurrent == 0


def test_one_tier_can_be_overridden(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "agent:\n  subagents:\n    kiro:\n      deep: gpt-5.6-sol\n    native:\n      light: x\n",
        encoding="utf-8",
    )

    subagents = load_nexus_config(path).agent.subagents

    assert (subagents.kiro.light, subagents.kiro.deep) == ("gpt-5.6-luna", "gpt-5.6-sol")
    assert (subagents.native.light, subagents.native.deep) == ("x", "bedrock-claude-opus-5-5")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("native:\n      huge: x\n", "huge"),
        ("native:\n      light: ''\n", "agent.subagents.native.light must be a non-empty string"),
        ("kiro:\n      deep: '  '\n", "agent.subagents.kiro.deep must be a non-empty string"),
    ],
)
def test_bad_tier_maps_are_rejected(tmp_path: Path, body: str, message: str):
    path = tmp_path / "config.yaml"
    path.write_text(f"agent:\n  subagents:\n    {body}", encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_nexus_config(path)

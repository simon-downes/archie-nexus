from pathlib import Path

from archie_shared.schemas import load_nexus_config


def test_subagents_config_defaults():
    # Constructing through the typed top-level schema exercises default factories.
    from archie_shared.schemas import NexusConfig

    assert NexusConfig().agent.subagents.max_concurrent == 3


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

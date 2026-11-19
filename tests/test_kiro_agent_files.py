"""Kiro agent-file generation and prompts (077, tier workers 095)."""

from __future__ import annotations

import json
from pathlib import Path

from archie_agent.kiro.agent_files import (
    WORKER_AGENTS,
    merge_settings,
    root_agent_config,
    tier_of,
    worker_agent_config,
    write_agent_files,
)
from archie_agent.prompt import build_kiro_prompt, build_kiro_worker_prompt

URL = "http://127.0.0.1:41234/mcp"
WORKERS = ["archie-worker-light", "archie-worker-standard", "archie-worker-deep"]
MODELS = {"light": "m-light", "standard": "m-standard", "deep": "m-deep"}


def _write(tmp_path, **kwargs):
    kwargs.setdefault("root_prompt", "R")
    kwargs.setdefault("worker_prompt", "W")
    return write_agent_files(tmp_path, mcp_url=URL, worker_models=MODELS, **kwargs)


def test_root_agent_config():
    config = root_agent_config(URL, [Path("/opt/archie/persona/skills/research/SKILL.md")])
    assert config["name"] == "archie"
    assert config["prompt"] == "file://../archie/archie.md"
    assert config["mcpServers"] == {
        "archie": {"type": "http", "url": URL, "timeout": 10000, "requestTimeout": 600000}
    }
    assert config["tools"] == ["@archie", "subagent", "disclose_context"]
    assert config["resources"] == ["skill:///opt/archie/persona/skills/research/SKILL.md"]
    assert config["includeMcpJson"] is False
    assert config["includePowers"] is False
    assert config["permissions"]["rules"] == [
        {"capability": "mcp", "match": ["archie/*"], "effect": "allow"},
        {"capability": "skill", "match": ["*"], "effect": "allow"},
        {"capability": "subagent", "match": WORKERS, "effect": "allow"},
    ]
    assert config["toolsSettings"]["subagent"] == {
        "availableAgents": WORKERS,
        "trustedAgents": WORKERS,
    }


def test_worker_agent_config():
    config = worker_agent_config(URL, "deep", "claude-opus-5.5")
    assert config["name"] == "archie-worker-deep"
    assert config["model"] == "claude-opus-5.5"
    assert config["description"].startswith("Archie worker on the deep tier, for reviews")
    assert config["prompt"] == "file://../archie/archie-worker.md"
    assert config["tools"] == ["@archie"]
    assert config["mcpServers"]["archie"]["url"] == URL
    assert config["permissions"]["rules"] == [
        {"capability": "mcp", "match": ["archie/*"], "effect": "allow"}
    ]
    assert "toolsSettings" not in config


def test_each_tier_worker_gets_its_model_and_guidance(tmp_path):
    paths = _write(tmp_path)
    for tier, model in MODELS.items():
        config = json.loads(paths[f"worker_agent_{tier}"].read_text())
        assert (config["name"], config["model"]) == (f"archie-worker-{tier}", model)
        assert f"on the {tier} tier" in config["description"]
    assert list(WORKER_AGENTS.values()) == WORKERS


def test_tier_of_maps_worker_names_only():
    assert [tier_of(name) for name in WORKERS] == ["light", "standard", "deep"]
    assert tier_of("archie-worker") is None
    assert tier_of("archie") is None


def test_worker_agent_config_with_an_explicit_tool_list(tmp_path):
    """091 D11: the harness lists the worker's tools to leave out question."""
    tools = ["@archie/shell", "@archie/read"]
    assert worker_agent_config(URL, "light", "m", tools)["tools"] == tools
    paths = _write(tmp_path, worker_tools=tools)
    for tier in MODELS:
        assert json.loads(paths[f"worker_agent_{tier}"].read_text())["tools"] == tools
    assert json.loads(paths["root_agent"].read_text())["tools"][0] == "@archie"


def test_write_agent_files_keeps_prompts_outside_agents_dir(tmp_path):
    paths = _write(tmp_path, root_prompt="ROOT", worker_prompt="WORKER")
    assert (tmp_path / "archie" / "archie.md").read_text() == "ROOT"
    assert (tmp_path / "archie" / "archie-worker.md").read_text() == "WORKER"
    assert not list((tmp_path / "agents").glob("*.md"))
    root = json.loads(paths["root_agent"].read_text())
    # The relative prompt URI resolves from agents/ to the prompt file.
    prompt_rel = root["prompt"].removeprefix("file://")
    assert (paths["root_agent"].parent / prompt_rel).resolve() == paths["root_prompt"].resolve()
    worker = json.loads(paths["worker_agent_deep"].read_text())
    worker_rel = worker["prompt"].removeprefix("file://")
    assert (paths["root_agent"].parent / worker_rel).resolve() == paths["worker_prompt"].resolve()


def test_write_agent_files_removes_the_unpinned_legacy_worker(tmp_path):
    legacy = tmp_path / "agents" / "archie-worker.json"
    legacy.parent.mkdir()
    legacy.write_text("{}")
    _write(tmp_path)
    assert not legacy.exists()
    assert sorted(p.name for p in (tmp_path / "agents").iterdir()) == sorted(
        ["archie.json", *(f"{name}.json" for name in WORKERS)]
    )


def test_merge_settings_keeps_other_keys(tmp_path):
    path = tmp_path / "settings" / "cli.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"toolSearch.enabled": False, "telemetry.enabled": True}))
    merge_settings(tmp_path)
    assert json.loads(path.read_text()) == {"toolSearch.enabled": False, "telemetry.enabled": False}


def test_merge_settings_creates_file(tmp_path):
    merge_settings(tmp_path)
    assert json.loads((tmp_path / "settings" / "cli.json").read_text()) == {
        "telemetry.enabled": False
    }


def test_kiro_prompt_sections():
    prompt = build_kiro_prompt()
    assert "## Running on Kiro" in prompt
    assert "29,000" in prompt
    assert "32,000" not in prompt
    assert "<skills>" not in prompt
    assert "<agents.md>" not in prompt
    assert "Delegating with task" not in prompt
    assert "archie-worker-light" in prompt and "archie-worker-deep" in prompt


def test_kiro_worker_prompt_sections():
    prompt = build_kiro_worker_prompt("You are a worker.")
    assert prompt.startswith("You are a worker.")
    assert "## Running on Kiro" in prompt
    assert "29,000" in prompt
    assert "32,000" not in prompt
    assert "Delegating with task" not in prompt

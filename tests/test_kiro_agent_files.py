"""Kiro agent-file generation and prompts (077)."""

from __future__ import annotations

import json
from pathlib import Path

from archie_agent.kiro.agent_files import (
    merge_settings,
    root_agent_config,
    worker_agent_config,
    write_agent_files,
)
from archie_agent.prompt import build_kiro_prompt, build_kiro_worker_prompt

URL = "http://127.0.0.1:41234/mcp"


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
        {"capability": "subagent", "match": ["archie-worker"], "effect": "allow"},
    ]
    assert config["toolsSettings"]["subagent"] == {
        "availableAgents": ["archie-worker"],
        "trustedAgents": ["archie-worker"],
    }


def test_worker_agent_config():
    config = worker_agent_config(URL)
    assert config["name"] == "archie-worker"
    assert config["prompt"] == "file://../archie/archie-worker.md"
    assert config["tools"] == ["@archie"]
    assert config["mcpServers"]["archie"]["url"] == URL
    assert config["permissions"]["rules"] == [
        {"capability": "mcp", "match": ["archie/*"], "effect": "allow"}
    ]
    assert "toolsSettings" not in config


def test_write_agent_files_keeps_prompts_outside_agents_dir(tmp_path):
    paths = write_agent_files(tmp_path, mcp_url=URL, root_prompt="ROOT", worker_prompt="WORKER")
    assert (tmp_path / "archie" / "archie.md").read_text() == "ROOT"
    assert (tmp_path / "archie" / "archie-worker.md").read_text() == "WORKER"
    assert not list((tmp_path / "agents").glob("*.md"))
    root = json.loads(paths["root_agent"].read_text())
    # The relative prompt URI resolves from agents/ to the prompt file.
    prompt_rel = root["prompt"].removeprefix("file://")
    assert (paths["root_agent"].parent / prompt_rel).resolve() == paths["root_prompt"].resolve()


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


def test_kiro_worker_prompt_sections():
    prompt = build_kiro_worker_prompt("You are a worker.")
    assert prompt.startswith("You are a worker.")
    assert "## Running on Kiro" in prompt
    assert "29,000" in prompt
    assert "32,000" not in prompt
    assert "Delegating with task" not in prompt

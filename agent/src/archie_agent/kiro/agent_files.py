"""Generate the Kiro agent files, prompts and settings for an Archie session (077 D8).

Prompts live under ``<kiro home>/archie/`` because Kiro parses every ``.md`` file in
``<kiro home>/agents/`` as an agent, and agent prompts must use relative ``file://`` URIs.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from archie_shared.schemas import SUBAGENT_TIERS

from archie_agent.tiers import TIER_GUIDANCE

log = logging.getLogger(__name__)

ROOT_AGENT = "archie"
MCP_SERVER = "archie"
# One worker per subagent tier, each pinned to the tier's model (095 D10). They share
# the ``archie-worker.md`` prompt.
WORKER_PROMPT = "archie-worker"
WORKER_AGENTS = {tier: f"archie-worker-{tier}" for tier in SUBAGENT_TIERS}


def tier_of(agent_name: str) -> str | None:
    """The tier of a worker agent name, or None for any other agent."""
    return next((tier for tier, name in WORKER_AGENTS.items() if name == agent_name), None)


def worker_description(tier: str) -> str:
    """The description Kiro shows the root agent when it chooses a worker."""
    return (
        f"Archie worker on the {tier} tier, for {TIER_GUIDANCE[tier]}. Pick the tier by "
        "the work, not by your own model; each tier runs on its own model."
    )


def _mcp_servers(mcp_url: str) -> dict:
    return {
        MCP_SERVER: {
            "type": "http",
            "url": mcp_url,
            "timeout": 10_000,
            "requestTimeout": 600_000,
        }
    }


def root_agent_config(mcp_url: str, skill_paths: Iterable[Path] = ()) -> dict:
    """Agent JSON for the root ``archie`` agent."""
    workers = list(WORKER_AGENTS.values())
    return {
        "name": ROOT_AGENT,
        "description": "Archie: Archie's prompt and tools on Kiro",
        "prompt": f"file://../archie/{ROOT_AGENT}.md",
        "mcpServers": _mcp_servers(mcp_url),
        "tools": [f"@{MCP_SERVER}", "subagent", "disclose_context"],
        "resources": [f"skill://{Path(p)}" for p in skill_paths],
        "includeMcpJson": False,
        "includePowers": False,
        "permissions": {
            "rules": [
                {"capability": "mcp", "match": [f"{MCP_SERVER}/*"], "effect": "allow"},
                {"capability": "skill", "match": ["*"], "effect": "allow"},
                {"capability": "subagent", "match": workers, "effect": "allow"},
            ]
        },
        "toolsSettings": {"subagent": {"availableAgents": workers, "trustedAgents": workers}},
    }


def worker_agent_config(
    mcp_url: str, tier: str, model: str, tools: Sequence[str] | None = None
) -> dict:
    """Agent JSON for the ``archie-worker-<tier>`` subagent, pinned to ``model``.

    ``tools`` lists the worker's tools (``@archie/<name>``); ``None`` gives it every Archie
    tool. The harness lists them to keep root-only tools such as ``question`` out (091 D11).
    """
    return {
        "name": WORKER_AGENTS[tier],
        "description": worker_description(tier),
        "model": model,
        "prompt": f"file://../archie/{WORKER_PROMPT}.md",
        "mcpServers": _mcp_servers(mcp_url),
        "tools": list(tools) if tools is not None else [f"@{MCP_SERVER}"],
        "includeMcpJson": False,
        "includePowers": False,
        "permissions": {
            "rules": [{"capability": "mcp", "match": [f"{MCP_SERVER}/*"], "effect": "allow"}]
        },
    }


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_agent_files(
    kiro_home: Path,
    *,
    mcp_url: str,
    root_prompt: str,
    worker_prompt: str,
    worker_models: Mapping[str, str],
    skill_paths: Iterable[Path] = (),
    worker_tools: Sequence[str] | None = None,
) -> dict[str, Path]:
    """Write the prompts, the root agent and one worker per tier; return paths by name.

    ``worker_models`` maps each tier to its Kiro model id. Paths are keyed
    ``root_prompt``, ``worker_prompt``, ``root_agent`` and ``worker_agent_<tier>``.
    """
    prompts = kiro_home / "archie"
    agents = kiro_home / "agents"
    paths = {
        "root_prompt": prompts / f"{ROOT_AGENT}.md",
        "worker_prompt": prompts / f"{WORKER_PROMPT}.md",
        "root_agent": agents / f"{ROOT_AGENT}.json",
    }
    _atomic_write(paths["root_prompt"], root_prompt)
    _atomic_write(paths["worker_prompt"], worker_prompt)
    _atomic_write(
        paths["root_agent"], json.dumps(root_agent_config(mcp_url, skill_paths), indent=2) + "\n"
    )
    for tier, name in WORKER_AGENTS.items():
        path = paths[f"worker_agent_{tier}"] = agents / f"{name}.json"
        config = worker_agent_config(mcp_url, tier, worker_models[tier], worker_tools)
        _atomic_write(path, json.dumps(config, indent=2) + "\n")
    # A restarted container may still have the unpinned pre-095 worker; Kiro would offer it.
    (agents / f"{WORKER_PROMPT}.json").unlink(missing_ok=True)
    return paths


def merge_settings(kiro_home: Path) -> Path:
    """Set ``telemetry.enabled: false`` in ``settings/cli.json``, keeping other keys."""
    path = kiro_home / "settings" / "cli.json"
    settings: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
            if isinstance(loaded, dict):
                settings = loaded
            else:
                log.warning("Ignoring non-object Kiro settings in %s", path)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Replacing unreadable Kiro settings %s: %s", path, exc)
    settings["telemetry.enabled"] = False
    _atomic_write(path, json.dumps(settings, indent=2) + "\n")
    return path

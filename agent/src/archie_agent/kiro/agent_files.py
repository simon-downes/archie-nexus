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
from collections.abc import Iterable
from pathlib import Path

log = logging.getLogger(__name__)

ROOT_AGENT = "archie"
WORKER_AGENT = "archie-worker"
MCP_SERVER = "archie"


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
                {"capability": "subagent", "match": [WORKER_AGENT], "effect": "allow"},
            ]
        },
        "toolsSettings": {
            "subagent": {"availableAgents": [WORKER_AGENT], "trustedAgents": [WORKER_AGENT]}
        },
    }


def worker_agent_config(mcp_url: str) -> dict:
    """Agent JSON for the ``archie-worker`` subagent."""
    return {
        "name": WORKER_AGENT,
        "description": "General-purpose Archie worker subagent with Archie tools",
        "prompt": f"file://../archie/{WORKER_AGENT}.md",
        "mcpServers": _mcp_servers(mcp_url),
        "tools": [f"@{MCP_SERVER}"],
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
    skill_paths: Iterable[Path] = (),
) -> dict[str, Path]:
    """Write both prompts and both agent JSONs; return their paths by name."""
    prompts = kiro_home / "archie"
    agents = kiro_home / "agents"
    paths = {
        "root_prompt": prompts / f"{ROOT_AGENT}.md",
        "worker_prompt": prompts / f"{WORKER_AGENT}.md",
        "root_agent": agents / f"{ROOT_AGENT}.json",
        "worker_agent": agents / f"{WORKER_AGENT}.json",
    }
    _atomic_write(paths["root_prompt"], root_prompt)
    _atomic_write(paths["worker_prompt"], worker_prompt)
    _atomic_write(
        paths["root_agent"], json.dumps(root_agent_config(mcp_url, skill_paths), indent=2) + "\n"
    )
    _atomic_write(paths["worker_agent"], json.dumps(worker_agent_config(mcp_url), indent=2) + "\n")
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

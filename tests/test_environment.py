"""Tests for the probed environment section."""

from archie_agent import environment
from archie_agent.environment import EnvironmentFacts, _tool_notes, render
from archie_agent.prompt import _build_environment, _load_prompt


def _facts(**overrides) -> EnvironmentFacts:
    base = {
        "in_container": True,
        "os_name": "Debian 13",
        "arch": "aarch64",
        "user": "archie",
        "uid": 502,
        "home": "/home/archie",
        "cwd": "/workspace",
        "sudo": True,
        "runtimes": ("python 3.13 + uv", "node 24 + npm", "go 1.27"),
        "tools": {"CLI": ("rg", "jq"), "Cloud/IaC": ("aws", "tofu")},
        "persistent": (("/workspace", "project files"), ("~/.nexus", "agent data")),
        "archie_self": ("/opt/archie/agent",),
        "available": frozenset({"rg", "jq", "aws", "tofu", "agent-browser"}),
    }
    base.update(overrides)
    return EnvironmentFacts(**base)


NOTES = """<!-- comment -->
- `agent-browser` — headless Chromium
  continuation line.
- `tofu` — use tofu not terraform.
- `scalr` — not installed here.
"""


def test_render_key_facts():
    text = render(_facts(), "/workspace", NOTES, "/opt/archie/venv/bin/python")
    assert "don't run docker or compose" in text
    assert "System: Debian 13 (aarch64) · User: archie (uid 502) · Home: /home/archie" in text
    assert "CWD: /workspace" in text
    assert "`~` is /home/archie, not /root" in text
    assert "Passwordless sudo" in text
    assert "- Runtimes: python 3.13 + uv, node 24 + npm, go 1.27" in text
    assert "- CLI: rg, jq" in text
    assert "- ~/.nexus — agent data" in text
    assert "/opt/archie/agent hold this agent's own code" in text
    assert "don't install into it" in text


def test_render_lists_all_self_paths():
    paths = ("/opt/archie/agent", "/opt/archie/persona", "/opt/archie/plugins")
    text = render(_facts(archie_self=paths), "/workspace")
    assert (
        "/opt/archie/agent, /opt/archie/persona and /opt/archie/plugins hold this agent's own code"
        in text
    )
    assert "/opt/archie/plugins" in environment.ARCHIE_SELF_PATHS


def test_render_omits_absent_facts():
    text = render(
        _facts(
            in_container=False, sudo=False, runtimes=(), tools={}, persistent=(), archie_self=()
        ),
        "/workspace",
    )
    assert "docker" not in text
    assert "sudo" not in text
    assert "Preinstalled" not in text
    assert "Persistent" not in text
    assert "own code" not in text


def test_tool_notes_only_for_installed_tools():
    kept = _tool_notes(NOTES, frozenset({"agent-browser", "tofu"}))
    assert kept == [
        "- `agent-browser` — headless Chromium\n  continuation line.",
        "- `tofu` — use tofu not terraform.",
    ]
    assert _tool_notes(NOTES, frozenset()) == []


def test_render_is_deterministic():
    facts = _facts()
    assert render(facts, "/workspace", NOTES) == render(facts, "/workspace", NOTES)


def test_shipped_tool_notes_parse():
    """Every shipped note entry is keyed by a backticked binary."""
    notes = _load_prompt("tool-notes.md")
    everything = frozenset(
        line.split("`")[1] for line in notes.splitlines() if line.startswith("- `")
    )
    assert {"agent-browser", "tofu", "pandoc", "pdftotext"} <= everything
    assert len(_tool_notes(notes, everything)) == len(everything)


def test_build_environment_uses_cached_probe(monkeypatch):
    calls = []

    def fake_probe():
        calls.append(1)
        return _facts()

    environment.facts.cache_clear()
    monkeypatch.setattr(environment, "probe", fake_probe)
    try:
        first = _build_environment("m", "/workspace")
        second = _build_environment("m", "/workspace")
    finally:
        environment.facts.cache_clear()
    assert first == second
    assert len(calls) == 1
    assert "- `tofu` — OpenTofu" in first


def test_probe_runs_on_this_host():
    facts = environment.probe()
    assert facts.user
    assert facts.home.startswith("/")
    assert isinstance(facts.sudo, bool)

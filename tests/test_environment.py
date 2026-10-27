"""Tests for the environment section rendered from persona/prompts/environment.md."""

import pytest
from archie_agent import environment
from archie_agent.environment import EnvironmentFacts, render
from archie_agent.prompt import _build_environment, _load_prompt

TEMPLATE = """## Environment

{?container}In a container.

System: {os_name} ({arch}) · User: {user} (uid {uid}) · Home: {home} · CWD: {workspace_dir}
{?sudo}Passwordless sudo.


Brain: {brain_dir}. Exec interpreter: {exec_python}.
"""


def _facts(**overrides) -> EnvironmentFacts:
    base = {
        "in_container": True,
        "os_name": "Debian 13",
        "arch": "aarch64",
        "user": "archie",
        "uid": 502,
        "home": "/home/archie",
        "sudo": True,
        "brain_dir": "~/.nexus/brain",
    }
    base.update(overrides)
    return EnvironmentFacts(**base)


# --- rendering ---------------------------------------------------------------


def test_render_fills_values():
    text = render(TEMPLATE, "/workspace", "/venv/bin/python", env=_facts())
    assert text == (
        "## Environment\n\n"
        "In a container.\n\n"
        "System: Debian 13 (aarch64) · User: archie (uid 502) · Home: /home/archie · "
        "CWD: /workspace\n"
        "Passwordless sudo.\n\n"
        "Brain: ~/.nexus/brain. Exec interpreter: /venv/bin/python."
    )


def test_render_drops_guarded_lines_and_blank_runs():
    text = render(TEMPLATE, "/workspace", env=_facts(in_container=False, sudo=False))
    assert "container" not in text
    assert "sudo" not in text
    assert "\n\n\n" not in text
    assert text.startswith("## Environment\n\nSystem:")
    assert text.endswith("Exec interpreter: /opt/archie/venv/bin/python.")


def test_render_inserts_values_verbatim():
    text = render("{user}\n", "/w", env=_facts(user="literal {home} and {?sudo}"))
    assert text == "literal {home} and {?sudo}"


def test_render_unknown_placeholder_raises():
    with pytest.raises(ValueError, match=r"environment.md: unknown placeholder \{nope\}"):
        render("Hello {nope}\n", "/w", env=_facts())


def test_render_unknown_guard_raises():
    with pytest.raises(ValueError, match=r"environment.md: unknown guard \{\?nope\}"):
        render("{?nope}Hello\n", "/w", env=_facts())


def test_render_mid_line_guard_raises():
    with pytest.raises(ValueError, match=r"guard \{\?sudo\} must start a line"):
        render("Hello {?sudo}there\n", "/w", env=_facts())


# --- probing -------------------------------------------------------------------


def test_probe_runs_on_this_host():
    facts = environment.probe()
    assert facts.user
    assert facts.home.startswith("/")
    assert isinstance(facts.sudo, bool)


def test_display_path_uses_tilde_inside_home(tmp_path):
    home = str(tmp_path / "home")
    assert environment._display_path(tmp_path / "home" / ".nexus" / "brain", home) == (
        "~/.nexus/brain"
    )
    assert environment._display_path(tmp_path / "srv" / "brain", home) == str(
        tmp_path / "srv" / "brain"
    )


def test_probe_reports_configured_brain_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_BRAIN_DIR", str(tmp_path / "brain"))
    assert environment.probe().brain_dir == str((tmp_path / "brain").resolve())


# --- shipped template ----------------------------------------------------------


def test_shipped_template_renders():
    text = render(_load_prompt("environment.md"), "/workspace", env=_facts())
    lines = text.splitlines()
    assert "### Installed tools" in lines
    assert "- `~/.nexus/brain` — the brain (curated knowledge store)" in lines
    assert "- `go`, `node`, `npm`, `python`, `uv`" in lines
    assert any(line.startswith("- `tofu` — OpenTofu") for line in lines)
    assert any(line.startswith("- `cf` — Cloudflare") for line in lines)
    assert "\n\n\n" not in text


def test_shipped_template_drops_guarded_lines():
    env = _facts(in_container=False, sudo=False)
    text = render(_load_prompt("environment.md"), "/workspace", env=env)
    assert "Docker container" not in text
    assert "Passwordless sudo" not in text


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

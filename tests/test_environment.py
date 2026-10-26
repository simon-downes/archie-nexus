"""Tests for the environment section rendered from persona/prompts/environment.md."""

import pytest
from archie_agent import environment
from archie_agent.environment import EnvironmentFacts, ToolSpec, parse_template, render
from archie_agent.prompt import _build_environment, _load_prompt

PY = ToolSpec("python", "Runtimes", "--version")
TOFU = ToolSpec("tofu", "Cloud/IaC", note="use tofu not terraform.")
JQ = ToolSpec("jq", "CLI")

TEMPLATE = """---
tools:
  python: {category: Runtimes, version_args: --version}
  jq: {category: CLI}
  tofu: {category: Cloud/IaC, note: use tofu not terraform.}
---
## Environment

{?container}In a container.

System: {os_name} ({arch}) · User: {user} (uid {uid}) · Home: {home} · CWD: {workspace_dir}
{?sudo}Passwordless sudo.

{?tools}Preinstalled:
{tools}

{?paths}Host-mounted:
{paths}

Exec interpreter: {exec_python}.
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
        "tools": ((PY, "3.13"), (JQ, None), (TOFU, None)),
        "paths": (("/workspace", "project files"), ("~/.nexus", "agent data")),
    }
    base.update(overrides)
    return EnvironmentFacts(**base)


def _template(tools_yaml: str, body: str = "{tools}\n") -> str:
    return f"---\ntools:\n{tools_yaml}---\n{body}"


# --- rendering ---------------------------------------------------------------


def test_render_fills_values_and_lists():
    text = render(TEMPLATE, "/workspace", "/venv/bin/python", env=_facts())
    assert text == (
        "## Environment\n\n"
        "In a container.\n\n"
        "System: Debian 13 (aarch64) · User: archie (uid 502) · Home: /home/archie · "
        "CWD: /workspace\n"
        "Passwordless sudo.\n\n"
        "Preinstalled:\n"
        "*Runtimes*:\n- `python` (3.13)\n"
        "*CLI*:\n- `jq`\n"
        "*Cloud/IaC*:\n- `tofu` — use tofu not terraform.\n\n"
        "Host-mounted:\n"
        "- `/workspace` — project files\n- `~/.nexus` — agent data\n\n"
        "Exec interpreter: /venv/bin/python."
    )


def test_render_drops_guarded_lines_and_blank_runs():
    env = _facts(in_container=False, sudo=False, tools=(), paths=())
    text = render(TEMPLATE, "/workspace", env=env)
    for absent in ("container", "sudo", "Preinstalled", "Host-mounted"):
        assert absent not in text
    assert "\n\n\n" not in text
    assert text.startswith("## Environment\n\nSystem:")
    assert text.endswith("Exec interpreter: /opt/archie/venv/bin/python.")


def test_render_inserts_values_verbatim():
    spec = ToolSpec("jq", "CLI", note="literal {user} and {?sudo}")
    text = render(_template("  jq: {category: CLI}\n"), "/w", env=_facts(tools=((spec, None),)))
    assert "- `jq` — literal {user} and {?sudo}" in text


def test_render_is_deterministic():
    env = _facts()
    assert render(TEMPLATE, "/workspace", env=env) == render(TEMPLATE, "/workspace", env=env)


def test_render_unknown_placeholder_raises():
    with pytest.raises(ValueError, match=r"environment.md: unknown placeholder \{nope\}"):
        render(_template("", "Hello {nope}\n"), "/w", env=_facts())


def test_render_unknown_guard_raises():
    with pytest.raises(ValueError, match=r"environment.md: unknown guard \{\?nope\}"):
        render(_template("", "{?nope}Hello\n"), "/w", env=_facts())


def test_render_mid_line_guard_raises():
    with pytest.raises(ValueError, match=r"guard \{\?sudo\} must start a line"):
        render(_template("", "Hello {?sudo}there\n"), "/w", env=_facts())


# --- template parsing --------------------------------------------------------


def test_parse_template_reads_specs_in_order():
    specs, body = parse_template(TEMPLATE)
    assert specs == (PY, JQ, TOFU)
    assert body.lstrip().startswith("## Environment")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("## no frontmatter\n", "missing YAML frontmatter"),
        ("---\ntools: [\n---\nbody\n", "invalid YAML frontmatter"),
        ("---\n- a\n---\nbody\n", "frontmatter must be a mapping"),
        ("---\ntool: {}\n---\nbody\n", "unknown frontmatter key 'tool'"),
        (_template("  jq: {}\n"), "tools.jq: category is required"),
        (_template("  jq: {category: CLI, notes: x}\n"), "tools.jq: unknown key 'notes'"),
        (_template("  jq: {category: 3}\n"), "tools.jq: category must be a non-empty string"),
        (_template("  jq: CLI\n"), "tools.jq: entry must be a mapping"),
    ],
)
def test_parse_template_rejects_bad_input(text, message):
    with pytest.raises(ValueError, match=message):
        parse_template(text)


def test_parse_template_empty_tools():
    specs, _ = parse_template("---\ntools:\n---\nbody\n")
    assert specs == ()


# --- probing -------------------------------------------------------------------


def test_probe_lists_only_tools_on_path(monkeypatch):
    monkeypatch.setattr(environment.shutil, "which", lambda name: name in {"python", "tofu"})
    monkeypatch.setattr(environment, "_version", lambda binary, args: "3.13")
    facts = environment.probe((PY, JQ, TOFU))
    assert facts.tools == ((PY, "3.13"), (TOFU, None))


def test_probe_runs_version_only_when_declared(monkeypatch):
    calls = []
    monkeypatch.setattr(environment.shutil, "which", lambda name: True)
    monkeypatch.setattr(environment, "_version", lambda b, a: calls.append((b, a)) or "1.2")
    environment.probe((PY, JQ, ToolSpec("go", "Runtimes", "version")))
    assert calls == [("python", "--version"), ("go", "version")]


def test_version_parses_major_minor():
    assert environment._version("python3", "--version") is not None
    assert environment._version("definitely-not-a-binary", "--version") is None


def test_paths_shows_mounts_and_children_of_shown_entries(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".nexus" / "sessions").mkdir(parents=True)
    (home / ".agents").mkdir()
    monkeypatch.setattr(
        environment,
        "PATHS",
        (
            ("~/.nexus", "data"),
            ("~/.nexus/sessions", "logs"),
            ("~/.agents", "skills"),
            ("/opt/missing", "gone"),
        ),
    )
    shown = environment._paths(str(home), frozenset({str(home / ".nexus")}))
    assert shown == (("~/.nexus", "data"), ("~/.nexus/sessions", "logs"))


def test_paths_ignores_mounted_ancestor_outside_paths(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".agents").mkdir(parents=True)
    monkeypatch.setattr(environment, "PATHS", (("~/.agents", "skills"),))
    assert environment._paths(str(home), frozenset({str(tmp_path), str(home)})) == ()


def test_probe_runs_on_this_host():
    facts = environment.probe(())
    assert facts.user
    assert facts.home.startswith("/")
    assert isinstance(facts.sudo, bool)
    assert facts.tools == ()


# --- shipped template ----------------------------------------------------------


def test_shipped_template_renders_every_tool():
    specs, _ = parse_template(_load_prompt("environment.md"))
    by_name = {spec.name: spec for spec in specs}
    env = _facts(tools=tuple((spec, None) for spec in specs))
    text = render(_load_prompt("environment.md"), "/workspace", env=env)

    assert by_name["go"].version_args == "version"
    assert "- `pandoc`\n" in text
    assert "- `scalr` — global options go before the command" in text
    assert "`-include=environment` expands related records" in text
    assert "- `tofu` — OpenTofu is installed in place of Terraform" in text
    assert "- `agent-browser` — drives headless Chromium" in text
    assert "- `pdftotext` — poppler PDF tools: `pdftotext`, `pdfinfo`, `pdftoppm`" in text
    assert "{" not in text.replace("`{", "")


def test_shipped_template_renders_with_nothing_found():
    env = _facts(in_container=False, sudo=False, tools=(), paths=())
    text = render(_load_prompt("environment.md"), "/workspace", env=env)
    assert "Preinstalled" not in text
    assert "Host-mounted" not in text
    assert "\n\n\n" not in text


def test_build_environment_uses_cached_probe(monkeypatch):
    calls = []

    def fake_probe(specs):
        calls.append(specs)
        return _facts(tools=tuple((s, None) for s in specs if s.name == "tofu"))

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

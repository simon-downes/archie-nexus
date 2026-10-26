"""Tests for the environment section rendered from persona/prompts/environment.md."""

import pytest
from archie_agent import environment
from archie_agent.environment import EnvironmentFacts, ToolSpec, parse_template, render
from archie_agent.prompt import _build_environment, _load_prompt

PY = ToolSpec("python", "Runtimes")
TOFU = ToolSpec("tofu", "Cloud/IaC", note="use tofu not terraform.")
JQ = ToolSpec("jq", "CLI")

TEMPLATE = """---
tools:
  Runtimes: [python]
  CLI: [jq]
  Cloud/IaC:
    - tofu: use tofu not terraform.
---
## Environment

{?container}In a container.

System: {os_name} ({arch}) · User: {user} (uid {uid}) · Home: {home} · CWD: {workspace_dir}
{?sudo}Passwordless sudo.

{?paths}Host-mounted:
{paths}

Exec interpreter: {exec_python}.

{?tools}### Installed tools
{?tools}
{tools}
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
        "tools": (PY, JQ, TOFU),
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
        "Host-mounted:\n"
        "- `/workspace` — project files\n- `~/.nexus` — agent data\n\n"
        "Exec interpreter: /venv/bin/python.\n\n"
        "### Installed tools\n\n"
        "*Runtimes*:\n- `python`\n\n"
        "*CLI*:\n- `jq`\n\n"
        "*Cloud/IaC*:\n- `tofu` — use tofu not terraform."
    )


def test_render_drops_guarded_lines_and_blank_runs():
    env = _facts(in_container=False, sudo=False, tools=(), paths=())
    text = render(TEMPLATE, "/workspace", env=env)
    for absent in ("container", "sudo", "Installed tools", "Host-mounted"):
        assert absent not in text
    assert "\n\n\n" not in text
    assert text.startswith("## Environment\n\nSystem:")
    assert text.endswith("Exec interpreter: /opt/archie/venv/bin/python.")


def test_render_inserts_values_verbatim():
    spec = ToolSpec("jq", "CLI", note="literal {user} and {?sudo}")
    text = render(_template("  CLI: [jq]\n"), "/w", env=_facts(tools=(spec,)))
    assert "- `jq` — literal {user} and {?sudo}" in text


def test_render_omits_empty_category():
    text = render(TEMPLATE, "/workspace", env=_facts(tools=(PY, TOFU)))
    assert "*CLI*" not in text
    assert "*Runtimes*:\n- `python`\n\n*Cloud/IaC*:\n- `tofu`" in text


def test_render_sorts_tools_and_puts_folded_line_first():
    tools = (
        ToolSpec("zz", "CLI", note="last note."),
        ToolSpec("rg", "CLI"),
        ToolSpec("Fd", "CLI"),
        ToolSpec("aa", "CLI", note="first note."),
        ToolSpec("jq", "CLI"),
    )
    text = render(TEMPLATE, "/w", env=_facts(tools=tools))
    assert "*CLI*:\n- `Fd`, `jq`, `rg`\n- `aa` — first note.\n- `zz` — last note." in text


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
        ("---\ntools: [jq]\n---\nbody\n", "tools must be a mapping of category to tool list"),
        (_template("  CLI: jq\n"), "tools.CLI: must be a non-empty list of tools"),
        (_template("  CLI: []\n"), "tools.CLI: must be a non-empty list of tools"),
        (_template("  3: [jq]\n"), "tools.3: category must be a non-empty string"),
        (_template("  CLI: [3]\n"), "tools.CLI: each tool must be a name"),
        (_template('  CLI: [""]\n'), "tools.CLI: each tool must be a name"),
        (_template("  CLI: [{jq: a, rg: b}]\n"), "tools.CLI: each tool must be a name"),
        (_template("  CLI: [{jq: }]\n"), "tools.CLI: each tool must be a name"),
        (_template("  CLI: [{jq: {category: CLI}}]\n"), "tools.CLI: each tool must be a name"),
        (_template("  CLI: [jq]\n  Data: [jq]\n"), "tool 'jq' is listed more than once"),
        ("---\ntools:\n", "unterminated YAML frontmatter"),
    ],
)
def test_parse_template_rejects_bad_input(text, message):
    with pytest.raises(ValueError, match=message):
        parse_template(text)


def test_parse_template_allows_dashes_inside_frontmatter():
    specs, body = parse_template(_template('  CLI:\n    - jq: "a --- b"\n', "B\n"))
    assert specs[0].note == "a --- b"
    assert body == "B\n"


def test_parse_template_empty_tools():
    specs, _ = parse_template("---\ntools:\n---\nbody\n")
    assert specs == ()


# --- probing -------------------------------------------------------------------


def test_probe_lists_only_tools_on_path(monkeypatch):
    monkeypatch.setattr(environment.shutil, "which", lambda name: name in {"python", "tofu"})
    facts = environment.probe((PY, JQ, TOFU))
    assert facts.tools == (PY, TOFU)


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
    env = _facts(tools=specs)
    text = render(_load_prompt("environment.md"), "/workspace", env=env)
    lines = text.splitlines()

    assert "### Installed tools" in lines
    assert lines[lines.index("*Runtimes*:") + 1] == "- `go`, `node`, `npm`, `python`, `uv`"
    assert "`pandoc`" in lines[lines.index("*CLI*:") + 1]
    expected = [
        "- `scalr` — global options go before the command (`scalr -format=table "
        "get-workspaces`); table shows a few columns, default JSON has every field. "
        "`-include=environment` expands related records, which are otherwise IDs only.",
        "- `tofu` — OpenTofu is installed in place of Terraform; use `tofu` wherever docs "
        "say `terraform`.",
        "- `agent-browser` — drives headless Chromium to load and interact with web pages "
        "(JS-rendered content, forms, logins, screenshots). Use `web_fetch` for static pages.",
        "- `pdftotext` — poppler PDF tools: `pdftotext`, `pdfinfo`, `pdftoppm` for reading "
        "and rasterising PDFs.",
    ]
    for line in expected:
        assert line in lines


def test_shipped_template_renders_with_nothing_found():
    env = _facts(in_container=False, sudo=False, tools=(), paths=())
    text = render(_load_prompt("environment.md"), "/workspace", env=env)
    assert "Installed tools" not in text
    assert "Host-mounted" not in text
    assert "\n\n\n" not in text


def test_build_environment_uses_cached_probe(monkeypatch):
    calls = []

    def fake_probe(specs):
        calls.append(specs)
        return _facts(tools=tuple(s for s in specs if s.name == "tofu"))

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

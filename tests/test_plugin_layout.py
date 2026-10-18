"""Layout and contract guards for plugins under ``plugins/<id>/`` (plan 070)."""

import re
from pathlib import Path

import pytest
import yaml
from archie_agent.exec.plugin_loader import load_plugins
from archie_shared.plugins import load_manifests

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("agent", "tests", "plugins", "shared", "orchestrator", "cli")
SKIP_PARTS = {"__pycache__", ".venv"}

# Procedure skills stay in ``persona/skills``; integration skills live in their plugin (M3).
PROCEDURE_SKILLS = {
    "build-it",
    "council",
    "create-skill",
    "research",
    "review-plan",
    "workflow-implement",
    "workflow-plan",
    "workflow-review",
}

# Integrations already moved out of ``agent/src/archie_agent/exec/tools/``; append on each move.
# Maps plugin id -> the old core module names under ``archie_agent.exec.tools``.
MOVED = {
    "datadog": ["datadog"],
    "incident_io": ["incident_io"],
    "jira": ["jira"],
    "notion": ["notion"],
    "slack": ["slack"],
    "google": ["mail", "calendar", "drive", "meet", "discovery", "google_common"],
}
OLD_MODULES = [(pid, name) for pid, names in MOVED.items() for name in names]


def _python_files():
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            if SKIP_PARTS.isdisjoint(path.relative_to(ROOT).parts):
                yield path


def _patterns(name: str) -> list[re.Pattern[str]]:
    # Built by concatenation so this file does not match its own patterns.
    module = re.escape("archie_agent.exec.tools." + name)
    path = re.escape("exec/tools/" + name)
    from_tools = re.escape("from archie_agent.exec.tools import") + r"[^\n]*\b" + name + r"\b"
    return [re.compile(module), re.compile(path), re.compile(from_tools)]


@pytest.mark.parametrize(("plugin", "name"), OLD_MODULES)
def test_no_references_to_old_integration_location(plugin, name):
    patterns = _patterns(name)
    offenders = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line}: {match.group(0)}")
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize(("plugin", "name"), OLD_MODULES)
def test_moved_integration_has_no_core_package(plugin, name):
    tools = ROOT / "agent" / "src" / "archie_agent" / "exec" / "tools"
    assert not (tools / name).exists()
    assert not (tools / f"{name}.py").exists()
    assert (ROOT / "plugins" / plugin / "plugin.yaml").is_file()


def _manifests():
    return load_manifests(ROOT / "plugins")


def _skill_files(loaded) -> list[Path]:
    return sorted(loaded.skills_dir.glob("*/SKILL.md"))


def test_every_plugin_function_is_documented_in_its_skills():
    manifests = _manifests()
    loaded = load_plugins(manifests, root=ROOT / "plugins")
    missing = []
    for pid, tools in loaded.items():
        text = "\n".join(path.read_text(encoding="utf-8") for path in _skill_files(manifests[pid]))
        namespaces = set(manifests[pid].manifest.namespaces)
        for key in sorted(tools):
            if key.rsplit(".", 1)[0] in namespaces and f"`{key}(" not in text:
                missing.append(f"{pid}: {key}")
    assert not missing, "undocumented plugin functions:\n" + "\n".join(missing)


def test_plugin_tools_match_declared_namespaces():
    """Every tool's namespace is declared by its plugin; every declared namespace has a tool."""
    manifests = _manifests()
    loaded = load_plugins(manifests, root=ROOT / "plugins")
    problems = []
    for pid, tools in loaded.items():
        declared = set(manifests[pid].manifest.namespaces)
        used = {key.rsplit(".", 1)[0] for key in tools}
        problems += [f"{pid}: tool namespace '{ns}' not declared" for ns in sorted(used - declared)]
        problems += [f"{pid}: namespace '{ns}' has no tools" for ns in sorted(declared - used)]
        if declared and manifests[pid].manifest.entry is None:
            problems.append(f"{pid}: declares namespaces but has no entry")
    assert not problems, "\n".join(problems)


def test_every_plugin_with_code_has_a_skill():
    missing = [
        pid
        for pid, loaded in _manifests().items()
        if (loaded.manifest.entry or (loaded.root / "__init__.py").is_file())
        and not _skill_files(loaded)
    ]
    assert not missing, f"plugins with code but no skill: {missing}"


def _plugin_skill_dirs() -> list[Path]:
    return sorted(p for p in (ROOT / "plugins").glob("*/skills/*") if p.is_dir())


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---"), f"{path}: missing frontmatter"
    parts = text.split("---", 2)
    assert len(parts) == 3, f"{path}: unterminated frontmatter"
    data = yaml.safe_load(parts[1])
    assert isinstance(data, dict), f"{path}: frontmatter is not a mapping"
    return data


@pytest.mark.parametrize(
    "skill_dir", _plugin_skill_dirs(), ids=lambda p: f"{p.parents[1].name}/{p.name}"
)
def test_plugin_skill_has_name_and_description(skill_dir):
    skill = skill_dir / "SKILL.md"
    assert skill.is_file(), f"{skill_dir} has no SKILL.md"
    data = _frontmatter(skill)
    assert data.get("name"), f"{skill}: frontmatter needs 'name'"
    assert data.get("description"), f"{skill}: frontmatter needs 'description'"


# Skills are model-facing; host internals must not leak into them. Built by
# concatenation so this file does not match its own patterns.
SKILL_LEAKS = ("ARCHIE_" + "HOME_DIR", "credentials" + ".yaml", "archie_" + "agent.")


@pytest.mark.parametrize(
    "skill_dir", _plugin_skill_dirs(), ids=lambda p: f"{p.parents[1].name}/{p.name}"
)
def test_plugin_skill_does_not_leak_internals(skill_dir):
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    leaked = [needle for needle in SKILL_LEAKS if needle in text]
    assert not leaked, f"{skill_dir}/SKILL.md mentions {leaked}"


def test_persona_skills_holds_only_procedure_skills():
    present = {
        p.name
        for p in (ROOT / "persona" / "skills").iterdir()
        if p.is_dir() and SKIP_PARTS.isdisjoint({p.name})
    }
    assert present <= PROCEDURE_SKILLS, (
        f"integration skills belong in plugins/<id>/skills: {sorted(present - PROCEDURE_SKILLS)}"
    )


# Plugins whose code takes credentials/policy/settings only from ``context()`` (M6+);
# append on each conversion.
CONVERTED = ["datadog", "aws", "incident_io", "jira", "notion", "slack", "google"]

# Built by concatenation so this file does not match its own patterns.
FORBIDDEN_READS = (
    "get_" + "credential",
    "current_" + "policy_snapshot",
    "load_" + "nexus_config",
    "os." + "environ",
    "os." + "getenv",
    "resolve_" + "provider_policy",
)


@pytest.mark.parametrize("name", CONVERTED)
def test_converted_plugin_code_reads_nothing_directly(name):
    plugin = ROOT / "plugins" / name
    offenders = []
    for path in sorted(plugin.rglob("*")):
        rel = path.relative_to(plugin)
        if not path.is_file() or rel.parts[0] == "tests" or not SKIP_PARTS.isdisjoint(rel.parts):
            continue
        if path.suffix not in {".py", ".yaml", ".md"}:
            continue
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN_READS:
            for match in re.finditer(re.escape(needle), text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line}: {needle}")
    assert not offenders, "plugin code must use context():\n" + "\n".join(offenders)

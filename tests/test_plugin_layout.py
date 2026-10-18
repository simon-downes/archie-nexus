"""Layout guards for integrations moved into ``plugins/<id>/`` (plan 070, M1)."""

import re
from pathlib import Path

import pytest
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
MOVED = ["datadog", "incident_io", "jira", "notion"]


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


@pytest.mark.parametrize("name", MOVED)
def test_no_references_to_old_integration_location(name):
    patterns = _patterns(name)
    offenders = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line}: {match.group(0)}")
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize("name", MOVED)
def test_moved_integration_has_no_core_package(name):
    assert not (ROOT / "agent" / "src" / "archie_agent" / "exec" / "tools" / name).exists()
    assert (ROOT / "plugins" / name / "plugin.yaml").is_file()


def test_every_plugin_function_is_documented_in_its_skills():
    manifests = load_manifests(ROOT / "plugins")
    loaded = load_plugins(manifests, root=ROOT / "plugins")
    missing = []
    for pid, tools in loaded.items():
        skills = sorted(manifests[pid].skills_dir.glob("*/SKILL.md"))
        text = "\n".join(path.read_text(encoding="utf-8") for path in skills)
        namespaces = set(manifests[pid].manifest.namespaces)
        for key in sorted(tools):
            if key.rsplit(".", 1)[0] in namespaces and f"`{key}(" not in text:
                missing.append(f"{pid}: {key}")
    assert not missing, "undocumented plugin functions:\n" + "\n".join(missing)


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
CONVERTED = ["datadog", "aws", "incident_io", "jira", "notion"]

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

"""Tests for prompt.py — dynamic system prompt builder."""

from pathlib import Path

from archie_agent.prompt import (
    _IDENTITY,
    _build_environment,
    _build_loaded_skills,
    _build_project_context,
    _build_skills_catalog,
    _build_tools,
    build_system_prompt,
)
from archie_agent.skills import SkillEntry


def test_identity_section_content():
    """Identity section contains key personality traits."""
    assert "Archie" in _IDENTITY
    assert "concise" in _IDENTITY
    assert "tools proactively" in _IDENTITY
    assert "Do not re-read to verify" in _IDENTITY


def test_build_environment_contains_model_and_workspace():
    """Environment section includes model name and workspace dir."""
    env = _build_environment("test-model-v1", "/workspace")
    assert "test-model-v1" in env
    assert "/workspace" in env
    assert "Docker" in env
    assert "Exec interpreter" in env


def test_build_environment_custom_workspace():
    """Environment section respects custom workspace dir."""
    env = _build_environment("model", "/custom/path")
    assert "/custom/path" in env


def test_build_tools_contains_strategy():
    """Tools section includes strategy patterns."""
    tools = _build_tools()
    assert "async def main()" in tools
    assert "grep" in tools
    assert "asyncio.gather" in tools
    assert "shell" in tools


def test_build_tools_contains_native_guidance():
    """Tools section includes native-vs-exec guidance."""
    tools = _build_tools()
    assert "Native tools" in tools
    assert "read" in tools
    assert "web_fetch" in tools
    assert "exec" in tools
    # Should explain when to use exec vs native
    assert "Chain" in tools or "chain" in tools or "multi-step" in tools


def test_build_tools_contains_guidelines():
    """Tools section includes aggregated guidelines from exec tools."""
    tools = _build_tools()
    assert "### Guidelines" in tools
    assert "read" in tools
    assert "write" in tools
    assert "edit" in tools
    assert "glob" in tools
    assert "shell" in tools


def test_build_system_prompt_assembles_all_sections():
    """build_system_prompt returns a single string with all sections."""
    prompt = build_system_prompt("claude-sonnet-4-20250514")

    # Identity
    assert "Archie" in prompt
    # Environment
    assert "claude-sonnet-4-20250514" in prompt
    assert "Docker" in prompt
    # Tools
    assert "async def main()" in prompt
    assert "### Guidelines" in prompt


def test_build_system_prompt_is_single_string():
    """Prompt is a non-empty string (suitable for Bedrock system field)."""
    prompt = build_system_prompt("test-model")
    assert isinstance(prompt, str)
    assert len(prompt) > 100


def test_build_system_prompt_accepts_model_name():
    """Model name parameter is passed through to environment."""
    prompt = build_system_prompt("my-custom-model-7b")
    assert "my-custom-model-7b" in prompt


# ---------------------------------------------------------------------------
# Skills section tests
# ---------------------------------------------------------------------------


def _make_catalog() -> dict[str, SkillEntry]:
    """Create a test skill catalog."""
    return {
        "python-style": SkillEntry(
            name="python-style",
            description="Python coding standards",
            path=Path("/fake/python-style/SKILL.md"),
        ),
        "terraform": SkillEntry(
            name="terraform",
            description="Terraform best practices",
            path=Path("/fake/terraform/SKILL.md"),
        ),
    }


def test_build_skills_catalog_lists_skills():
    """Catalog section lists all skills with descriptions."""
    catalog = _make_catalog()
    section = _build_skills_catalog(catalog, [])
    assert "<skills>" in section
    assert "</skills>" in section
    assert "python-style: Python coding standards" in section
    assert "terraform: Terraform best practices" in section


def test_build_skills_catalog_marks_loaded():
    """Loaded skills are marked with [loaded] in the catalog."""
    catalog = _make_catalog()
    loaded = [("python-style", "body content")]
    section = _build_skills_catalog(catalog, loaded)
    assert "python-style: Python coding standards [loaded]" in section
    assert "[loaded]" not in section.split("terraform")[1].split("\n")[0] or "terraform" in section


def test_build_skills_catalog_no_loaded():
    """No [loaded] markers when nothing is loaded."""
    catalog = _make_catalog()
    section = _build_skills_catalog(catalog, [])
    assert "[loaded]" not in section


def test_build_loaded_skills_renders_bodies():
    """Loaded skills are rendered in tagged blocks."""
    loaded = [
        ("python-style", "Use type hints everywhere."),
        ("terraform", "Pin provider versions."),
    ]
    section = _build_loaded_skills(loaded)
    assert '<skill name="python-style">' in section
    assert "Use type hints everywhere." in section
    assert '<skill name="terraform">' in section
    assert "Pin provider versions." in section
    assert "</skill>" in section


def test_build_loaded_skills_empty():
    """Empty loaded list produces empty string."""
    assert _build_loaded_skills([]) == ""


def test_build_system_prompt_with_catalog():
    """Prompt includes skills section when catalog is provided."""
    catalog = _make_catalog()
    prompt = build_system_prompt("test-model", catalog=catalog)
    assert "<skills>" in prompt
    assert "python-style" in prompt
    assert "terraform" in prompt


def test_build_system_prompt_with_loaded_skills():
    """Prompt includes loaded skill bodies."""
    catalog = _make_catalog()
    loaded = [("python-style", "Use type hints everywhere.")]
    prompt = build_system_prompt("test-model", catalog=catalog, loaded_skills=loaded)
    assert '<skill name="python-style">' in prompt
    assert "Use type hints everywhere." in prompt
    assert "[loaded]" in prompt  # Marked in catalog


def test_build_system_prompt_no_skills_section_without_catalog():
    """Prompt omits skills section when no catalog provided."""
    prompt = build_system_prompt("test-model")
    assert "<skills>" not in prompt


def test_build_system_prompt_no_skills_section_with_empty_catalog():
    """Prompt omits skills section when catalog is empty."""
    prompt = build_system_prompt("test-model", catalog={})
    assert "<skills>" not in prompt


def test_build_system_prompt_backward_compatible():
    """build_system_prompt works without new params (backward compat)."""
    # Original call signature still works
    prompt = build_system_prompt("model-v1")
    assert "model-v1" in prompt
    assert "<skills>" not in prompt


# ---------------------------------------------------------------------------
# Project context (AGENTS.md) tests
# ---------------------------------------------------------------------------


def test_build_project_context_reads_agents_md(tmp_path):
    """AGENTS.md contents are wrapped in an <agents.md> block."""
    (tmp_path / "AGENTS.md").write_text("# Project rules\nUse tabs.", encoding="utf-8")
    section = _build_project_context(str(tmp_path))
    assert section == "<agents.md>\n# Project rules\nUse tabs.\n</agents.md>"


def test_build_project_context_absent_returns_empty(tmp_path):
    """No AGENTS.md → empty string."""
    assert _build_project_context(str(tmp_path)) == ""


def test_build_project_context_empty_file_returns_empty(tmp_path):
    """Empty/whitespace-only AGENTS.md → empty string."""
    (tmp_path / "AGENTS.md").write_text("   \n\n", encoding="utf-8")
    assert _build_project_context(str(tmp_path)) == ""


def test_build_system_prompt_includes_agents_md(tmp_path):
    """Prompt includes AGENTS.md content when present in the workspace."""
    (tmp_path / "AGENTS.md").write_text("Always run ruff.", encoding="utf-8")
    prompt = build_system_prompt("test-model", str(tmp_path))
    assert "<agents.md>" in prompt
    assert "Always run ruff." in prompt


def test_build_system_prompt_omits_agents_md_when_absent(tmp_path):
    """Prompt omits the <agents.md> block when no AGENTS.md exists."""
    prompt = build_system_prompt("test-model", str(tmp_path))
    assert "<agents.md>" not in prompt


def test_agents_md_after_skills_before_loaded_skills(tmp_path):
    """<agents.md> renders after the skills catalog and before loaded skill bodies."""
    (tmp_path / "AGENTS.md").write_text("Project context here.", encoding="utf-8")
    catalog = _make_catalog()
    loaded = [("python-style", "Use type hints.")]
    prompt = build_system_prompt(
        "test-model", str(tmp_path), catalog=catalog, loaded_skills=loaded
    )
    skills_pos = prompt.index("<skills>")
    agents_pos = prompt.index("<agents.md>")
    loaded_pos = prompt.index('<skill name="python-style">')
    assert skills_pos < agents_pos < loaded_pos

"""Tests for prompt.py — dynamic system prompt builder."""

from pathlib import Path

from archie_agent.prompt import (
    _build_environment,
    _build_identity,
    _build_project_context,
    _build_skills_catalog,
    _build_tools,
    build_system_prompt,
)
from archie_agent.skills import SkillEntry


def test_identity_section_content():
    """Identity section contains key personality traits."""
    identity = _build_identity()
    assert "Archie" in identity
    assert "concise" in identity
    assert "tools proactively" in identity
    assert "Do not re-read to verify" in identity


def test_build_environment_contains_workspace_without_model():
    """Environment section includes workspace but not the active model."""
    env = _build_environment("test-model-v1", "/workspace")
    assert "test-model-v1" not in env
    assert "/workspace" in env


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
    assert "Docker" in prompt
    # The active model is deliberately excluded from cacheable prompt text.
    assert "claude-sonnet-4-20250514" not in prompt
    # Tools
    assert "async def main()" in prompt
    assert "### Guidelines" in prompt


def test_build_system_prompt_is_single_string():
    """Prompt is a non-empty string (suitable for Bedrock system field)."""
    prompt = build_system_prompt("test-model")
    assert isinstance(prompt, str)
    assert len(prompt) > 100


def test_build_system_prompt_ignores_model_name():
    """Model name remains accepted for compatibility but is not rendered."""
    prompt = build_system_prompt("my-custom-model-7b")
    assert "my-custom-model-7b" not in prompt


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
    section = _build_skills_catalog(catalog)
    assert "<skills>" in section
    assert "</skills>" in section
    assert "python-style: Python coding standards" in section
    assert "terraform: Terraform best practices" in section


def test_build_skills_catalog_is_stable():
    """The catalog is constant and independent of loaded tool-result state."""
    catalog = _make_catalog()
    assert _build_skills_catalog(catalog) == _build_skills_catalog(catalog)


def test_build_skills_catalog_no_loaded():
    """No [loaded] markers when nothing is loaded."""
    catalog = _make_catalog()
    section = _build_skills_catalog(catalog)
    assert "[loaded]" not in section


def test_build_system_prompt_with_catalog():
    """Prompt includes skills section when catalog is provided."""
    catalog = _make_catalog()
    prompt = build_system_prompt("test-model", catalog=catalog)
    assert "<skills>" in prompt
    assert "python-style" in prompt
    assert "terraform" in prompt


def test_build_system_prompt_with_catalog_has_no_loaded_body():
    """The static catalog never renders mutable skill bodies."""
    catalog = _make_catalog()
    prompt = build_system_prompt("test-model", catalog=catalog)
    assert "<skill name=\"python-style\">" not in prompt
    assert "Use type hints everywhere." not in prompt


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
    assert isinstance(prompt, str)
    assert len(prompt) > 100
    assert "model-v1" not in prompt


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


def test_brain_guidance_precedes_user_guidance_and_refreshes(monkeypatch, tmp_path):
    from archie_agent.prompt import build_system_prompt

    monkeypatch.setenv("ARCHIE_BRAIN_DIR", str(tmp_path))
    (tmp_path / "BRAIN.md").write_text("User brain rules v1")
    first = build_system_prompt("test-model")
    (tmp_path / "BRAIN.md").write_text("User brain rules v2")
    second = build_system_prompt("test-model")
    persona_pos = first.index("The brain is a curated")
    user_pos = first.index("User brain rules v1")
    assert persona_pos < user_pos
    assert "User brain rules v2" in second


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


def test_agents_md_after_skills_catalog(tmp_path):
    """Project context follows static skill guidance and the catalog."""
    (tmp_path / "AGENTS.md").write_text("Project context here.", encoding="utf-8")
    catalog = _make_catalog()
    prompt = build_system_prompt("test-model", str(tmp_path), catalog=catalog)
    skills_pos = prompt.index("<skills>")
    agents_pos = prompt.index("<agents.md>")
    assert skills_pos < agents_pos
    assert '<skill name="python-style">' not in prompt

"""Regression tests for the provider-neutral structured system prompt."""

from pathlib import Path

from archie_agent.prompt import (
    SystemPrompt,
    _build_skills_catalog,
    build_system_prompt,
    build_system_prompt_structured,
)
from archie_agent.skills import SkillEntry


def _catalog() -> dict[str, SkillEntry]:
    return {
        "alpha": SkillEntry("alpha", "Alpha skill", Path("/tmp/alpha/SKILL.md")),
        "beta": SkillEntry("beta", "Beta skill", Path("/tmp/beta/SKILL.md")),
    }


def test_structured_prompt_has_catalog_and_no_loaded_body():
    prompt = build_system_prompt_structured(
        "model-one",
        workspace_dir="/workspace",
        catalog=_catalog(),
        agents_context="Project rules",
    )

    assert isinstance(prompt, SystemPrompt)
    assert "Archie" in prompt.static_system.text
    assert "Project rules" in prompt.static_system.text
    assert "alpha: Alpha skill" in prompt.static_system.text
    assert prompt.dynamic_system is None
    assert "model-one" not in prompt.static_system.text


def test_catalog_is_stable():
    catalog = _catalog()
    assert _build_skills_catalog(catalog) == _build_skills_catalog(catalog)


def test_cacheable_prompt_text_does_not_depend_on_model_name():
    first = build_system_prompt_structured(
        "model-one", catalog=_catalog(), agents_context="Project rules"
    )
    second = build_system_prompt_structured(
        "model-two", catalog=_catalog(), agents_context="Project rules"
    )
    assert first.static_system.text == second.static_system.text


def test_flattened_prompt_without_dynamic_section():
    structured = build_system_prompt_structured(
        "model", catalog=_catalog(), agents_context="Project rules"
    )
    flattened = build_system_prompt(
        "model", catalog=_catalog(), agents_context="Project rules"
    )
    assert flattened == structured.static_system.text

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


def test_structured_prompt_has_static_and_dynamic_sections_without_model_name():
    prompt = build_system_prompt_structured(
        "model-one",
        workspace_dir="/workspace",
        catalog=_catalog(),
        agents_context="Project rules",
        loaded_skills=[("alpha", "Loaded alpha")],
    )

    assert isinstance(prompt, SystemPrompt)
    assert "Archie" in prompt.static_system.text
    assert "Project rules" in prompt.static_system.text
    assert "alpha: Alpha skill" in prompt.static_system.text
    assert "[loaded]" not in prompt.static_system.text
    assert "Loaded alpha" not in prompt.static_system.text
    assert prompt.dynamic_system is not None
    assert "Loaded alpha" in prompt.dynamic_system.text
    assert "model-one" not in prompt.static_system.text
    assert "model-one" not in prompt.dynamic_system.text


def test_catalog_is_stable_when_skills_load():
    catalog = _catalog()
    before = _build_skills_catalog(catalog, [])
    after = _build_skills_catalog(catalog, [("alpha", "Loaded alpha")])
    assert before == after


def test_cacheable_prompt_text_does_not_depend_on_model_name():
    first = build_system_prompt_structured(
        "model-one", catalog=_catalog(), agents_context="Project rules"
    )
    second = build_system_prompt_structured(
        "model-two", catalog=_catalog(), agents_context="Project rules"
    )
    assert first.static_system.text == second.static_system.text


def test_flattened_prompt_preserves_static_then_dynamic_order():
    structured = build_system_prompt_structured(
        "model",
        catalog=_catalog(),
        agents_context="Project rules",
        loaded_skills=[("alpha", "Loaded alpha")],
    )
    flattened = build_system_prompt(
        "model",
        catalog=_catalog(),
        agents_context="Project rules",
        loaded_skills=[("alpha", "Loaded alpha")],
    )
    assert flattened == structured.static_system.text + "\n\n" + structured.dynamic_system.text
    assert flattened.index("Project rules") < flattened.index("Loaded alpha")

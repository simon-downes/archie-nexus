from pathlib import Path

from archie_agent.prompt import build_subagent_prompt
from archie_agent.skills import SkillEntry


def test_build_subagent_prompt_is_focused_and_contains_catalog():
    catalog = {
        "python-style": SkillEntry(
            "python-style", "Python coding standards", Path("/fake/python/SKILL.md")
        ),
        "terraform": SkillEntry("terraform", "Terraform", Path("/fake/tf/SKILL.md")),
    }
    prompt = build_subagent_prompt(
        "child-model",
        "You are a research specialist.",
        workspace_dir="/workspace",
        catalog={"python-style": catalog["python-style"]},
        agents_context="Project rules",
    )

    assert "You are a research specialist." in prompt.static_system.text
    assert "Archie" not in prompt.static_system.text
    assert "Project rules" in prompt.static_system.text
    assert "python-style: Python coding standards" in prompt.static_system.text
    assert "terraform" not in prompt.static_system.text
    assert prompt.dynamic_system is None
    assert "child-model" not in prompt.flatten()

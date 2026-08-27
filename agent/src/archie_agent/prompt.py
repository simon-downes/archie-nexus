"""Provider-neutral structured system prompt builder.

The prompt has two logical sections: a static session prefix and a dynamic
section containing loaded skill bodies. Providers can flatten the sections when
their wire format does not support typed prompt blocks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from archie_shared.brain import brain_root
from archie_shared.config import persona_dir

from archie_agent.exec.tool import PYTHON
from archie_agent.exec.tools import get_tool_guidelines
from archie_agent.exec.tools.fs import WORKSPACE

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from archie_agent.skills import SkillEntry


@dataclass(frozen=True)
class PromptSection:
    """One logical prompt section rendered as text."""

    text: str


@dataclass(frozen=True)
class SystemPrompt:
    """Structured system prompt passed through the provider-neutral boundary."""

    static_system: PromptSection
    dynamic_system: PromptSection | None = None

    def flatten(self) -> str:
        """Render the structured prompt using the existing string format."""
        sections = [self.static_system.text]
        if self.dynamic_system is not None and self.dynamic_system.text:
            sections.append(self.dynamic_system.text)
        return "\n\n".join(section for section in sections if section)


def flatten_system_prompt(system: SystemPrompt | str) -> str:
    """Flatten a structured prompt or return a legacy string unchanged."""
    if isinstance(system, SystemPrompt):
        return system.flatten()
    return system


# ---------------------------------------------------------------------------
# Prompt fragment loading
# ---------------------------------------------------------------------------


def _load_prompt(name: str) -> str:
    """Load a static prompt fragment from ``persona/prompts/<name>``."""
    path = persona_dir() / "prompts" / name
    return path.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def _build_identity() -> str:
    """Load the identity section from persona/prompts/identity.md."""
    return _load_prompt("identity.md")


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def _build_environment(model_name: str, workspace_dir: str) -> str:
    """Build environment context without model-specific cacheable content."""
    # ``model_name`` remains in the compatibility signature for callers that
    # supplied it, but the active model must not affect cacheable prompt text.
    return f"""\
## Environment

- Workspace: {workspace_dir} (project root, mounted from host)
- Exec interpreter: {PYTHON}
- Container: isolated Docker (Debian), ephemeral — destroyed after session
- All file paths are relative to {workspace_dir} unless absolute"""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _build_brain_guidance() -> str:
    parts = [_load_prompt("brain.md")]
    try:
        user = (brain_root() / "BRAIN.md").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as e:
        if not isinstance(e, FileNotFoundError):
            log.warning("Unable to load brain guidance: %s", e)
        user = ""
    if user:
        parts.append(user)
    return "\n\n".join(parts)


def _build_tools() -> str:
    """Build the tools section: strategy (from persona) + aggregated guidelines."""
    guidelines = get_tool_guidelines()

    parts = [_load_prompt("tools-strategy.md")]

    if guidelines:
        bullet_list = "\n".join(f"- {g}" for g in guidelines)
        parts.append(f"\n\n### Guidelines\n\n{bullet_list}")

    return "".join(parts)


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


def _build_skills_catalog(
    catalog: dict[str, SkillEntry],
    loaded_skills: list[tuple[str, str]] | None = None,
) -> str:
    """Build the constant skills catalog section.

    ``loaded_skills`` remains accepted for compatibility with older callers,
    but intentionally does not change the catalog text. Loaded bodies belong in
    the dynamic section.
    """
    lines = ["<skills>", "Available skills (use the `skill` tool to load):"]
    for name in sorted(catalog):
        entry = catalog[name]
        lines.append(f"- {name}: {entry.description}")
    lines.append("</skills>")
    return "\n".join(lines)


def _build_loaded_skills(loaded_skills: list[tuple[str, str]]) -> str:
    """Build the loaded skills section — each skill body in a tagged block."""
    parts: list[str] = []
    for name, body in loaded_skills:
        parts.append(f'<skill name="{name}">\n{body}\n</skill>')
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Project context (AGENTS.md)
# ---------------------------------------------------------------------------


def read_agents_context(workspace_dir: str = str(WORKSPACE)) -> str:
    """Read AGENTS.md once and return its normalized content without a wrapper."""
    try:
        agents_md = (Path(workspace_dir) / "AGENTS.md").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""
    return agents_md.strip()


def _format_project_context(agents_context: str) -> str:
    """Wrap a non-empty AGENTS.md snapshot for inclusion in the prompt."""
    if not agents_context:
        return ""
    return f"<agents.md>\n{agents_context}\n</agents.md>"


def _build_project_context(workspace_dir: str) -> str:
    """Build project context by reading AGENTS.md at call time.

    This compatibility helper remains dynamic; the harness uses
    :func:`read_agents_context` during construction to create a session snapshot.
    """
    return _format_project_context(read_agents_context(workspace_dir))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_system_prompt_structured(
    model_name: str,
    workspace_dir: str = str(WORKSPACE),
    *,
    catalog: dict[str, SkillEntry] | None = None,
    loaded_skills: list[tuple[str, str]] | None = None,
    agents_context: str | None = None,
    dynamic_content: str | None = None,
) -> SystemPrompt:
    """Assemble the static and dynamic system prompt sections.

    ``model_name`` is retained for source compatibility but deliberately does
    not appear in either section. When ``agents_context`` is omitted, the
    compatibility builder reads AGENTS.md for this call; the harness passes an
    explicit session snapshot instead.
    """
    static_sections = [
        _build_identity(),
        _build_environment(model_name, workspace_dir),
        _build_tools(),
        _build_brain_guidance(),
    ]

    if catalog:
        static_sections.append(_build_skills_catalog(catalog))

    if agents_context is None:
        project_context = _build_project_context(workspace_dir)
    else:
        project_context = _format_project_context(agents_context)
    if project_context:
        static_sections.append(project_context)

    dynamic_sections: list[str] = []
    if loaded_skills:
        dynamic_sections.append(_build_loaded_skills(loaded_skills))
    if dynamic_content:
        dynamic_sections.append(dynamic_content)

    return SystemPrompt(
        static_system=PromptSection("\n\n".join(static_sections)),
        dynamic_system=PromptSection("\n\n".join(dynamic_sections)) if dynamic_sections else None,
    )


def build_subagent_prompt(
    model_name: str,
    body: str,
    workspace_dir: str = str(WORKSPACE),
    *,
    catalog: dict[str, SkillEntry] | None = None,
    loaded_skills: list[tuple[str, str]] | None = None,
    agents_context: str | None = None,
) -> SystemPrompt:
    """Build a focused child prompt without the primary identity section.

    The returned structured prompt preserves the provider boundary introduced by
    the prompt-caching work: static content remains separate from dynamic loaded
    skill bodies. ``agents_context`` is expected to be the harness snapshot.
    """
    static_sections = [
        body.strip(),
        _build_environment(model_name, workspace_dir),
        _build_tools(),
        _build_brain_guidance(),
    ]
    if catalog:
        static_sections.append(_build_skills_catalog(catalog))
    if agents_context is None:
        project_context = _build_project_context(workspace_dir)
    else:
        project_context = _format_project_context(agents_context)
    if project_context:
        static_sections.append(project_context)

    dynamic_system = _build_loaded_skills(loaded_skills or [])
    return SystemPrompt(
        static_system=PromptSection("\n\n".join(section for section in static_sections if section)),
        dynamic_system=PromptSection(dynamic_system) if dynamic_system else None,
    )


def build_system_prompt(
    model_name: str,
    workspace_dir: str = str(WORKSPACE),
    *,
    catalog: dict[str, SkillEntry] | None = None,
    loaded_skills: list[tuple[str, str]] | None = None,
    agents_context: str | None = None,
) -> str:
    """Flatten the structured prompt for legacy string callers."""
    return build_system_prompt_structured(
        model_name,
        workspace_dir,
        catalog=catalog,
        loaded_skills=loaded_skills,
        agents_context=agents_context,
    ).flatten()

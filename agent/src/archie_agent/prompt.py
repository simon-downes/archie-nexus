"""Dynamic system prompt builder.

Assembles the system prompt from discrete sections: identity, environment, tools.
Each section is a pure function; the assembled prompt is a single string suitable
for the Bedrock `system` field.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from archie_shared.config import persona_dir

from archie_agent.exec.tool import PYTHON
from archie_agent.exec.tools import get_tool_guidelines
from archie_agent.exec.tools.fs import WORKSPACE

if TYPE_CHECKING:
    from archie_agent.skills import SkillEntry


# ---------------------------------------------------------------------------
# Prompt fragment loading
# ---------------------------------------------------------------------------


def _load_prompt(name: str) -> str:
    """Load a static prompt fragment from ``persona/prompts/<name>``.

    Fails fast: the persona dir is always mounted, so a missing fragment is a
    deployment error, not a condition to paper over with a fallback.
    """
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
    """Build the environment context section."""
    return f"""\
## Environment

- Model: {model_name}
- Workspace: {workspace_dir} (project root, mounted from host)
- Exec interpreter: {PYTHON}
- Container: isolated Docker (Debian), ephemeral — destroyed after session
- All file paths are relative to {workspace_dir} unless absolute"""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


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
    loaded_skills: list[tuple[str, str]],
) -> str:
    """Build the skills catalog section for the system prompt.

    Lists all available skills with their descriptions. Skills that are
    already loaded are marked with [loaded].
    """
    loaded_names = {name for name, _ in loaded_skills}
    lines = ["<skills>", "Available skills (use the `skill` tool to load):"]
    for name in sorted(catalog):
        entry = catalog[name]
        marker = " [loaded]" if name in loaded_names else ""
        lines.append(f"- {name}: {entry.description}{marker}")
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


def _build_project_context(workspace_dir: str) -> str:
    """Build the project-context section from the workspace's AGENTS.md.

    Reads ``{workspace_dir}/AGENTS.md`` at prompt-build time and wraps its
    contents in an ``<agents.md>`` block. Returns an empty string if the file
    is absent, empty, or unreadable.
    """
    try:
        agents_md = (Path(workspace_dir) / "AGENTS.md").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""
    agents_md = agents_md.strip()
    if not agents_md:
        return ""
    return f"<agents.md>\n{agents_md}\n</agents.md>"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_system_prompt(
    model_name: str,
    workspace_dir: str = str(WORKSPACE),
    *,
    catalog: dict[str, SkillEntry] | None = None,
    loaded_skills: list[tuple[str, str]] | None = None,
) -> str:
    """Assemble the full system prompt from sections.

    Args:
        model_name: Model identifier (e.g. "claude-sonnet-4-20250514").
        workspace_dir: Project root path inside the container.
        catalog: Optional skill catalog for rendering the skills section.
        loaded_skills: Optional list of (name, body) tuples for loaded skills.

    Reads ``{workspace_dir}/AGENTS.md`` at build time (if present) and includes
    it as an ``<agents.md>`` block for project-specific context.
    """
    sections = [
        _build_identity(),
        _build_environment(model_name, workspace_dir),
        _build_tools(),
    ]

    # Skills sections (only when catalog is non-empty)
    if catalog:
        sections.append(_build_skills_catalog(catalog, loaded_skills or []))

    # Project context (AGENTS.md), between skills catalog and loaded skill bodies
    project_context = _build_project_context(workspace_dir)
    if project_context:
        sections.append(project_context)

    # Loaded skill bodies
    if loaded_skills:
        sections.append(_build_loaded_skills(loaded_skills))

    return "\n\n".join(sections)

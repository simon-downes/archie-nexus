"""Skill discovery, catalog, and tool handler.

Skills are discovered from two locations (in priority order):
1. ~/.agents/skills/*/SKILL.md — user-level, cross-project (lower priority)
2. ~/.archie/skills/*/SKILL.md — archie-specific user skills (higher priority)

Each SKILL.md uses YAML frontmatter with required `name` and `description`
fields. The body (everything after the second `---`) is loaded on-demand
via the skill tool.

Design decisions:
- One level deep only (no recursive scan)
- Duplicate names: ~/.archie wins, ~/.agents skill silently shadowed
- Discovery returns a dict[str, SkillEntry] keyed by skill name
- Malformed files are skipped with a warning logged
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from archie_agent.tools import ToolSpec

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillEntry:
    """A discovered skill from the catalog."""

    name: str
    description: str
    path: Path  # Path to the SKILL.md file


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_skills() -> dict[str, SkillEntry]:
    """Discover skills from user-level directories.

    Scans ~/.agents/skills/ (lower priority) then ~/.archie/skills/ (higher
    priority). Skills in the higher-priority directory overwrite same-name
    skills from the lower-priority directory.

    Returns:
        Dict of skill name → SkillEntry.
    """
    catalog: dict[str, SkillEntry] = {}

    # User-level shared (lower priority — added first, overwritten by archie-specific)
    agents_skills_dir = Path.home() / ".agents" / "skills"
    _scan_directory(agents_skills_dir, catalog)

    # Archie-specific user skills (higher priority — overwrites shared)
    archie_skills_dir = Path.home() / ".archie" / "skills"
    _scan_directory(archie_skills_dir, catalog)

    return catalog


def _scan_directory(skills_dir: Path, catalog: dict[str, SkillEntry]) -> None:
    """Scan a skills directory and add entries to catalog."""
    if not skills_dir.is_dir():
        return

    try:
        entries = sorted(skills_dir.iterdir())
    except OSError as e:
        log.warning("Failed to read skills directory %s: %s", skills_dir, e)
        return

    for skill_dir in entries:
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            continue

        entry = _parse_skill_file(skill_file)
        if entry is not None:
            catalog[entry.name] = entry


def _parse_skill_file(path: Path) -> SkillEntry | None:
    """Parse a SKILL.md file, returning SkillEntry or None on failure."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("Failed to read skill file %s: %s", path, e)
        return None

    # Split on --- delimiters
    if not content.startswith("---"):
        log.warning("Skill file missing frontmatter delimiters: %s", path)
        return None

    parts = content.split("---", 2)
    if len(parts) < 3:
        log.warning("Skill file missing closing frontmatter delimiter: %s", path)
        return None

    # parts[0] is empty (before first ---), parts[1] is YAML, parts[2] is body
    frontmatter_raw = parts[1]

    try:
        frontmatter = yaml.safe_load(frontmatter_raw)
    except yaml.YAMLError as e:
        log.warning("YAML parse error in skill file %s: %s", path, e)
        return None

    if not isinstance(frontmatter, dict):
        log.warning("Frontmatter is not a mapping in skill file: %s", path)
        return None

    name = frontmatter.get("name")
    description = frontmatter.get("description")

    if not name or not description:
        log.warning("Skill file missing required frontmatter (name/description): %s", path)
        return None

    return SkillEntry(name=str(name), description=str(description), path=path)


# ---------------------------------------------------------------------------
# Skill tool handler
# ---------------------------------------------------------------------------


def create_skill_tool(
    catalog: dict[str, SkillEntry],
    loaded_skills: list[tuple[str, str]],
) -> ToolSpec:
    """Create a skill ToolSpec bound to the given catalog and loaded state.

    Args:
        catalog: Discovered skills (name → SkillEntry).
        loaded_skills: Mutable list of (name, body) tuples. The handler appends
            to this when a skill is loaded.
    """

    async def handler(name: str = "", file: str | None = None, **kwargs) -> str:
        if not name:
            return "Error: missing required parameter: name"

        if name not in catalog:
            available = ", ".join(sorted(catalog.keys()))
            return f"Error: unknown skill '{name}'. Available: {available}"

        entry = catalog[name]

        if file:
            return _handle_read(entry, file)
        else:
            return _handle_load(entry, name, loaded_skills)

    return ToolSpec(
        name="skill",
        description=(
            "Load domain expertise from the skills catalog into the system prompt, "
            "or read a reference file from a skill's directory. "
            "Call without 'file' to load the skill body (persists for the session). "
            "Call with 'file' to read a specific reference file."
        ),
        schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name from the catalog.",
                },
                "file": {
                    "type": "string",
                    "description": (
                        "Optional path to a reference file within the skill directory "
                        "(e.g. 'references/patterns.md'). Omit to load the skill body."
                    ),
                },
            },
            "required": ["name"],
        },
        handler=handler,
    )


def _handle_load(
    entry: SkillEntry,
    name: str,
    loaded_skills: list[tuple[str, str]],
) -> str:
    """Load a skill's body into session state."""
    # Check if already loaded
    for loaded_name, _ in loaded_skills:
        if loaded_name == name:
            return f"Skill '{name}' already loaded."

    # Parse body from SKILL.md
    body = _extract_body(entry.path)

    # Append to loaded skills
    loaded_skills.append((name, body))

    # List reference files in the skill directory
    skill_dir = entry.path.parent
    files = _list_reference_files(skill_dir)

    parts = [f"Loaded skill '{name}' into system prompt."]
    if files:
        parts.append("")
        parts.append("Reference files available (use file param to read):")
        for f in files:
            parts.append(f"- {f}")

    return "\n".join(parts)


def _handle_read(entry: SkillEntry, file: str) -> str:
    """Read a reference file from a skill's directory."""
    skill_dir = entry.path.parent

    # Resolve the path relative to skill directory
    try:
        target = (skill_dir / file).resolve()
    except (OSError, ValueError) as e:
        return f"Error: invalid path: {e}"

    # Validate containment
    try:
        if not target.is_relative_to(skill_dir.resolve()):
            return "Error: path outside skill directory"
    except ValueError:
        return "Error: path outside skill directory"

    if not target.exists():
        return f"Error: file not found: {file}"

    if not target.is_file():
        return f"Error: not a file: {file}"

    # Check for binary content
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "Error: binary file"
    except OSError as e:
        return f"Error: failed to read file: {e}"

    return content


def _extract_body(path: Path) -> str:
    """Extract the body (everything after second ---) from a SKILL.md file."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return ""

    if not content.startswith("---"):
        return ""

    parts = content.split("---", 2)
    if len(parts) < 3:
        return ""

    return parts[2].strip()


def _list_reference_files(skill_dir: Path) -> list[str]:
    """List all files in skill_dir (excluding SKILL.md), as relative paths."""
    files: list[str] = []
    if not skill_dir.is_dir():
        return files

    for item in sorted(skill_dir.rglob("*")):
        if item.is_file() and item.name != "SKILL.md":
            files.append(str(item.relative_to(skill_dir)))

    return files

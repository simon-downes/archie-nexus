"""Skill discovery, catalog, and tool handler."""

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

import yaml
from archie_shared.config import ConfigError, persona_dir
from archie_shared.plugins import PluginManifestError, enabled_plugins, load_manifests
from archie_shared.schemas import load_nexus_config

from archie_agent.tools import ToolSpec

log = logging.getLogger(__name__)
BODY_KEY = "__body__"


@dataclass(frozen=True)
class SkillEntry:
    """A discovered skill from the catalog."""

    name: str
    description: str
    path: Path
    context: tuple[str, ...] = ()  # brain-relative paths the skill depends on


def discover_skills(enabled: Iterable[str] | None = None) -> dict[str, SkillEntry]:
    """Scan ~/.agents/skills, persona skills, then each plugin's skills (by plugin id).

    Plugin skills are limited to ``enabled`` plugin ids; ``None`` reads the
    enabled set from ``config.yaml``. If that config is invalid, a warning is
    logged and every plugin's skills are included (tools stay policy-gated).
    """
    catalog: dict[str, SkillEntry] = {}
    _scan_directory(Path.home() / ".agents" / "skills", catalog)
    _scan_directory(persona_dir() / "skills", catalog)
    try:
        plugins = load_manifests()
    except PluginManifestError as error:
        log.warning("Failed to load plugin manifests for skills: %s", error)
        plugins = {}
    allowed = _enabled_plugin_ids(plugins) if enabled is None else set(enabled)
    for plugin_id in sorted(plugins):
        if allowed is not None and plugin_id not in allowed:
            continue
        loaded = plugins[plugin_id]
        _scan_directory(loaded.skills_dir, catalog, context=tuple(loaded.manifest.context))
    return catalog


def _enabled_plugin_ids(plugins: dict) -> set[str] | None:
    """Enabled plugin ids from ``config.yaml``; None (= all) if the config is invalid."""
    try:
        config = load_nexus_config()
    except ConfigError as error:
        log.warning("Invalid config; including skills of every plugin: %s", error)
        return None
    return enabled_plugins(config.plugins, plugins)


def _scan_directory(
    skills_dir: Path, catalog: dict[str, SkillEntry], *, context: tuple[str, ...] = ()
) -> None:
    if not skills_dir.is_dir():
        return
    try:
        entries = sorted(skills_dir.iterdir())
    except OSError as error:
        log.warning("Failed to read skills directory %s: %s", skills_dir, error)
        return
    for skill_dir in entries:
        skill_file = skill_dir / "SKILL.md"
        if skill_dir.is_dir() and skill_file.is_file():
            entry = _parse_skill_file(skill_file)
            if entry is not None:
                if context:
                    entry = SkillEntry(entry.name, entry.description, entry.path, context)
                catalog[entry.name] = entry


def _parse_skill_file(path: Path) -> SkillEntry | None:
    try:
        content = path.read_text(encoding="utf-8")
        if not content.startswith("---"):
            raise ValueError("missing frontmatter delimiters")
        parts = content.split("---", 2)
        if len(parts) < 3:
            raise ValueError("missing closing frontmatter delimiter")
        frontmatter = yaml.safe_load(parts[1])
        if not isinstance(frontmatter, dict):
            raise ValueError("frontmatter is not a mapping")
        name = frontmatter.get("name")
        description = frontmatter.get("description")
        if not name or not description:
            raise ValueError("missing required frontmatter (name/description)")
        return SkillEntry(str(name), str(description), path)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
        log.warning("Unable to parse skill file %s: %s", path, error)
        return None


def create_skill_tool(
    catalog: dict[str, SkillEntry],
    loaded_content_keys: set[tuple[str, str]],
) -> ToolSpec:
    """Create a skill tool with session-local, deduplicated content state."""
    lock = asyncio.Lock()

    async def handler(name: str = "", references=None, **kwargs) -> str:
        async with lock:
            if not name:
                return "Error: missing required parameter: name"
            if name not in catalog:
                available = ", ".join(sorted(catalog))
                return f"Error: unknown skill '{name}'. Available: {available}"
            if references is not None and not isinstance(references, list):
                return f"Error: references must be a list; received {references!r}"

            requested = []
            invalid = []
            for reference in references or []:
                if (
                    not isinstance(reference, str)
                    or not reference.strip()
                    or Path(reference).is_absolute()
                    or PureWindowsPath(reference).is_absolute()
                    or ".." in Path(reference).parts
                    or ".." in PureWindowsPath(reference).parts
                ):
                    invalid.append(reference)
                elif reference not in requested:
                    requested.append(reference)
            if invalid:
                return "\n".join(f"Error: invalid reference path: {value!r}" for value in invalid)

            entry = catalog[name]
            body_key = (name, BODY_KEY)
            prior = set(loaded_content_keys)
            body_needed = body_key not in prior
            new_references = [
                reference for reference in requested if (name, reference) not in prior
            ]

            body = ""
            if body_needed:
                body, body_error = _read_skill_body(entry.path)
                if body_error:
                    return body_error

            reference_contents: dict[str, str] = {}
            errors: list[str] = []
            for reference in new_references:
                content, error = _read_reference(entry, reference)
                if error:
                    errors.append(error)
                else:
                    reference_contents[reference] = content
            if errors:
                return "\n".join(errors)

            parts: list[str] = []
            if body_needed:
                formatted = _format_skill(
                    name,
                    body,
                    _list_reference_files(entry.path.parent),
                    entry.path.parent.resolve(),
                )
                warning = _missing_context_warning(entry.context)
                parts.append(f"{warning}\n{formatted}" if warning else formatted)
            for reference in new_references:
                parts.append(_format_reference(name, reference, reference_contents[reference]))

            if body_needed:
                loaded_content_keys.add(body_key)
            loaded_content_keys.update((name, reference) for reference in new_references)

            requested_keys = {body_key} | {(name, ref) for ref in requested}
            if prior.intersection(requested_keys):
                parts.append(_loaded_manifest(name, prior))
            if not parts:
                parts.append(_loaded_manifest(name, loaded_content_keys))
            elif not body_needed and not new_references:
                parts = [_loaded_manifest(name, prior)]
            return "\n\n".join(parts)

    return ToolSpec(
        name="skill",
        description=(
            "Load one skill body into the conversation as a <skill> block. "
            "Optionally load explicit relative reference files for that skill with "
            "references=[...], returned as <reference> blocks. The body is returned "
            "before references when first loaded; repeated content is replaced by a "
            "plain-text manifest pointing to earlier tool results. Plain text outside "
            "<skill> and <reference> blocks is status. After loading a skill, check "
            "the listed reference files and request relevant ones before applying "
            "guidance that depends on them. Reference files are never inferred or "
            "loaded automatically."
        ),
        schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name from the catalog."},
                "references": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional relative paths to reference files for this skill. "
                        "Request relevant files before applying guidance that "
                        "depends on them; paths are not inferred. Returned content "
                        "is wrapped in <reference> tags."
                    ),
                },
            },
            "required": ["name"],
        },
        handler=handler,
    )


def _extract_body(path: Path) -> str:
    """Extract a normalized body; load handling distinguishes malformed files."""
    body, error = _read_skill_body(path)
    return "" if error else body


def _read_skill_body(path: Path) -> tuple[str, str | None]:
    try:
        content = path.read_text(encoding="utf-8")
        if not content.startswith("---"):
            return "", "Error: malformed SKILL.md"
        parts = content.split("---", 2)
        if len(parts) < 3:
            return "", "Error: malformed SKILL.md"
        frontmatter = yaml.safe_load(parts[1])
        if (
            not isinstance(frontmatter, dict)
            or not frontmatter.get("name")
            or not frontmatter.get("description")
        ):
            return "", "Error: malformed SKILL.md"
        return parts[2].strip(), None
    except yaml.YAMLError:
        return "", "Error: malformed SKILL.md"
    except (OSError, UnicodeError) as error:
        return "", f"Error: failed to read SKILL.md: {error}"


def _read_reference(entry: SkillEntry, reference: str) -> tuple[str, str | None]:
    skill_dir = entry.path.parent
    try:
        target = (skill_dir / reference).resolve()
        if not target.is_relative_to(skill_dir.resolve()):
            return "", f"Error: path outside skill directory: {reference}"
    except (OSError, ValueError) as error:
        return "", f"Error: invalid reference path {reference!r}: {error}"
    if not target.exists():
        return "", f"Error: file not found: {reference}"
    if not target.is_file():
        return "", f"Error: not a file: {reference}"
    try:
        with target.open("r", encoding="utf-8", newline="") as handle:
            return handle.read(), None
    except UnicodeDecodeError:
        return "", f"Error: binary file: {reference}"
    except OSError as error:
        return "", f"Error: failed to read file {reference}: {error}"


def _missing_context_warning(context: tuple[str, ...]) -> str:
    """Return a warning line naming missing brain entries, or "" when all exist."""
    if not context:
        return ""
    from archie_shared.brain import brain_root

    try:
        root = brain_root()
    except Exception:  # noqa: BLE001 — an unresolvable brain root counts as missing
        root = None
    missing = []
    for path in context:
        try:
            present = root is not None and (root / path).exists()
        except OSError:
            present = False
        if not present:
            missing.append(path)
    if not missing:
        return ""
    return f"Warning: brain entries this skill depends on are missing: {', '.join(missing)}"


def _format_skill(name: str, body: str, references: list[str], skill_dir: Path) -> str:
    result = (
        f"Skill '{name}' loaded. Follow the content inside the `<skill>` tag as guidance "
        f"for the current task.\n"
        f"Skill directory: {skill_dir}\n"
        f"Paths in the skill are relative to this directory.\n\n"
        f'<skill name="{name}">\n{body}\n</skill>'
    )
    if references:
        result += "\n\nReference files available:\n" + "\n".join(
            f"- {reference}" for reference in references
        )
    return result


def _format_reference(name: str, reference: str, content: str) -> str:
    return f'<reference name="{name}" file="{reference}">\n{content}\n</reference>'


def _loaded_manifest(name: str, keys: set[tuple[str, str]]) -> str:
    items = [path for skill, path in keys if skill == name]
    items.sort()
    lines = [
        f"Skill '{name}' content is already available in earlier tool results. "
        "Use those tagged results from the conversation history.",
        "Loaded content:",
    ]
    if BODY_KEY in items:
        lines.append("- skill body")
        items.remove(BODY_KEY)
    lines.extend(f"- {path}" for path in items)
    return "\n".join(lines)


def _list_reference_files(skill_dir: Path) -> list[str]:
    if not skill_dir.is_dir():
        return []
    return [
        str(item.relative_to(skill_dir))
        for item in sorted(skill_dir.rglob("*"))
        if item.is_file() and item.name != "SKILL.md"
    ]

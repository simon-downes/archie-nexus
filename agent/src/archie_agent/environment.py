"""Environment section of the system prompt.

The section's text and the list of preinstalled tools live in the template
``persona/prompts/environment.md``: YAML frontmatter declares the tools and the
markdown body is the layout. This module probes the container once per process
and fills the template's placeholders, so the rendered section stays
byte-stable for prompt caching.

Template syntax:

- ``{name}`` is replaced by a value. Values are inserted verbatim.
- A line starting with ``{?name}`` is kept, without the guard, only when
  ``name`` is non-empty.
- An unknown name is a ``ValueError``.
"""

from __future__ import annotations

import functools
import itertools
import os
import platform
import pwd
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

# Paths worth knowing about, in display order. An entry is shown when it is a
# mount point, or when it exists beneath an earlier shown entry.
PATHS: tuple[tuple[str, str], ...] = (
    ("/workspace", "project files"),
    ("~/.nexus", "agent config, credentials, brain"),
    ("~/.nexus/sessions", "session logs (JSONL)"),
    ("~/.agents", "shared agent skills"),
    ("/opt/archie/agent", "this agent's own code (don't edit unless asked)"),
    ("/opt/archie/persona", "this agent's own prompts and skills (don't edit unless asked)"),
    ("/opt/archie/plugins", "this agent's plugins (don't edit unless asked)"),
)

_SOURCE = "environment.md"
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_GUARD = re.compile(r"\{\?(\w+)\}")


@dataclass(frozen=True)
class ToolSpec:
    """One preinstalled tool declared in the template frontmatter."""

    name: str
    category: str
    note: str | None = None


@dataclass(frozen=True)
class EnvironmentFacts:
    """Probed facts about the agent's container."""

    in_container: bool
    os_name: str
    arch: str
    user: str
    uid: int
    home: str
    sudo: bool
    # Each declared tool found on PATH, in template order (grouped by category).
    tools: tuple[ToolSpec, ...] = ()
    # (display path, description) for each PATHS entry that is shown.
    paths: tuple[tuple[str, str], ...] = ()


# ---------------------------------------------------------------------------
# Template parsing
# ---------------------------------------------------------------------------


def parse_template(text: str) -> tuple[tuple[ToolSpec, ...], str]:
    """Split the template into tool specs and body. Raises ValueError."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{_SOURCE}: missing YAML frontmatter")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        raise ValueError(f"{_SOURCE}: unterminated YAML frontmatter")
    try:
        data = yaml.safe_load("".join(lines[1:end]))
    except yaml.YAMLError as error:
        raise ValueError(f"{_SOURCE}: invalid YAML frontmatter: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"{_SOURCE}: frontmatter must be a mapping")
    unknown = set(data) - {"tools"}
    if unknown:
        raise ValueError(f"{_SOURCE}: unknown frontmatter key {sorted(unknown)[0]!r}")
    categories = data.get("tools") or {}
    if not isinstance(categories, dict):
        raise ValueError(f"{_SOURCE}: tools must be a mapping of category to tool list")
    specs: list[ToolSpec] = []
    for category, entries in categories.items():
        where = f"{_SOURCE}: tools.{category}"
        if not _text(category):
            raise ValueError(f"{where}: category must be a non-empty string")
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"{where}: must be a non-empty list of tools")
        specs.extend(_tool_spec(category, entry, where) for entry in entries)
    names = [spec.name for spec in specs]
    duplicate = next((name for name in names if names.count(name) > 1), None)
    if duplicate:
        raise ValueError(f"{_SOURCE}: tool {duplicate!r} is listed more than once")
    return tuple(specs), "".join(lines[end + 1 :])


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _tool_spec(category: str, entry: object, where: str) -> ToolSpec:
    """An entry is a binary name, or a one-key mapping of binary name to note."""
    if isinstance(entry, dict) and len(entry) == 1:
        [(name, note)] = entry.items()
        if _text(name) and _text(note):
            return ToolSpec(name.strip(), category.strip(), note.strip())
    elif _text(entry):
        return ToolSpec(entry.strip(), category.strip())
    raise ValueError(f"{where}: each tool must be a name or a single `name: note` mapping")


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


def _os_name() -> str:
    try:
        info = platform.freedesktop_os_release()
    except OSError:
        return platform.system()
    name = info.get("NAME", "Linux").removesuffix(" GNU/Linux")
    version = info.get("VERSION_ID", "")
    return f"{name} {version}".strip()


def _has_sudo() -> bool:
    if shutil.which("sudo") is None:
        return False
    try:
        result = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _mount_points() -> frozenset[str]:
    try:
        text = Path("/proc/self/mounts").read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    # Mount paths escape spaces as \040.
    return frozenset(
        line.split()[1].replace("\\040", " ") for line in text.splitlines() if line.strip()
    )


def _paths(home: str, mounts: frozenset[str]) -> tuple[tuple[str, str], ...]:
    shown: list[tuple[str, str]] = []
    shown_real: list[str] = []
    for display, description in PATHS:
        real = display.replace("~", home, 1)
        under_shown = any(real.startswith(parent + "/") for parent in shown_real)
        if real in mounts or (under_shown and os.path.exists(real)):
            shown.append((display, description))
            shown_real.append(real)
    return tuple(shown)


def probe(specs: tuple[ToolSpec, ...]) -> EnvironmentFacts:
    """Probe the current environment for the declared tools (uncached)."""
    entry = pwd.getpwuid(os.getuid())
    tools = tuple(spec for spec in specs if shutil.which(spec.name))
    return EnvironmentFacts(
        in_container=Path("/.dockerenv").exists(),
        os_name=_os_name(),
        arch=platform.machine(),
        user=entry.pw_name,
        uid=entry.pw_uid,
        home=entry.pw_dir,
        sudo=_has_sudo(),
        tools=tools,
        paths=_paths(entry.pw_dir, _mount_points()),
    )


@functools.cache
def facts(specs: tuple[ToolSpec, ...]) -> EnvironmentFacts:
    """Probe once per process; facts are fixed for a session."""
    return probe(specs)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_tools(tools: tuple[ToolSpec, ...]) -> str:
    """One block per category, separated by blank lines; tools sorted by name.

    Tools without a note share the first bullet; each noted tool gets its own.
    """
    blocks = []
    for category, group in itertools.groupby(tools, key=lambda spec: spec.category):
        specs = sorted(group, key=lambda spec: spec.name.lower())
        plain = ", ".join(f"`{spec.name}`" for spec in specs if not spec.note)
        lines = [f"*{category}*:", *([f"- {plain}"] if plain else [])]
        lines += [f"- `{spec.name}` — {spec.note}" for spec in specs if spec.note]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _render_paths(paths: tuple[tuple[str, str], ...]) -> str:
    return "\n".join(f"- `{path}` — {description}" for path, description in paths)


def _values(env: EnvironmentFacts, workspace_dir: str, exec_python: str) -> dict[str, str]:
    return {
        "os_name": env.os_name,
        "arch": env.arch,
        "user": env.user,
        "uid": str(env.uid),
        "home": env.home,
        "workspace_dir": workspace_dir,
        "exec_python": exec_python,
        "tools": _render_tools(env.tools),
        "paths": _render_paths(env.paths),
        "container": "yes" if env.in_container else "",
        "sudo": "yes" if env.sudo else "",
    }


def _lookup(values: dict[str, str], name: str, kind: str) -> str:
    if name not in values:
        raise ValueError(f"{_SOURCE}: unknown {kind} {{{'?' if kind == 'guard' else ''}{name}}}")
    return values[name]


def _fill(body: str, values: dict[str, str]) -> str:
    kept: list[str] = []
    for line in body.splitlines():
        guard = _GUARD.match(line)
        if guard:
            if not _lookup(values, guard.group(1), "guard"):
                continue
            line = line[guard.end() :]
        stray = _GUARD.search(line)
        if stray:
            raise ValueError(f"{_SOURCE}: guard {stray.group(0)} must start a line")
        kept.append(_PLACEHOLDER.sub(lambda m: _lookup(values, m.group(1), "placeholder"), line))
    text = "\n".join(kept).strip()
    return re.sub(r"\n\s*\n(\s*\n)+", "\n\n", text)


def render(
    template_text: str,
    workspace_dir: str,
    exec_python: str = "/opt/archie/venv/bin/python",
    env: EnvironmentFacts | None = None,
) -> str:
    """Render the environment section. ``env`` defaults to the cached probe."""
    specs, body = parse_template(template_text)
    if env is None:
        env = facts(specs)
    return _fill(body, _values(env, workspace_dir, exec_python))

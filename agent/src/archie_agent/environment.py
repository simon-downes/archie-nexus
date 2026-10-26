"""Environment section of the system prompt.

The section is the plain markdown template ``persona/prompts/environment.md``.
The tool and path lists are written out in it; this module only fills the values
that vary between hosts. It probes the container once per process, so the
rendered section stays byte-stable for prompt caching.

Template syntax:

- ``{name}`` is replaced by a value. Values are inserted verbatim.
- A line starting with ``{?name}`` is kept, without the guard, only when
  ``name`` is non-empty.
- An unknown name is a ``ValueError``.
"""

from __future__ import annotations

import functools
import os
import platform
import pwd
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from archie_shared.brain import brain_root

_SOURCE = "environment.md"
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_GUARD = re.compile(r"\{\?(\w+)\}")


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
    brain_dir: str


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


def _display_path(path: Path, home: str) -> str:
    """Show ``path`` relative to ``~`` when it is inside ``home``."""
    try:
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)


def probe() -> EnvironmentFacts:
    """Probe the current environment (uncached)."""
    entry = pwd.getpwuid(os.getuid())
    return EnvironmentFacts(
        in_container=Path("/.dockerenv").exists(),
        os_name=_os_name(),
        arch=platform.machine(),
        user=entry.pw_name,
        uid=entry.pw_uid,
        home=entry.pw_dir,
        sudo=_has_sudo(),
        brain_dir=_display_path(brain_root(), entry.pw_dir),
    )


@functools.cache
def facts() -> EnvironmentFacts:
    """Probe once per process; facts are fixed for a session."""
    return probe()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _values(env: EnvironmentFacts, workspace_dir: str, exec_python: str) -> dict[str, str]:
    return {
        "os_name": env.os_name,
        "arch": env.arch,
        "user": env.user,
        "uid": str(env.uid),
        "home": env.home,
        "brain_dir": env.brain_dir,
        "workspace_dir": workspace_dir,
        "exec_python": exec_python,
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
    return _fill(template_text, _values(env or facts(), workspace_dir, exec_python))

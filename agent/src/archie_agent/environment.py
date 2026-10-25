"""Container environment facts for the system prompt.

Facts are probed once per process and cached: they are fixed for the life of a
session, so the rendered section stays byte-stable for prompt caching.
"""

from __future__ import annotations

import functools
import os
import platform
import pwd
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Preinstalled tools by category. Only tools found on PATH are listed.
# git, curl, wget, zip, tree etc. are omitted — models assume them anyway.
TOOL_CATEGORIES: dict[str, tuple[str, ...]] = {
    "CLI": (
        "rg",
        "fd",
        "jq",
        "yq",
        "xh",
        "difft",
        "just",
        "shellcheck",
        "shfmt",
        "pandoc",
        "pdftotext",
    ),
    "Data": ("sqlite3", "psql", "mysql", "redis-cli"),
    "Cloud/IaC": ("aws", "gh", "tofu", "terraform-docs", "cf"),
    "Browser": ("agent-browser",),
}

# Runtimes rendered with their major[.minor] version: (label, binary, version args).
RUNTIMES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("python", "python3", ("--version",)),
    ("node", "node", ("--version",)),
    ("go", "go", ("version",)),
)
# Package managers appended to their runtime when present.
RUNTIME_COMPANIONS: dict[str, str] = {"python": "uv", "node": "npm"}

# Host-mounted paths (container side) and their purpose. Listed only when the
# path is actually a mount point in this container.
PERSISTENT_PATHS: tuple[tuple[str, str], ...] = (
    ("/workspace", "project files"),
    ("~/.nexus", "agent config, credentials, sessions, brain"),
    ("~/.agents", "shared agent skills"),
)
# The agent's own runtime: mounted from the host but off-limits unless asked.
ARCHIE_SELF_PATHS: tuple[str, ...] = (
    "/opt/archie/agent",
    "/opt/archie/persona",
    "/opt/archie/plugins",
)

_VERSION = re.compile(r"(\d+)\.(\d+)")


@dataclass(frozen=True)
class EnvironmentFacts:
    """Probed facts about the agent's container."""

    in_container: bool
    os_name: str
    arch: str
    user: str
    uid: int
    home: str
    cwd: str
    sudo: bool
    runtimes: tuple[str, ...] = ()
    tools: dict[str, tuple[str, ...]] = field(default_factory=dict)
    persistent: tuple[tuple[str, str], ...] = ()
    archie_self: tuple[str, ...] = ()
    available: frozenset[str] = frozenset()


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


def _version(binary: str, args: tuple[str, ...]) -> str | None:
    try:
        result = subprocess.run(
            [binary, *args], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _VERSION.search(result.stdout + result.stderr)
    if not match:
        return None
    major, minor = match.groups()
    # Node majors carry the meaning; minor releases are noise.
    return major if binary == "node" else f"{major}.{minor}"


def _runtimes() -> tuple[str, ...]:
    rendered: list[str] = []
    for label, binary, args in RUNTIMES:
        if shutil.which(binary) is None:
            continue
        version = _version(binary, args)
        text = f"{label} {version}" if version else label
        companion = RUNTIME_COMPANIONS.get(label)
        if companion and shutil.which(companion):
            text += f" + {companion}"
        rendered.append(text)
    return tuple(rendered)


def _mount_points() -> frozenset[str]:
    try:
        text = Path("/proc/self/mounts").read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    # Mount paths escape spaces as \040.
    return frozenset(
        line.split()[1].replace("\\040", " ") for line in text.splitlines() if line.strip()
    )


def _persistent(home: str, mounts: frozenset[str]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (path, purpose)
        for path, purpose in PERSISTENT_PATHS
        if path.replace("~", home, 1) in mounts
    )


def probe() -> EnvironmentFacts:
    """Probe the current environment (uncached)."""
    entry = pwd.getpwuid(os.getuid())
    tools = {
        category: tuple(name for name in names if shutil.which(name))
        for category, names in TOOL_CATEGORIES.items()
    }
    tools = {category: names for category, names in tools.items() if names}
    candidates = {n for names in TOOL_CATEGORIES.values() for n in names}
    candidates |= {binary for _, binary, _ in RUNTIMES}
    mounts = _mount_points()
    return EnvironmentFacts(
        in_container=Path("/.dockerenv").exists(),
        os_name=_os_name(),
        arch=platform.machine(),
        user=entry.pw_name,
        uid=entry.pw_uid,
        home=entry.pw_dir,
        cwd=os.getcwd(),
        sudo=_has_sudo(),
        runtimes=_runtimes(),
        tools=tools,
        persistent=_persistent(entry.pw_dir, mounts),
        archie_self=tuple(p for p in ARCHIE_SELF_PATHS if p in mounts),
        available=frozenset(name for name in candidates if shutil.which(name)),
    )


@functools.cache
def facts() -> EnvironmentFacts:
    """Probe once per process; facts are fixed for a session."""
    return probe()


def _tool_notes(notes_text: str, available: frozenset[str]) -> list[str]:
    """Keep note entries whose leading `binary` is installed.

    Entries are markdown bullets starting with a backticked binary name;
    continuation lines (indented) belong to the preceding entry.
    """
    entries: list[list[str]] = []
    for line in notes_text.splitlines():
        if line.startswith("- "):
            entries.append([line])
        elif line.startswith("  ") and entries:
            entries[-1].append(line)
    kept = []
    for entry in entries:
        match = re.match(r"- `([^`]+)`", entry[0])
        if match is None or match.group(1) in available:
            kept.append("\n".join(entry))
    return kept


def render(
    env: EnvironmentFacts,
    workspace_dir: str,
    notes_text: str = "",
    exec_python: str = "/opt/archie/venv/bin/python",
) -> str:
    """Render the environment section from probed facts."""
    lines = ["## Environment", ""]
    if env.in_container:
        lines += ["Running inside a Docker container — don't run docker or compose commands.", ""]

    lines.append(
        f"System: {env.os_name} ({env.arch}) · User: {env.user} (uid {env.uid}) · "
        f"Home: {env.home} · CWD: {workspace_dir}"
    )
    if env.sudo:
        lines.append("Passwordless sudo — install missing packages as needed.")
    lines.append(f"Relative paths resolve from {workspace_dir}; `~` is {env.home}, not /root.")

    if env.runtimes or env.tools:
        lines += ["", "Preinstalled:"]
        if env.runtimes:
            lines.append(f"- Runtimes: {', '.join(env.runtimes)}")
        lines += [f"- {category}: {', '.join(names)}" for category, names in env.tools.items()]

    notes = _tool_notes(notes_text, env.available)
    if notes:
        lines += ["", "Tool notes:", *notes]

    if env.persistent:
        lines += ["", "Persistent (host-mounted; everything else is lost when the session ends):"]
        lines += [f"- {path} — {purpose}" for path, purpose in env.persistent]

    lines.append("")
    if env.archie_self:
        paths = list(env.archie_self)
        joined = paths[0] if len(paths) == 1 else f"{', '.join(paths[:-1])} and {paths[-1]}"
        lines.append(
            f"{joined} hold this agent's own code and prompts "
            "(host-mounted) — don't edit unless asked."
        )
    lines.append(
        f"Exec interpreter: {exec_python}. /opt/archie/venv runs this agent — don't install "
        "into it; use uv or a project venv."
    )
    return "\n".join(lines)

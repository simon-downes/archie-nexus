"""Resolve user-configured host bind mounts for agent containers."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from archie_shared.schemas import MountConfig

from archie_orchestrator.docker import (
    CONTAINER_AGENT_DIR,
    CONTAINER_AGENTS_HOME,
    CONTAINER_HOME,
    CONTAINER_NEXUS_HOME,
    CONTAINER_PERSONA_DIR,
    CONTAINER_SHARED_DIR,
    CONTAINER_WORKSPACE_DIR,
)

log = logging.getLogger(__name__)
_MODES = frozenset({"ro", "rw"})
_MAGIC = frozenset("*?[")
_FIXED_TARGETS = (
    CONTAINER_AGENT_DIR,
    CONTAINER_SHARED_DIR,
    CONTAINER_PERSONA_DIR,
    CONTAINER_WORKSPACE_DIR,
    CONTAINER_NEXUS_HOME,
    CONTAINER_AGENTS_HOME,
)


def _split_mode(value: str, label: str) -> tuple[str, str]:
    """Return a mount path and its supported Docker mode."""
    path, separator, mode = value.rpartition(":")
    if not separator:
        path, mode = value, "rw"
    elif not path:
        raise ValueError(f"Mount {label} path must not be empty")
    elif mode not in _MODES:
        raise ValueError(f"Mount {label} has unsupported mode: {mode!r}")
    if not path:
        raise ValueError(f"Mount {label} path must not be empty")
    return path, mode


def _container_path(value: str) -> str:
    if value == "~":
        return CONTAINER_HOME
    if value.startswith("~/"):
        return f"{CONTAINER_HOME}/{value[2:]}"
    return value


def _same_container_path(value: str) -> str:
    expanded = _expand_home(value)
    home = str(Path.home())
    if expanded == home:
        return CONTAINER_HOME
    if expanded.startswith(f"{home}/"):
        return f"{CONTAINER_HOME}/{expanded[len(home) + 1 :]}"
    return expanded


def _expand_home(value: str) -> str:
    if value == "~":
        return str(Path.home())
    if value.startswith("~/"):
        return str(Path.home() / value[2:])
    return value


def _split_glob(source: str) -> tuple[Path, str] | None:
    expanded = _expand_home(source)
    if not any(char in expanded for char in _MAGIC):
        return None
    parts = Path(expanded).parts
    magic_index = next(index for index, part in enumerate(parts) if any(c in part for c in _MAGIC))
    if magic_index != len(parts) - 1:
        raise ValueError("Mount wildcards are only supported in the final path component")
    pattern = parts[magic_index]
    if "**" in pattern:
        raise ValueError("Recursive '**' mount globs are not supported")
    return Path(*parts[:magic_index]), pattern


def _matches(source: str) -> list[Path]:
    expanded = _expand_home(source)
    glob = _split_glob(source)
    if glob is None:
        path = Path(expanded)
        return [path] if path.exists() else []
    parent, pattern = glob
    return sorted(parent.glob(pattern))


def _validate_target(target: str) -> None:
    if not target.startswith("/"):
        raise ValueError(f"Mount destination must be absolute: {target!r}")
    for fixed in _FIXED_TARGETS:
        if target == fixed or target.startswith(f"{fixed}/"):
            raise ValueError(f"Mount destination conflicts with fixed mount {fixed!r}")


def resolve_mounts(mounts: Iterable[MountConfig]) -> tuple[str, ...]:
    """Resolve config entries into Docker ``host:container:mode`` volume specs."""
    resolved: list[str] = []
    for entry in mounts:
        if isinstance(entry, str):
            source_value, mode = _split_mode(entry, "source")
            destination_value = source_value
        else:
            source_value, destination_value = entry
            destination_value, mode = _split_mode(destination_value, "destination")

        is_wildcard = any(char in source_value for char in _MAGIC)
        destination = _container_path(destination_value)
        glob = _split_glob(source_value) if is_wildcard else None
        if is_wildcard and isinstance(entry, str):
            destination = _same_container_path(str(glob[0]))
        _validate_target(destination)
        matches = _matches(source_value)
        if not matches:
            log.warning("Configured mount source not found: %s", source_value)
            continue

        for match in matches:
            if is_wildcard:
                target = str(Path(destination) / match.name)
            elif isinstance(entry, str):
                target = _same_container_path(source_value)
            else:
                target = destination
            _validate_target(target)
            resolved.append(f"{match}:{target}:{mode}")
    return tuple(resolved)

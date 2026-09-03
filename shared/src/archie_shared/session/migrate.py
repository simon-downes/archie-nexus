"""One-shot migration of legacy session logs to the schema-2 event model."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import msgspec

from archie_shared.events import decode_event

log = logging.getLogger(__name__)


class MessageMetadata(msgspec.Struct):
    """Legacy per-message metadata retained for migration decoding."""

    model: str
    backend: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float = 0.0
    interrupted: bool = False


class MessageEntry(msgspec.Struct):
    """Legacy JSONL message entry retained for migration decoding."""

    id: str
    when: str
    role: str
    content: str
    metadata: MessageMetadata | None = None


@dataclass(frozen=True)
class MigrationStats:
    """Counts produced by one session-log migration."""

    logs_migrated: int = 0
    shell_records_removed: int = 0
    model_switch_records_removed: int = 0


class MigrationAbortError(ValueError):
    """A supported log cannot be converted into the final event schema."""


_IDENTITY_EVENT_TYPES = {"iteration_start", "text_delta", "llm_request", "tool_call", "tool_result"}


def _schema_version(path: Path) -> int | None:
    """Return the session schema version, if a session_started line declares one."""
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("type") == "session_started":
            version = data.get("schema_version")
            if isinstance(version, int) and not isinstance(version, bool):
                return version
            log.warning("Session log %s line %d has an invalid schema_version", path, number)
    return None


def _split_turn_iteration(value: object) -> tuple[int, int]:
    if not isinstance(value, str):
        raise ValueError("turn_iteration must be a string")
    parts = value.split(".")
    if len(parts) != 2:
        raise ValueError(f"invalid turn_iteration {value!r}")
    try:
        turn, iteration = (int(part) for part in parts)
    except ValueError as exc:
        raise ValueError(f"invalid turn_iteration {value!r}") from exc
    return turn, iteration


def _request_index(lines: list[str], path: Path) -> dict[str, tuple[int, int]]:
    """Index legacy request IDs and their turn/iteration identity."""
    requests: dict[str, tuple[int, int]] = {}
    for number, raw in enumerate(lines, 1):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or data.get("type") != "llm_request":
            continue
        request_id = data.get("id")
        turn_iteration = data.get("turn_iteration")
        if isinstance(request_id, str) and turn_iteration is not None:
            try:
                requests[request_id] = _split_turn_iteration(turn_iteration)
            except ValueError as exc:
                log.warning(
                    "Migration skipped request identity on line %d in %s: %s",
                    number,
                    path,
                    exc,
                )
    return requests


def _migrate_line(
    raw: str, path: Path, number: int, request_index: dict[str, tuple[int, int]]
) -> tuple[str | None, int, int]:
    """Convert one line and return ``(line, shell_removed, model_removed)``."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("Migration copied malformed line %d in %s: %s", number, path, exc)
        return raw, 0, 0

    if not isinstance(data, dict):
        log.warning("Migration copied unrecognised line %d in %s", number, path)
        return raw, 0, 0

    event_type = data.get("type")
    has_shell_discriminator = data.get("role") == "shell" or event_type == "shell_command"
    if has_shell_discriminator:
        return None, 1, 0
    if event_type == "model_switch":
        return None, 0, 1

    try:
        if event_type == "session_started":
            data["schema_version"] = 2
        elif event_type in _IDENTITY_EVENT_TYPES and "turn_iteration" in data:
            turn, iteration = _split_turn_iteration(data.pop("turn_iteration"))
            data["turn"] = turn
            data["iteration"] = iteration
            if event_type == "iteration_start":
                data.pop("index", None)
        elif event_type == "assistant_message":
            request_ids = data.get("request_ids")
            if (
                not isinstance(request_ids, list)
                or not request_ids
                or not isinstance(request_ids[-1], str)
                or request_ids[-1] not in request_index
            ):
                raise MigrationAbortError(
                    f"assistant_message on line {number} has no matching request identity"
                )
            request_id = request_ids[-1]
            turn, iteration = request_index[request_id]
            data["turn"] = turn
            data["iteration"] = iteration
            data["request_id"] = request_id
            data.pop("request_ids", None)
            data.pop("turn_iteration", None)

        migrated = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        decode_event(migrated, persisted=True)
        return migrated, 0, 0
    except MigrationAbortError:
        raise
    except (ValueError, TypeError, msgspec.DecodeError, msgspec.ValidationError) as exc:
        log.warning("Migration copied unrecognised line %d in %s: %s", number, path, exc)
        return raw, 0, 0


def migrate_session_log(path: Path, *, force: bool = False) -> MigrationStats:
    """Migrate one session log atomically and retain the original as ``.legacy``.

    A schema-version-2 log is skipped and returns zero counts. A failed
    temporary-file write or rename leaves the original log untouched; the
    backup may remain and must be explicitly forced on retry.
    """
    path = Path(path)
    if _schema_version(path) == 2:
        log.info("Skipping already migrated session log %s", path)
        return MigrationStats()

    backup = path.with_name(f"{path.name}.legacy")
    if backup.exists() and not force:
        raise FileExistsError(f"migration backup already exists: {backup}")

    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    request_index = _request_index(lines, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    temporary: Path | None = None
    shell_removed = 0
    model_switch_removed = 0
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            for number, raw in enumerate(lines, 1):
                migrated, shell_count, model_count = _migrate_line(raw, path, number, request_index)
                shell_removed += shell_count
                model_switch_removed += model_count
                if migrated is not None:
                    output.write(migrated)
                    output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return MigrationStats(
        logs_migrated=1,
        shell_records_removed=shell_removed,
        model_switch_records_removed=model_switch_removed,
    )


def migrate_session_logs(paths: list[Path], *, force: bool = False) -> MigrationStats:
    """Migrate supplied logs and aggregate their immutable statistics."""
    total = MigrationStats()
    for path in paths:
        stats = migrate_session_log(path, force=force)
        total = MigrationStats(
            logs_migrated=total.logs_migrated + stats.logs_migrated,
            shell_records_removed=total.shell_records_removed + stats.shell_records_removed,
            model_switch_records_removed=(
                total.model_switch_records_removed + stats.model_switch_records_removed
            ),
        )
    return total

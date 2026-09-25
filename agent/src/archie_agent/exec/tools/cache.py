"""Small internal file cache for tool-owned metadata."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from archie_shared.config import home_dir

_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}\.[A-Za-z0-9_-]{1,64}$")
_MAX_BYTES = 256 * 1024
_ENVELOPE_KEYS = {"expires", "data"}


class ToolCache:
    def __init__(self, root: Path | None = None, clock: Callable[[], datetime] | None = None):
        self.root = root if root is not None else home_dir() / "cache"
        self.clock = clock or (lambda: datetime.now(UTC))

    def _path(self, key: str) -> Path:
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise ValueError("cache key must match <tool>.<item>")
        return self.root / f"{key}.json"

    def _read(self, key: str) -> tuple[datetime, dict[str, Any]] | None:
        path = self._path(key)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) != _ENVELOPE_KEYS:
                return None
            expires = datetime.fromisoformat(raw["expires"])
            data = raw["data"]
            if expires.tzinfo is None or not isinstance(data, dict):
                return None
            return expires.astimezone(UTC), data
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._read(key)
        if entry is None or entry[0] <= self.clock().astimezone(UTC):
            return None
        return entry[1]

    def exists(self, key: str) -> bool:
        path = self._path(key)
        try:
            return path.is_file()
        except OSError:
            return False

    def valid(self, key: str) -> bool:
        entry = self._read(key)
        if entry is None:
            return False
        return entry[0] > self.clock().astimezone(UTC) + timedelta(seconds=10)

    def delete(self, key: str) -> None:
        path = self._path(key)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return

    def set(self, key: str, value: dict[str, Any], ttl: float) -> None:
        path = self._path(key)
        if (
            not isinstance(value, dict)
            or isinstance(ttl, bool)
            or not isinstance(ttl, (int, float))
            or not math.isfinite(ttl)
            or ttl <= 0
        ):
            raise ValueError("invalid cache value or ttl")
        payload = json.dumps(
            {
                "expires": (self.clock().astimezone(UTC) + timedelta(seconds=ttl)).isoformat(),
                "data": value,
            },
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(payload.encode("utf-8")) > _MAX_BYTES:
            raise ValueError("cache value is too large")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.root)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        except OSError:
            return


setattr(ToolCache, "del", ToolCache.delete)
cache = ToolCache()

__all__ = ["ToolCache", "cache"]

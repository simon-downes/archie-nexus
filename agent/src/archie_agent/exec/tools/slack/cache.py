"""Small metadata-only Slack cache seam with complete envelopes."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class MetadataCache:
    def __init__(self, root: Path | None = None):
        self.root = root

    def load(self, kind: str, *, ttl: float) -> list[dict[str, Any]] | None:
        if self.root is None:
            return None
        path = self.root / "slack" / f"{kind}.json"
        try:
            value = json.loads(path.read_text())
            if value.get("complete") is not True or time.time() >= value["expires_at"]:
                return None
            records = value["records"]
            return records if isinstance(records, list) else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, kind: str, records: list[dict[str, Any]], *, ttl: float) -> None:
        if self.root is None:
            return
        directory = self.root / "slack"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{kind}.json"
        temporary = path.with_suffix(".tmp")
        envelope = {
            "schema_version": 1,
            "fetched_at": time.time(),
            "expires_at": time.time() + ttl,
            "complete": True,
            "records": records,
        }
        temporary.write_text(json.dumps(envelope, separators=(",", ":")))
        temporary.replace(path)

"""Result envelope for exec runner ↔ host IPC.

Defines the structured result that the runner subprocess writes to result.json
and the host-side handler reads back. Using dataclasses instead of raw dicts
gives us type safety, autocomplete, and a single source of truth for the shape.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ErrorInfo:
    """Structured error information from a failed execution."""

    type: str
    message: str
    traceback: str = ""


@dataclass
class CallRecord:
    """Audit record for a single exec tool invocation."""

    fn: str
    args: str
    ms: int
    ok: bool


@dataclass
class Envelope:
    """Result envelope for exec runner subprocess IPC.

    Written as JSON to <run_dir>/result.json by the runner, read by the
    host-side handler (tool.py).
    """

    ok: bool
    return_value: Any = None
    return_repr: bool = False
    stdout: str = ""
    stderr: str = ""
    error: ErrorInfo | None = None
    calls: list[CallRecord] = field(default_factory=list)
    truncated: bool = False
    duration_ms: int = 0

    def to_json(self) -> str:
        """Serialize to JSON string for file IPC."""
        d = asdict(self)
        # Rename return_value → return for the JSON wire format
        d["return"] = d.pop("return_value")
        return json.dumps(d, ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, raw: str) -> Envelope:
        """Deserialize from JSON string (reads result.json)."""
        d = json.loads(raw)
        # Rename return → return_value (return is a Python keyword)
        d["return_value"] = d.pop("return", None)
        # Handle nested error dict → ErrorInfo
        if d.get("error") and isinstance(d["error"], dict):
            d["error"] = ErrorInfo(**d["error"])
        else:
            d["error"] = None
        # Handle calls list → CallRecord
        d["calls"] = [CallRecord(**c) for c in d.get("calls", [])]
        # Drop unknown keys (forward compat)
        known = {f.name for f in cls.__dataclass_fields__.values()}
        d = {k: v for k, v in d.items() if k in known}
        return cls(**d)

    def write(self, run_dir: Path) -> None:
        """Write this envelope to result.json in the given run directory."""
        (run_dir / "result.json").write_text(self.to_json())

    @classmethod
    def read(cls, run_dir: Path) -> Envelope:
        """Read an envelope from result.json in the given run directory."""
        return cls.from_json((run_dir / "result.json").read_text())

    @classmethod
    def error_envelope(
        cls,
        error_type: str,
        message: str,
        tb: str = "",
        stdout: str = "",
        stderr: str = "",
        calls: list[CallRecord] | None = None,
        duration_ms: int = 0,
    ) -> Envelope:
        """Convenience constructor for error envelopes."""
        return cls(
            ok=False,
            error=ErrorInfo(type=error_type, message=message, traceback=tb),
            stdout=stdout,
            stderr=stderr,
            calls=calls or [],
            duration_ms=duration_ms,
        )

"""Live tool-policy reload for exec calls.

The agent re-reads ``config.yaml`` before each exec call so a policy edit takes
effect without a new session. If the file is invalid, the last valid snapshot
stays in force. Notices are emitted only on state transitions so a broken file
does not flood the TUI.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from archie_shared.events import ErrorNotice
from archie_shared.schemas import load_nexus_config
from archie_shared.tool_policy import ToolsConfig, policy_snapshot
from ulid import ULID

RELOAD_FAILED_KIND = "tool_policy_reload_failed"
RECOVERED_KIND = "tool_policy_reloaded"

Snapshot = dict[str, Any]


def load_policy_snapshot() -> Snapshot:
    """Read ``config.yaml`` now and return its detached tool-policy snapshot."""
    return policy_snapshot(ToolsConfig(providers=load_nexus_config().tools))


class LiveToolPolicy:
    """The last valid tool-policy snapshot plus a loader that refreshes it."""

    def __init__(
        self,
        initial: Snapshot,
        *,
        loader: Callable[[], Snapshot] | None = load_policy_snapshot,
    ) -> None:
        self._snapshot = initial or {}
        self._loader = loader
        self._reported_error: str | None = None

    @classmethod
    def static(cls, snapshot: Snapshot | None) -> LiveToolPolicy:
        """A policy that never reloads."""
        return cls(snapshot or {}, loader=None)

    def refresh(self) -> tuple[Snapshot, ErrorNotice | None]:
        """Reload the policy; return the snapshot to use and any notice to show."""
        if self._loader is None:
            return self._snapshot, None
        try:
            snapshot = self._loader()
        except Exception as exc:  # noqa: BLE001 — any failure keeps the last valid policy
            message = str(exc) or type(exc).__name__
            if message == self._reported_error:
                return self._snapshot, None
            self._reported_error = message
            return self._snapshot, ErrorNotice(
                id=str(ULID()),
                kind=RELOAD_FAILED_KIND,
                message=f"Tool policy reload failed; using the last valid policy. {message}",
            )
        self._snapshot = snapshot
        if self._reported_error is None:
            return snapshot, None
        self._reported_error = None
        return snapshot, ErrorNotice(
            id=str(ULID()),
            kind=RECOVERED_KIND,
            message="Tool policy reloaded from config.yaml.",
        )

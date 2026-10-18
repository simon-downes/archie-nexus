"""Live tool-policy reload for exec calls, plus the startup-only runner context.

The agent re-reads ``config.yaml`` before each exec call so a policy edit takes
effect without a new session. If the file is invalid, the last valid snapshot
stays in force. Notices are emitted only on state transitions so a broken file
does not flood the TUI.

Plugin ``settings`` and the ``enabled`` set are captured once at construction
(agent startup) and never reloaded (D11); ``runner_context()`` combines them
with the current policy into the ``context.json`` payload for the runner.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from archie_shared.events import ErrorNotice
from archie_shared.plugins.config import enabled_plugins, policy_snapshot, resolved_settings
from archie_shared.plugins.manifest import LoadedPlugin, load_manifests
from archie_shared.schemas import load_nexus_config
from ulid import ULID

if TYPE_CHECKING:
    from archie_shared.schemas import NexusConfig

RELOAD_FAILED_KIND = "tool_policy_reload_failed"
RECOVERED_KIND = "tool_policy_reloaded"

Snapshot = dict[str, Any]


def load_policy_snapshot() -> Snapshot:
    """Read ``config.yaml`` now and return its detached tool-policy snapshot."""
    return policy_snapshot(load_nexus_config().plugins, load_manifests())


def startup_runner_state(
    config: NexusConfig, manifests: dict[str, LoadedPlugin]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Return ``(settings, enabled)``: resolved settings per enabled plugin, sorted ids."""
    enabled = sorted(enabled_plugins(config.plugins, manifests))
    settings = {pid: resolved_settings(pid, config.plugins, manifests) for pid in enabled}
    return settings, enabled


class LiveToolPolicy:
    """The last valid tool-policy snapshot plus a loader that refreshes it.

    ``settings`` and ``enabled`` are startup-only; ``enabled=None`` means every
    manifest is enabled.
    """

    def __init__(
        self,
        initial: Snapshot,
        *,
        loader: Callable[[], Snapshot] | None = load_policy_snapshot,
        settings: dict[str, dict[str, Any]] | None = None,
        enabled: list[str] | None = None,
    ) -> None:
        self._snapshot = initial or {}
        self._loader = loader
        self._reported_error: str | None = None
        self._settings = dict(settings or {})
        self._enabled = None if enabled is None else list(enabled)

    @classmethod
    def static(cls, snapshot: Snapshot | None) -> LiveToolPolicy:
        """A policy that never reloads."""
        return cls(snapshot or {}, loader=None)

    @property
    def settings(self) -> dict[str, dict[str, Any]]:
        return self._settings

    @property
    def enabled(self) -> list[str] | None:
        return self._enabled

    @property
    def snapshot(self) -> Snapshot:
        """The current (last valid) policy snapshot, without reloading."""
        return self._snapshot

    def runner_context(self, policy: Snapshot | None = None) -> dict[str, Any]:
        """The ``context.json`` payload: ``policy`` (current or given), settings, enabled."""
        return {
            "policy": self._snapshot if policy is None else policy,
            "settings": self._settings,
            "enabled": self._enabled,
        }

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

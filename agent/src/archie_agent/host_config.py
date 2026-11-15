"""The agent's view of the host config, synced from the orchestrator (plan 090).

Agent code never reads ``config.yaml`` or ``credentials.yaml``. :class:`HostConfig`
holds the config and the credentials it has been asked for in memory, and refreshes
them with :meth:`HostConfig.sync`, which sends the held versions and gets back only what
changed (``archie_shared.agent_sync``). A credential that isn't held is fetched on
first read, and an absent one is held as absent, so repeated reads make no request.
A held OAuth credential within :data:`EARLY_SYNC_WINDOW` seconds of its ``expires_at``
is synced when read, at most once per :data:`EARLY_SYNC_SPACING` seconds.

:func:`current` returns the process's instance, building it on first use from
:func:`source_from_env`; the agent lifespan calls it at startup, so a bad environment or
an unreachable orchestrator stops the agent there. The exec runner and other
subprocesses never call it: they have no token (see :func:`source_from_env`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
import msgspec
from archie_shared.agent_sync import (
    ORCHESTRATOR_URL_ENV,
    SYNC_PATH,
    TOKEN_ENV,
    SyncRequest,
    SyncResponse,
)
from archie_shared.config import AGENT_CONTAINER_ENV, ConfigError
from archie_shared.credentials.models import CREDENTIAL_TYPES
from archie_shared.schemas import NexusConfig

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 5.0
"""Seconds an orchestrator request may take before the sync fails."""
SYNC_INTERVAL = 300.0
"""Seconds between the periodic syncs that bring host edits to a running session."""
EARLY_SYNC_WINDOW = 180.0
"""Seconds before a held credential's ``expires_at`` at which a read syncs it (D12)."""
EARLY_SYNC_SPACING = 30.0
"""Minimum seconds between early syncs triggered by reads of one credential."""


class HostConfigError(ValueError):
    """The orchestrator couldn't be asked, or couldn't answer, for config or credentials.

    A ``ValueError`` so callers that handle an invalid stored credential handle this too.
    """


class Source(Protocol):
    """Where syncs are answered: the orchestrator in production, a fake in tests."""

    def sync(self, request: SyncRequest) -> SyncResponse:
        """Answer one sync; raise :class:`HostConfigError` when it can't."""
        ...


class HttpSource:
    """Syncs over ``POST /api/agent/sync`` with the session's bearer token.

    Args:
        base_url: The orchestrator's URL as seen from the container.
        token: The session's agent token.
        client: The HTTP client; tests pass a Starlette ``TestClient``.
    """

    def __init__(self, base_url: str, token: str, *, client: httpx.Client | None = None) -> None:
        self._url = base_url.rstrip("/") + SYNC_PATH
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self._client = client if client is not None else httpx.Client(timeout=REQUEST_TIMEOUT)

    def sync(self, request: SyncRequest) -> SyncResponse:
        try:
            reply = self._client.post(
                self._url, content=msgspec.json.encode(request), headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise HostConfigError(f"can't reach the orchestrator at {self._url}: {exc}") from None
        if reply.status_code != 200:
            raise HostConfigError(
                f"the orchestrator answered the sync with HTTP {reply.status_code}"
            )
        try:
            return msgspec.json.decode(reply.content, type=SyncResponse)
        except (msgspec.DecodeError, msgspec.ValidationError) as exc:
            raise HostConfigError(f"the orchestrator's sync reply can't be read: {exc}") from None


def source_from_env() -> Source:
    """The orchestrator source this container was started with.

    Removes :data:`~archie_shared.agent_sync.TOKEN_ENV` from ``os.environ`` first, so
    shells and other subprocesses never inherit the token.

    Raises:
        HostConfigError: If the container flag, the orchestrator URL or the token is missing.
    """
    token = os.environ.pop(TOKEN_ENV, "").strip()
    if os.environ.get(AGENT_CONTAINER_ENV) != "1":
        raise HostConfigError(
            f"{AGENT_CONTAINER_ENV} is not 1: the agent runs only in a container started by "
            "the orchestrator"
        )
    url = os.environ.get(ORCHESTRATOR_URL_ENV, "").strip()
    if not url:
        raise HostConfigError(
            f"{ORCHESTRATOR_URL_ENV} is not set; restart the orchestrator so new sessions get it"
        )
    if not token:
        raise HostConfigError(
            f"{TOKEN_ENV} is not set; restart the orchestrator so new sessions get it"
        )
    return HttpSource(url, token)


class HostConfig:
    """The host config, held in memory and refreshed by :meth:`sync`.

    Thread-safe: reads take a short data lock; syncs are serialised by their own lock,
    so a slow orchestrator never blocks a read of what is already held.
    """

    def __init__(
        self, source: Source, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._source = source
        self._now = now
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._config: NexusConfig | None = None
        self._config_version: str | None = None
        self._config_error: str | None = None
        # Provider name -> (version, raw store entry or None for absent).
        self._credentials: dict[str, tuple[str, dict[str, Any] | None]] = {}
        self._early_synced: dict[str, datetime] = {}
        self.generation = 0
        """Bumped whenever a held value changes, for caches built on top."""

    def start(self) -> None:
        """Fetch the config for the first time.

        Raises:
            HostConfigError: If the orchestrator can't be asked or doesn't answer.
            ConfigError: If the host config is invalid.
        """
        self.sync()
        with self._lock:
            if self._config is None:
                raise ConfigError(self._config_error or "the orchestrator sent no config")

    def config(self) -> NexusConfig:
        """The last valid config.

        Raises:
            ConfigError: If none has been received (:meth:`start` hasn't succeeded).
        """
        with self._lock:
            if self._config is None:
                raise ConfigError(self._config_error or "no config has been synced yet")
            return self._config

    @property
    def config_error(self) -> str | None:
        """Why the host config was invalid at the last sync, or None if it was valid."""
        with self._lock:
            return self._config_error

    def credential_entry(self, name: str) -> dict[str, Any] | None:
        """The raw stored entry for a provider, or None when nothing is stored.

        Fetched from the orchestrator when it isn't held; a held OAuth entry close to its
        ``expires_at`` triggers an early sync first (D12).

        Raises:
            HostConfigError: If it isn't held and the orchestrator can't send it.
        """
        with self._lock:
            held = self._credentials.get(name)
        if held is None:
            errors = self.sync([name])
            with self._lock:
                held = self._credentials.get(name)
            if held is None:
                reason = errors.get(name, "no reply for it")
                raise HostConfigError(
                    f"the orchestrator can't send the '{name}' credential: {reason}"
                )
            return held[1]
        if self._near_expiry(name, held[1]):
            try:
                self.sync()
            except HostConfigError as exc:
                log.warning(
                    "Early sync for the %s credential failed; using the held one. %s", name, exc
                )
            with self._lock:
                held = self._credentials.get(name, held)
        return held[1]

    def get_credential[T](self, name: str, credential_type: type[T] | None = None) -> T | None:
        """A provider's credential as its typed struct, like the store's ``get_credential``.

        Raises:
            KeyError: If ``name`` isn't a known provider and no type is given.
            ValueError: If the entry doesn't fit the type, or (:class:`HostConfigError`) it
                isn't held and the orchestrator can't send it.
        """
        if credential_type is None:
            if name not in CREDENTIAL_TYPES:
                known = ", ".join(sorted(CREDENTIAL_TYPES))
                raise KeyError(f"Unknown service '{name}'. Known: {known}")
            credential_type = CREDENTIAL_TYPES[name]
        entry = self.credential_entry(name)
        if entry is None:
            return None
        try:
            return msgspec.convert(entry, credential_type, strict=False)
        except msgspec.ValidationError as exc:
            raise ValueError(
                f"Validation error for '{name}' from the orchestrator: {exc}"
            ) from None

    def _near_expiry(self, name: str, entry: dict[str, Any] | None) -> bool:
        """Whether a read of this held entry should sync first, recording the attempt."""
        expires_at = _expiry(entry)
        if expires_at is None:
            return False
        now = self._now()
        if (expires_at - now).total_seconds() >= EARLY_SYNC_WINDOW:
            return False
        with self._lock:
            last = self._early_synced.get(name)
            if last is not None and (now - last).total_seconds() < EARLY_SYNC_SPACING:
                return False
            self._early_synced[name] = now
        return True

    def sync(self, names: Iterable[str] = ()) -> dict[str, str]:
        """Ask the orchestrator what changed since the held versions, and apply it.

        Sends the config version, every held credential's version, and ``names`` that
        aren't held yet. An error for a held credential keeps the held value (D24).

        Returns:
            The orchestrator's errors for the requested names that weren't held.

        Raises:
            HostConfigError: If the orchestrator can't be asked or doesn't answer; held
                values are unchanged.
        """
        with self._sync_lock:
            with self._lock:
                credentials = {name: version for name, (version, _) in self._credentials.items()}
                for name in names:
                    credentials.setdefault(name, None)
                request = SyncRequest(config=self._config_version, credentials=credentials)
            return self._apply(self._source.sync(request), request)

    def _apply(self, response: SyncResponse, request: SyncRequest) -> dict[str, str]:
        with self._lock:
            changed = False
            error = response.config_error
            if response.config is not None:
                try:
                    config = msgspec.convert(response.config.value, NexusConfig)
                except msgspec.ValidationError as exc:
                    error = f"the orchestrator sent a config this agent can't read: {exc}"
                else:
                    self._config = config
                    self._config_version = response.config.version
                    changed = True
            if error != self._config_error:
                if error is not None:
                    log.warning("Host config is invalid; keeping the last valid config. %s", error)
                self._config_error = error
            for name, item in response.credentials.items():
                self._credentials[name] = (item.version, item.value)
                changed = True
            missing: dict[str, str] = {}
            for name, message in response.credential_errors.items():
                if request.credentials.get(name) is not None:
                    log.warning("Keeping the held %s credential: %s", name, message)
                else:
                    missing[name] = message
            if changed:
                self.generation += 1
            return missing


def _expiry(entry: dict[str, Any] | None) -> datetime | None:
    """An entry's ``expires_at`` as an aware UTC datetime, or None if it has none."""
    value = entry.get("expires_at") if isinstance(entry, dict) else None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


_current: HostConfig | None = None
_current_lock = threading.Lock()


def current() -> HostConfig:
    """This process's :class:`HostConfig`, built and started on first use.

    Raises:
        HostConfigError: If the environment or the orchestrator fails (see
            :func:`source_from_env` and :meth:`HostConfig.start`).
        ConfigError: If the host config is invalid on first use.
    """
    global _current
    with _current_lock:
        if _current is None:
            host = HostConfig(source_from_env())
            host.start()
            _current = host
        return _current


def install(host: HostConfig | None) -> None:
    """Make ``host`` this process's instance (None clears it, for tests)."""
    global _current
    with _current_lock:
        _current = host


def reset() -> None:
    """Forget this process's instance, so the next :func:`current` builds a new one."""
    install(None)


def get_config() -> NexusConfig:
    """The last valid host config (see :func:`current` for what can be raised)."""
    return current().config()


def get_credential[T](service: str, credential_type: type[T] | None = None) -> T | None:
    """A provider's credential from this process's :class:`HostConfig`.

    The agent-process replacement for the store's ``get_credential`` (plan 090).
    """
    return current().get_credential(service, credential_type)


def refresh(service: str) -> None:
    """Sync now, including ``service``, after a provider rejected its credential (D19).

    A failed sync is logged; the held value stays in use.
    """
    try:
        current().sync([service])
    except HostConfigError as exc:
        log.warning("Sync after a rejected %s credential failed: %s", service, exc)


async def sync_loop(host: HostConfig, interval: float | None = None) -> None:
    """Sync ``host`` every ``interval`` seconds (default :data:`SYNC_INTERVAL`) until cancelled.

    A failed sync is logged and the held values stay in use until the next one (D24).
    """
    delay = SYNC_INTERVAL if interval is None else interval
    while True:
        await asyncio.sleep(delay)
        try:
            await asyncio.to_thread(host.sync)
        except HostConfigError as exc:
            log.warning("Periodic sync failed; keeping the held config and credentials. %s", exc)
        except Exception:  # noqa: BLE001 — the loop must outlive one bad reply
            log.exception("Periodic sync failed unexpectedly")

"""The agent's view of the host config, synced from the orchestrator (plan 090).

Agent code never reads ``config.yaml`` or ``credentials.yaml``. :class:`HostConfig`
holds the config in memory and refreshes it with :meth:`HostConfig.sync`, which sends
the held version and gets back only what changed (``archie_shared.agent_sync``).

:func:`current` returns the process's instance, building it on first use from
:func:`source_from_env`; the agent lifespan calls it at startup, so a bad environment or
an unreachable orchestrator stops the agent there. The exec runner and other
subprocesses never call it: they have no token (see :func:`source_from_env`).
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Protocol

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
from archie_shared.schemas import NexusConfig

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 5.0
"""Seconds an orchestrator request may take before the sync fails."""


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

    def __init__(self, source: Source) -> None:
        self._source = source
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._config: NexusConfig | None = None
        self._config_version: str | None = None
        self._config_error: str | None = None
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

    def sync(self) -> None:
        """Ask the orchestrator what changed since the held versions, and apply it.

        Raises:
            HostConfigError: If the orchestrator can't be asked or doesn't answer; held
                values are unchanged.
        """
        with self._sync_lock:
            with self._lock:
                request = SyncRequest(config=self._config_version)
            self._apply(self._source.sync(request))

    def _apply(self, response: SyncResponse) -> None:
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
            if changed:
                self.generation += 1


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

"""The TUI's access to an orchestrator: session history, live streams and stop.

``OrchestratorClient`` is the seam the session controller talks through, so tests can
swap in a fake (``tests/tui_fakes.py``). ``HttpOrchestrator`` implements it over the
orchestrator's HTTP API and WebSocket stream for one orchestrator base URL.
"""

from __future__ import annotations

from typing import Protocol

import httpx
from archie_shared.protocol import API_PREFIX

from archie_cli.ws_client import WSClient

EVENTS_TIMEOUT = 5.0
"""Seconds allowed for one ``/events`` history request."""

DELETE_TIMEOUT = 15.0
"""Seconds allowed for stopping a session (the orchestrator stops its container)."""


class OrchestratorError(Exception):
    """An orchestrator request failed: a transport error or an unexpected status.

    Attributes:
        status_code: The HTTP status, or None for a transport error.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class CursorNotFound(OrchestratorError):  # noqa: N818 — names the condition, as HTTP 409 does
    """``/events?after=`` named an event the log no longer has (HTTP 409)."""


class OrchestratorClient(Protocol):
    """What a session controller needs from an orchestrator."""

    async def events(self, session_id: str, after: str | None) -> str:
        """Return the session's persisted events after ``after`` as an NDJSON body.

        Args:
            session_id: The session to read.
            after: Id of the last event already held, or None for the whole log.

        Raises:
            CursorNotFound: ``after`` is not in the log.
            OrchestratorError: The request failed.
        """
        ...

    async def stream(self, session_id: str) -> WSClient:
        """Open the session's live event stream and return the connected client."""
        ...

    async def delete(self, session_id: str) -> int:
        """Stop the session and return the response's status code.

        Raises:
            OrchestratorError: The request could not be completed.
        """
        ...


class HttpOrchestrator:
    """``OrchestratorClient`` over one orchestrator's HTTP API and WebSocket stream."""

    def __init__(self, base_url: str) -> None:
        """Create a client for the orchestrator at ``base_url`` (e.g. ``http://host:7600``)."""
        self.base_url = base_url.rstrip("/")

    def session_url(self, session_id: str) -> str:
        """The HTTP URL of one session's record."""
        return f"{self.base_url}{API_PREFIX}/sessions/{session_id}"

    def stream_url(self, session_id: str) -> str:
        """The WebSocket URL of one session's live stream (``ws``/``wss`` for ``http``/``https``)."""
        scheme, _, rest = self.base_url.partition("://")
        ws_scheme = {"http": "ws", "https": "wss"}.get(scheme, scheme)
        return f"{ws_scheme}://{rest}{API_PREFIX}/sessions/{session_id}/stream"

    async def events(self, session_id: str, after: str | None) -> str:
        """Return persisted events after ``after`` as NDJSON (see ``OrchestratorClient``)."""
        url = f"{self.session_url(session_id)}/events"
        if after:
            url += f"?after={after}"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=EVENTS_TIMEOUT)
        except httpx.HTTPError as exc:
            raise OrchestratorError(str(exc)) from exc
        if response.status_code == 409:
            raise CursorNotFound(f"event {after} is not in the session log", 409)
        if response.status_code != 200:
            raise OrchestratorError(
                f"history request failed: {response.status_code}", response.status_code
            )
        return response.text

    async def stream(self, session_id: str) -> WSClient:
        """Connect a new ``WSClient`` to the session's stream."""
        ws = WSClient()
        await ws.connect(self.stream_url(session_id))
        return ws

    async def delete(self, session_id: str) -> int:
        """Stop the session; any status is returned for the caller to judge."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.delete(self.session_url(session_id), timeout=DELETE_TIMEOUT)
        except httpx.HTTPError as exc:
            raise OrchestratorError(str(exc)) from exc
        return response.status_code

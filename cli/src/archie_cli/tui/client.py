"""The TUI's access to an orchestrator: the sessions feed, history, streams and changes.

``OrchestratorClient`` is the seam the app and the session controllers talk through, so
tests can swap in a fake (``tests/tui_fakes.py``). ``HttpOrchestrator`` implements it
over the orchestrator's HTTP API, its server-sent sessions feed and the WebSocket
stream, for one orchestrator base URL. ``ProfileInfo`` describes that orchestrator to
the app (084 D19, D20, D34).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx
import msgspec
from archie_shared.protocol import API_PREFIX
from archie_shared.session import SessionDescriptor

from archie_cli.ws_client import WSClient

log = logging.getLogger(__name__)

EVENTS_TIMEOUT = 5.0
"""Seconds allowed for one ``/events`` history request."""

DELETE_TIMEOUT = 15.0
"""Seconds allowed for stopping a session (the orchestrator stops its container)."""

PATCH_TIMEOUT = 5.0
"""Seconds allowed for renaming a session or marking it seen."""

SESSION_TIMEOUT = 5.0
"""Seconds allowed for reading one session record."""

FEED_CONNECT_TIMEOUT = 5.0
"""Seconds allowed for connecting to the sessions feed."""

FEED_READ_TIMEOUT = 40.0
"""Seconds without a byte after which the feed counts as dead (the server pings after 15).

This is the dead-feed rule (084 D9): the server's ``: ping`` comments are bytes on the
wire, so a healthy idle feed never reaches it, while a feed whose connection silently
died raises ``OrchestratorError`` and the app reconnects."""


@dataclass(frozen=True)
class ProfileInfo:
    """The orchestrator profile a window talks to (084 D19).

    Attributes:
        label: Name shown in the sidebar: ``local`` for the default profile, else the
            profile's name (or the ``ARCHIE_HOST`` value).
        web_url: The orchestrator's web interface (its ``/`` page).
        local: The orchestrator runs on this machine, so ``!`` commands can reach its
            containers with ``docker exec`` (the default profile with no ``ARCHIE_HOST``).
    """

    label: str
    web_url: str
    local: bool


type FeedEvent = (
    tuple[Literal["snapshot"], list[SessionDescriptor]]
    | tuple[Literal["session"], SessionDescriptor]
)
"""One sessions-feed event: every live record on connect, then one changed record."""


class _Snapshot(msgspec.Struct):
    sessions: list[SessionDescriptor]


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
    """What the app and its session controllers need from an orchestrator."""

    def feed(self) -> AsyncIterator[FeedEvent]:
        """Follow the sessions feed: a ``snapshot`` on connect, then ``session`` events.

        Raises:
            OrchestratorError: The feed could not be opened or was lost.
        """
        ...

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

    async def patch(self, session_id: str, body: dict[str, object]) -> SessionDescriptor:
        """Change a session's ``name`` or clear ``unseen``; return the updated record.

        Raises:
            OrchestratorError: The request failed or was refused.
        """
        ...

    async def session(self, session_id: str) -> SessionDescriptor:
        """Return one session's record (live or ended).

        Raises:
            OrchestratorError: The request failed; ``status_code`` 404 for an unknown ID.
        """
        ...


class HttpOrchestrator:
    """``OrchestratorClient`` over one orchestrator's HTTP API and WebSocket stream."""

    def __init__(self, base_url: str, *, feed_read_timeout: float = FEED_READ_TIMEOUT) -> None:
        """Create a client for the orchestrator at ``base_url`` (e.g. ``http://host:7600``).

        Args:
            base_url: The orchestrator's base URL.
            feed_read_timeout: Seconds of feed silence that count as a dead feed.
        """
        self.base_url = base_url.rstrip("/")
        self.feed_read_timeout = feed_read_timeout

    def session_url(self, session_id: str) -> str:
        """The HTTP URL of one session's record."""
        return f"{self.base_url}{API_PREFIX}/sessions/{session_id}"

    def stream_url(self, session_id: str) -> str:
        """The WebSocket URL of one session's live stream (``ws``/``wss`` for ``http``/``https``)."""
        scheme, _, rest = self.base_url.partition("://")
        ws_scheme = {"http": "ws", "https": "wss"}.get(scheme, scheme)
        return f"{ws_scheme}://{rest}{API_PREFIX}/sessions/{session_id}/stream"

    async def feed(self) -> AsyncIterator[FeedEvent]:
        """Follow ``GET /api/sessions/feed`` (see ``OrchestratorClient``)."""
        url = f"{self.base_url}{API_PREFIX}/sessions/feed"
        timeout = httpx.Timeout(FEED_CONNECT_TIMEOUT, read=self.feed_read_timeout)
        try:
            async with (
                httpx.AsyncClient(timeout=timeout) as client,
                client.stream("GET", url) as response,
            ):
                if response.status_code != 200:
                    raise OrchestratorError(
                        f"sessions feed failed: {response.status_code}", response.status_code
                    )
                async for event in parse_feed(response.aiter_lines()):
                    yield event
        except httpx.HTTPError as exc:
            raise OrchestratorError(str(exc) or type(exc).__name__) from exc

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

    async def patch(self, session_id: str, body: dict[str, object]) -> SessionDescriptor:
        """Send ``PATCH /api/sessions/{id}`` and return the updated record."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.patch(
                    self.session_url(session_id), json=body, timeout=PATCH_TIMEOUT
                )
        except httpx.HTTPError as exc:
            raise OrchestratorError(str(exc)) from exc
        if response.status_code != 200:
            raise OrchestratorError(
                f"session update failed: {response.status_code}", response.status_code
            )
        try:
            return msgspec.json.decode(response.content, type=SessionDescriptor)
        except (msgspec.DecodeError, msgspec.ValidationError) as exc:
            raise OrchestratorError(f"unexpected session record: {exc}") from exc

    async def session(self, session_id: str) -> SessionDescriptor:
        """Send ``GET /api/sessions/{id}`` and return the record."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(self.session_url(session_id), timeout=SESSION_TIMEOUT)
        except httpx.HTTPError as exc:
            raise OrchestratorError(str(exc)) from exc
        if response.status_code != 200:
            raise OrchestratorError(
                f"session request failed: {response.status_code}", response.status_code
            )
        try:
            return msgspec.json.decode(response.content, type=SessionDescriptor)
        except (msgspec.DecodeError, msgspec.ValidationError) as exc:
            raise OrchestratorError(f"unexpected session record: {exc}") from exc


async def parse_feed(lines: AsyncIterable[str]) -> AsyncIterator[FeedEvent]:
    """Parse server-sent sessions-feed lines into feed events.

    ``event:`` and ``data:`` fields collect until a blank line ends the event. Comment
    lines (``: ping``) and unknown event names are skipped, and so is an event whose
    data does not decode (logged).
    """
    name = ""
    data: list[str] = []
    async for line in lines:
        if line.startswith(":"):
            continue
        if line:
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "event":
                name = value
            elif field == "data":
                data.append(value)
            continue
        payload = "\n".join(data)
        kind, name, data = name, "", []
        if not payload:
            continue
        try:
            if kind == "snapshot":
                yield "snapshot", msgspec.json.decode(payload, type=_Snapshot).sessions
            elif kind == "session":
                yield "session", msgspec.json.decode(payload, type=SessionDescriptor)
        except (msgspec.DecodeError, msgspec.ValidationError) as exc:
            log.warning("Skipping malformed %s feed event: %s", kind, exc)

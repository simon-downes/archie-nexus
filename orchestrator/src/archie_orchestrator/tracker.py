"""Session tracker: the orchestrator's live view of every session.

:class:`SessionTracker` owns every write to the ``sessions`` table. It creates the row on
``POST /api/sessions``, runs the container start in the background, follows each running
agent's ``/stream`` read-only (as the live viewer does) and folds the events into the
record (082 D10). Records of sessions that have not ended are kept in memory and are
authoritative (D12a); reads of ended sessions go to the store. Every change is passed to
the registered listeners; changes to ``last_activity_at`` alone are throttled (D11).
:meth:`SessionTracker.subscribe_feed` queues those changes for one sessions-feed client.

At start the stored records are reconciled with ``docker ps`` (D13), the port of a
container listed without one is looked up again until it appears, and a closed stream
is followed again while the container runs, or ends the session when it has gone (D14).
:meth:`SessionTracker.stop` stops a container for ``DELETE`` (D19, D19a) and
:meth:`SessionTracker.patch` renames a session or marks it seen for ``PATCH`` (D18).

Sessions the user hasn't named get titles from a :class:`Titler` (plan 083): on the first
root prompt, at the end of root turns 1, 3, 8, 15 and every 10 after, and at every root
turn end while the session still has only its ID. Viewing a session that still has only
its ID (:meth:`SessionTracker.title_if_untitled`) and resetting a name also request one.
One title job runs per session at a time, later requests coalesce into one rerun, and at
most :data:`TITLE_CONCURRENCY` calls run at once.

Docker is reached only through a :class:`Runtime`, so tests can substitute a fake.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import msgspec
import websockets
from archie_shared.events import (
    ErrorNotice,
    Handshake,
    SessionStatus,
    TurnComplete,
    TurnError,
    TurnInterrupted,
    UserMessage,
    decode_event,
)
from archie_shared.kiro import KIRO_MODEL_PREFIX
from archie_shared.schemas import NexusConfig
from archie_shared.session import SessionDescriptor, container_name, split_id
from websockets.exceptions import WebSocketException

from archie_orchestrator import docker, lifecycle
from archie_orchestrator.docker import DockerError, RunningContainer
from archie_orchestrator.lifecycle import PreparedStart
from archie_orchestrator.store import SessionStore, id_start_time, import_log, now_iso
from archie_orchestrator.titles import TitleInput, TitleResult, read_title_input

log = logging.getLogger(__name__)

RESUBSCRIBE_DELAYS = (1.0, 2.0, 5.0)
"""Seconds before following a stream again after it closed while the container runs (D14).

The last delay repeats for as long as the container runs; a stream that delivered a frame
starts the sequence again.
"""
TERMINAL_RETRY_INTERVAL = 5.0
"""Seconds between retries of a failed terminal write (D12a)."""
RECONCILE_RETRY_INTERVAL = 30.0
"""Seconds between reconcile attempts while ``docker ps`` fails, and between port lookups
for sessions whose container was listed without a port (D13)."""
ACTIVITY_INTERVAL = 10.0
"""Minimum seconds between writes of one session that change only ``last_activity_at`` (D11)."""
RESTARTED_DURING_STARTUP = "orchestrator restarted during startup"
"""The ``error`` of a session that was ``starting`` when the orchestrator stopped (D13)."""
FEED_QUEUE_SIZE = 256
"""Records a feed subscriber may fall behind before it is dropped."""

TITLE_TURNS = frozenset({1, 3, 8, 15})
"""Root turns whose end refreshes the title; after turn 15, every 10th turn does (083 D3)."""
TITLE_EVERY = 10
TITLE_CONCURRENCY = 2
"""Most title calls running at once, across all sessions (083 D14)."""
TITLE_VIEW_COOLDOWN = 300.0
"""Seconds after a failed title call before viewing the session tries again (083 D15)."""

_UNSET: object = object()
"""Default of :meth:`SessionTracker.patch` arguments that were not given."""

_STREAM_ERRORS = (OSError, TimeoutError, WebSocketException)
# Events that are not agent activity: connect frames, status and targeted notices.
_NOT_ACTIVITY = (Handshake, SessionStatus, ErrorNotice)


def title_due(turn: int) -> bool:
    """Whether the end of root turn ``turn`` refreshes a session's title (083 D3)."""
    return turn in TITLE_TURNS or (turn > 15 and (turn - 15) % TITLE_EVERY == 0)


class SessionTitler(Protocol):
    """What the tracker needs from :class:`archie_orchestrator.titles.Titler`."""

    def enabled(self) -> bool: ...

    def generate(self, session_id: str, title_input: TitleInput) -> TitleResult: ...


def resubscribe_delay(attempt: int) -> float:
    """The back-off before resubscribe attempt ``attempt`` (0-based) of one session (D14)."""
    return RESUBSCRIBE_DELAYS[min(attempt, len(RESUBSCRIBE_DELAYS) - 1)]


class SessionConflictError(Exception):
    """The session's status doesn't allow the operation (``starting``, ``stopping`` or ended)."""


class FeedSubscription:
    """One sessions-feed client: every record the tracker publishes, in order, until closed.

    Created by :meth:`SessionTracker.subscribe_feed`; use it as a context manager or call
    :meth:`close`. A subscriber that falls ``maxsize`` records behind is dropped: its
    backlog is discarded, :meth:`get` returns None and the client is expected to reconnect
    to a fresh snapshot.
    """

    def __init__(self, tracker: SessionTracker, maxsize: int) -> None:
        self._tracker = tracker
        self._queue: asyncio.Queue[SessionDescriptor | None] = asyncio.Queue(maxsize=maxsize)
        self.dropped = False

    def __enter__(self) -> FeedSubscription:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    async def get(self) -> SessionDescriptor | None:
        """The next published record, or None once the subscriber has been dropped."""
        return await self._queue.get()

    def close(self) -> None:
        """Stop receiving records. Safe to call more than once."""
        self._tracker.remove_listener(self._offer)

    def _offer(self, record: SessionDescriptor) -> None:
        """The tracker listener: queue ``record``, or drop the subscriber when full."""
        if self.dropped:
            return
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self._drop()

    def _drop(self) -> None:
        log.warning("Sessions feed subscriber fell %d records behind; dropped", self._queue.maxsize)
        self.dropped = True
        self.close()
        # Replace the backlog with the end marker so the reader stops at once.
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)


# ---------------------------------------------------------------------------
# Docker access
# ---------------------------------------------------------------------------


class Runtime(Protocol):
    """The container operations the tracker needs. Every method is synchronous."""

    def list_running(self) -> list[RunningContainer]:
        """Running archie containers; raises ``DockerError`` or ``OSError`` on failure."""
        ...

    def prepare(self, workspace: str, config: NexusConfig, backend: str) -> PreparedStart:
        """Validate a start (``ValueError``/``RuntimeError`` on bad input or no image)."""
        ...

    def run(self, prepared: PreparedStart) -> int:
        """Run a prepared start and wait until the agent is ready; return its port."""
        ...

    def is_running(self, session_id: str) -> bool:
        """Whether the session's container is running."""
        ...

    def stop(self, session_id: str) -> bool:
        """Stop the session's container; return whether its prompt-less log was removed."""
        ...


class DockerRuntime:
    """The :class:`Runtime` backed by the Docker CLI."""

    def list_running(self) -> list[RunningContainer]:
        return docker.list_sessions()

    def prepare(self, workspace: str, config: NexusConfig, backend: str) -> PreparedStart:
        return lifecycle.prepare_start(workspace, config, backend=backend)

    def run(self, prepared: PreparedStart) -> int:
        return lifecycle.run_start(prepared)

    def is_running(self, session_id: str) -> bool:
        return docker.container_running(container_name(session_id))

    def stop(self, session_id: str) -> bool:
        """``docker stop`` by name (no ``docker ps``), then drop a log with no prompt (D19)."""
        return lifecycle.stop_session(session_id)


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


def _newest_first(records) -> list[SessionDescriptor]:
    return sorted(records, key=lambda r: (r.started_at, r.session_id), reverse=True)


class SessionTracker:
    """Keeps session records current and serves them to the API.

    Args:
        store: The migrated (or about to be, by :meth:`start`) session store.
        runtime: Container operations; :class:`DockerRuntime` in production.
        activity_interval: Minimum seconds between activity-only writes of one session.
        clock: Monotonic seconds, used to space activity-only writes.
        titler: Writes session titles; None turns titles off.
    """

    def __init__(
        self,
        store: SessionStore,
        runtime: Runtime,
        *,
        activity_interval: float = ACTIVITY_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
        titler: SessionTitler | None = None,
    ) -> None:
        self.store = store
        self.titler = titler
        self.runtime = runtime
        self.activity_interval = activity_interval
        self.clock = clock
        # Every record that hasn't ended, plus ended ones whose terminal write is pending.
        self._records: dict[str, SessionDescriptor] = {}
        self._ports: dict[str, int] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._background: set[asyncio.Task] = set()
        self._write_locks: dict[str, asyncio.Lock] = {}
        self._listeners: list[Callable[[SessionDescriptor], None]] = []
        # Stored records not yet checked against ``docker ps`` (D13).
        self._unreconciled: set[str] = set()
        self._reconciled = False
        # Live sessions whose container was listed without a published port (D13).
        self._portless: set[str] = set()
        # Sessions whose last root turn failed: their next ``idle`` is ``error`` (D10).
        self._turn_failed: set[str] = set()
        # Activity not yet written, the clock time activity was last written, and the
        # trailing writes scheduled for the end of the window (D11).
        self._pending_activity: dict[str, str] = {}
        self._activity_written: dict[str, float] = {}
        self._flush_tasks: dict[str, asyncio.Task] = {}
        # Stopped sessions whose log was removed: their terminal write deletes the row (D19a).
        self._discard: set[str] = set()
        # Title jobs by session, sessions asked for a title while their job ran, and the
        # clock time of each untitled session's last failed title call (083 D14, D15).
        self._title_tasks: dict[str, asyncio.Task] = {}
        self._title_again: set[str] = set()
        self._title_failed_at: dict[str, float] = {}
        self._title_slots = asyncio.Semaphore(TITLE_CONCURRENCY)

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Migrate the store, load live records, reconcile with Docker and follow sessions.

        If ``docker ps`` fails, the stored records are kept as they are and reconcile is
        retried every :data:`RECONCILE_RETRY_INTERVAL` seconds until it succeeds. A
        container listed without a port has its port looked up again at the same
        interval until it has one or has gone.
        """
        await asyncio.to_thread(self.store.migrate)
        for record in await asyncio.to_thread(self.store.list, False):
            self._records[record.session_id] = record
        self._unreconciled = set(self._records)
        if not await self._reconcile() or self._portless:
            self._background_task(self._retry_reconcile())

    async def close(self) -> None:
        """Stop every background task and write what is pending; statuses are kept.

        Sessions run on without the orchestrator, so nothing is marked ended: followed
        streams are cancelled without the end check (D14), pending activity is written
        (D11) and pending terminal writes get one last attempt (D12a).
        """
        tasks = [*self._tasks.values(), *self._background, *self._flush_tasks.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._background.clear()
        self._flush_tasks.clear()
        for session_id in list(self._pending_activity):
            await self._update(session_id)
        for session_id, record in list(self._records.items()):
            if record.ended:
                await self._persist(session_id)

    async def _reconcile(self) -> bool:
        """Match the stored records against ``docker ps`` (D13); False if Docker failed.

        A running container makes its record live (``idle``, ``ended_at`` and ``error``
        cleared, followed when it has a port, else noted for :meth:`_refresh_ports`),
        adopting containers with no row. A stored record that is
        still live with no container ends: ``stopping`` becomes ``stopped``, ``starting``
        becomes ``failed`` and anything else ``exited``, with ``ended_at`` the log's mtime
        (or now). Records this tracker created or already revived are left alone, so a
        retry after a Docker failure can't end a session that started meanwhile. On a
        Docker failure nothing is ended, since "cannot list" is not "nothing runs".
        """
        try:
            running = await asyncio.to_thread(self.runtime.list_running)
        except (DockerError, OSError) as exc:
            log.warning("Cannot list running sessions: %s", exc)
            return False
        found: set[str] = set()
        for container in running:
            session_id = container.session_id
            found.add(session_id)
            if session_id in self._records and session_id not in self._unreconciled:
                continue
            record = self._records.get(session_id)
            if record is None:
                record = await asyncio.to_thread(self.store.get, session_id)
            if record is None:
                record = await asyncio.to_thread(self._adopt, session_id)
                log.info("Adopted running session %s", session_id)
            self._unreconciled.discard(session_id)
            if container.port is not None:
                self._ports[session_id] = container.port
            else:
                self._portless.add(session_id)
            revived = msgspec.structs.replace(record, status="idle", ended_at=None, error=None)
            await self._put(revived)
            if session_id in self._ports and session_id not in self._tasks:
                self._spawn(session_id, self._follow(session_id))
        for session_id in sorted(self._unreconciled - found):
            record = self._records.get(session_id)
            if record is None or record.ended:
                continue
            ended_at = await asyncio.to_thread(self._log_mtime, session_id) or now_iso()
            if record.status == "stopping":
                changes: dict[str, object] = {"status": "stopped"}
            elif record.status == "starting":
                changes = {"status": "failed", "error": RESTARTED_DURING_STARTUP}
            else:
                changes = {"status": "exited"}
            log.info("Session %s has no container: %s", session_id, changes["status"])
            await self._update(session_id, **changes, ended_at=ended_at)
        self._unreconciled.clear()
        self._reconciled = True
        return True

    async def _retry_reconcile(self) -> None:
        """Reconcile until ``docker ps`` succeeds, then look up ports until none is missing.

        Each attempt waits :data:`RECONCILE_RETRY_INTERVAL` seconds (D13).
        """
        while True:
            await asyncio.sleep(RECONCILE_RETRY_INTERVAL)
            if not self._reconciled:
                if not await self._reconcile():
                    continue
                log.info("Reconciled sessions with Docker")
            else:
                await self._refresh_ports()
            if not self._portless:
                return

    async def _refresh_ports(self) -> None:
        """Look up the ports of live sessions whose container was listed without one.

        A container that now has a port is followed. One that is no longer listed ends
        the session as ``exited`` with ``ended_at`` the log's mtime (or now), as reconcile
        would (D13). ``stopping`` sessions are left to the stop; nothing changes when
        ``docker ps`` fails.
        """
        try:
            running = await asyncio.to_thread(self.runtime.list_running)
        except (DockerError, OSError) as exc:
            log.warning("Cannot list running sessions: %s", exc)
            return
        ports = {container.session_id: container.port for container in running}
        for session_id in sorted(self._portless):
            record = self._records.get(session_id)
            if record is None or record.ended or session_id in self._ports:
                self._portless.discard(session_id)
                continue
            if record.status == "stopping":
                continue  # the stop records the outcome; a failed stop leaves it running
            if session_id not in ports:
                ended_at = await asyncio.to_thread(self._log_mtime, session_id) or now_iso()
                record = self._records.get(session_id)
                if record is None or record.ended or record.status == "stopping":
                    continue  # stopped meanwhile; checked again on the next pass
                self._portless.discard(session_id)
                log.info("Session %s has no container: exited", session_id)
                await self._update(session_id, status="exited", ended_at=ended_at)
            elif ports[session_id] is not None:
                self._portless.discard(session_id)
                self._ports[session_id] = ports[session_id]
                log.info("Session %s published port %d", session_id, ports[session_id])
                if session_id not in self._tasks:
                    self._spawn(session_id, self._follow(session_id))

    def log_path(self, session_id: str) -> Path:
        """Where a session's event log lives: ``<home>/sessions/<id>.jsonl``."""
        return self.store.db.parent / "sessions" / f"{session_id}.jsonl"

    def _log_mtime(self, session_id: str) -> str | None:
        """The session log's mtime as an ISO 8601 UTC string, or None without a log."""
        try:
            mtime = self.log_path(session_id).stat().st_mtime
        except OSError:
            return None
        return datetime.fromtimestamp(mtime, UTC).isoformat()

    def _adopt(self, session_id: str) -> SessionDescriptor:
        """A record for a running container with no row: from its log, else from its ID."""
        path = self.log_path(session_id)
        record = import_log(path) if path.is_file() else None
        if record is not None:
            return record
        return SessionDescriptor(
            session_id=session_id,
            workspace=split_id(session_id)[0],
            backend="native",
            name=session_id,
            status="idle",
            started_at=id_start_time(session_id) or now_iso(),
        )

    # -- reads ---------------------------------------------------------------

    def live(self, session_id: str) -> SessionDescriptor | None:
        """The in-memory record of a session that hasn't ended, or None."""
        record = self._records.get(session_id)
        return record if record is not None and not record.ended else None

    def live_ids(self) -> set[str]:
        """IDs of every session that hasn't ended."""
        return {sid for sid, record in self._records.items() if not record.ended}

    def port(self, session_id: str) -> int | None:
        """The agent's host port for a live session, or None (unknown or still starting)."""
        return self._ports.get(session_id) if self.live(session_id) is not None else None

    async def get(self, session_id: str) -> SessionDescriptor | None:
        """One record: from memory when held there, else from the store."""
        record = self._records.get(session_id)
        if record is not None:
            return record
        return await asyncio.to_thread(self.store.get, session_id)

    async def list(self, include_ended: bool = False) -> list[SessionDescriptor]:
        """Records newest first: live ones from memory, ended ones from the store."""
        if not include_ended:
            return _newest_first(r for r in self._records.values() if not r.ended)
        merged = {r.session_id: r for r in await asyncio.to_thread(self.store.list, True)}
        merged.update(self._records)
        return _newest_first(merged.values())

    async def last_started_by_workspace(self) -> dict[str, str]:
        """The newest ``started_at`` per workspace, over stored and in-memory records.

        A just-created record is in memory before its row is written, so both count.
        """
        latest = await asyncio.to_thread(self.store.last_started_by_workspace)
        for record in self._records.values():
            if record.started_at > latest.get(record.workspace, ""):
                latest[record.workspace] = record.started_at
        return latest

    # -- listeners -----------------------------------------------------------

    def add_listener(self, listener: Callable[[SessionDescriptor], None]) -> None:
        """Call ``listener`` with the full record after every change."""
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[SessionDescriptor], None]) -> None:
        """Stop calling a listener added with :meth:`add_listener`."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def subscribe_feed(self, maxsize: int = FEED_QUEUE_SIZE) -> FeedSubscription:
        """Start queueing every published record for one feed client (D20).

        Subscribe before reading the snapshot: a change made while the snapshot is read
        is then queued as well, so none is lost (a client may see a record twice).
        """
        subscription = FeedSubscription(self, maxsize)
        self.add_listener(subscription._offer)
        return subscription

    def _publish(self, record: SessionDescriptor) -> None:
        for listener in list(self._listeners):
            try:
                listener(record)
            except Exception:  # noqa: BLE001 — one bad listener must not stop the others
                log.exception("Session listener failed for %s", record.session_id)

    # -- create --------------------------------------------------------------

    async def create(
        self, workspace: str, *, backend: str, name: str | None, config: NexusConfig
    ) -> SessionDescriptor:
        """Record a new ``starting`` session and start its container in the background.

        Args:
            workspace: Workspace directory name.
            backend: ``native`` or ``kiro``.
            name: User-chosen name (already validated and trimmed), or None.
            config: Config as of the request.

        Returns:
            The ``starting`` record.

        Raises:
            ValueError: Bad workspace or backend (nothing is recorded).
            RuntimeError: The image is missing.
            sqlite3.Error: The row could not be inserted (nothing is started).
        """
        prepared = await asyncio.to_thread(self.runtime.prepare, workspace, config, backend)
        session_id = prepared.session_id
        record = SessionDescriptor(
            session_id=session_id,
            workspace=split_id(session_id)[0],
            backend="kiro" if backend == "kiro" else "native",
            name=name if name is not None else session_id,
            name_source="user" if name is not None else "id",
            status="starting",
            started_at=now_iso(),
        )
        await asyncio.to_thread(self.store.insert, record)
        self._records[session_id] = record
        self._publish(record)
        self._spawn(session_id, self._start(prepared))
        return record

    async def _start(self, prepared: PreparedStart) -> None:
        session_id = prepared.session_id
        try:
            port = await asyncio.to_thread(self.runtime.run, prepared)
        except Exception as exc:  # noqa: BLE001 — any start failure is recorded on the session
            log.warning("Session %s failed to start: %s", session_id, exc)
            await self._update(
                session_id,
                status="failed",
                error=str(exc) or type(exc).__name__,
                ended_at=now_iso(),
            )
            return
        self._ports[session_id] = port
        await self._follow(session_id)

    # -- stop and patch ------------------------------------------------------

    async def stop(self, session_id: str) -> SessionDescriptor:
        """Stop a running session and return its ``stopped`` record (D19).

        The record is ``stopping`` (written and published) while the container stops. If
        the stop removed a log with no prompt, the row is deleted after the ``stopped``
        record is published (D19a), so later reads return 404. If ``docker stop`` fails
        but the container has gone, the session is still ``stopped``; if it still runs,
        the previous status is restored, the stream followed again if it ended, and the
        error raised.

        Raises:
            KeyError: No session with that ID.
            SessionConflictError: The session is ``starting``, ``stopping`` or has ended.
            sqlite3.Error: The store could not be read to tell an ended session from an
                unknown one.
            Exception: Whatever the runtime's stop raised, with the container still running.
        """
        record = self._records.get(session_id)
        if record is None or record.ended:
            # Only ended sessions are outside memory (D12a); the store tells them from unknown.
            ended = record or await asyncio.to_thread(self.store.get, session_id)
            if ended is None:
                raise KeyError(session_id)
            raise SessionConflictError(f"Session '{session_id}' has already ended ({ended.status})")
        if record.status == "starting":
            raise SessionConflictError(f"Session '{session_id}' is still starting")
        if record.status == "stopping":
            raise SessionConflictError(f"Session '{session_id}' is already stopping")
        previous = record.status
        await self._update(session_id, status="stopping")
        try:
            log_removed = await asyncio.to_thread(self.runtime.stop, session_id)
        except Exception as exc:
            if await self._container_alive(session_id):
                log.warning("Cannot stop session %s: %s", session_id, exc)
                await self._update(session_id, status=previous)
                task = self._tasks.get(session_id)
                if session_id in self._ports and (task is None or task.done()):
                    self._spawn(session_id, self._follow(session_id))
                raise
            log.info("Session %s stop failed but its container has gone: %s", session_id, exc)
            log_removed = False
        task = self._tasks.get(session_id)
        if task is not None and not task.done():
            task.cancel()  # the stream is over; nothing it sends changes the record now
        if log_removed:
            self._discard.add(session_id)
        stopped = await self._update(session_id, status="stopped", ended_at=now_iso())
        if stopped is None:  # pragma: no cover - only the stop ends a stopping record
            stopped = msgspec.structs.replace(record, status="stopped", ended_at=now_iso())
        return stopped

    async def _container_alive(self, session_id: str) -> bool:
        """After a failed stop: whether the container still runs (True when Docker can't tell)."""
        try:
            return await asyncio.to_thread(self.runtime.is_running, session_id)
        except (DockerError, OSError) as exc:
            log.warning("Cannot check whether session %s runs: %s", session_id, exc)
            return True

    async def patch(
        self, session_id: str, *, name: object = _UNSET, unseen: bool | None = None
    ) -> SessionDescriptor:
        """Rename a session or mark it seen, live or ended (D18), and publish the record.

        Args:
            session_id: The session.
            name: A validated, trimmed name (``name_source`` becomes ``user``), or None
                to reset the name to the session ID (``name_source`` ``id``) and request
                a title; leave out to keep the name.
            unseen: The new ``unseen`` flag; None keeps it.

        Returns:
            The updated record.

        Raises:
            KeyError: No session with that ID.
            sqlite3.Error: The change could not be written. A record held in memory keeps
                the change (D12a) and the next write retries it.
        """
        changes: dict[str, object] = {}
        if name is not _UNSET:
            changes["name"] = session_id if name is None else name
            changes["name_source"] = "user" if name is not None else "id"
        if unseen is not None:
            changes["unseen"] = unseen
        updated = None
        if session_id in self._records:
            updated = await self._update(session_id, strict=True, **changes)
        if updated is None:
            # Ended sessions are not held in memory: change the stored row directly.
            updated = await asyncio.to_thread(self.store.update, session_id, **changes)
            self._publish(updated)
        if name is None:
            self.request_title(session_id)  # the model may name it again (083 D9)
        return updated

    # -- following the agent stream ------------------------------------------

    async def _follow(self, session_id: str) -> None:
        """Fold the agent's ``/stream`` into the record until the session ends.

        When the stream closes or can't be opened, the session ends if its container has
        gone; otherwise it is followed again after :func:`resubscribe_delay` (D14).
        Cancellation (:meth:`close`) skips that check.
        """
        attempt = 0
        while True:
            port = self._ports.get(session_id)
            if port is None:
                return
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/stream", open_timeout=10
                ) as backend:
                    log.info("Tracking session %s", session_id)
                    async for frame in backend:
                        attempt = 0
                        if isinstance(frame, str):
                            await self._on_frame(session_id, frame)
            except _STREAM_ERRORS as exc:
                log.info("Session %s stream closed: %s", session_id, exc)
            if not await self._still_running(session_id):
                return
            await asyncio.sleep(resubscribe_delay(attempt))
            attempt += 1

    async def _on_frame(self, session_id: str, frame: str) -> None:
        """Apply one agent event to the record (D10) and note activity (D11).

        Only root events (``scope is None``) change the status: a ``user_message`` starts
        work, ``turn_complete`` and ``turn_error`` set ``unseen``, and after a
        ``turn_error`` the next ``idle`` is stored as ``error`` until the next
        ``user_message``; ``turn_interrupted`` drops that pending error. ``session_status``
        sets status, model and question, and a ``kiro/`` model marks the backend. The
        turn-1 ``user_message`` and scheduled ``turn_complete`` events request a title.
        """
        try:
            event = decode_event(frame)
        except (msgspec.DecodeError, msgspec.ValidationError, ValueError) as exc:
            log.debug("Tracker: skipping undecodable frame for %s: %s", session_id, exc)
            return
        record = self._records.get(session_id)
        if record is None or record.ended:
            return
        root = getattr(event, "scope", "") is None
        changes: dict[str, object] = {}
        if isinstance(event, SessionStatus):
            status = "idle" if event.state == "done" else event.state
            if status == "idle" and session_id in self._turn_failed:
                status = "error"
            changes = {"status": status, "model_key": event.model_key, "question": event.question}
            if event.model_key.startswith(KIRO_MODEL_PREFIX):
                changes["backend"] = "kiro"
        elif root and isinstance(event, UserMessage):
            self._turn_failed.discard(session_id)
            changes = {"status": "working"}
        elif root and isinstance(event, TurnError):
            self._turn_failed.add(session_id)
            changes = {"unseen": True}
        elif root and isinstance(event, TurnComplete):
            changes = {"unseen": True}
        elif root and isinstance(event, TurnInterrupted):
            self._turn_failed.discard(session_id)
        if record.status == "stopping":
            changes.pop("status", None)  # the stop decides the next status (D19)
        changes = {key: value for key, value in changes.items() if getattr(record, key) != value}
        if not isinstance(event, _NOT_ACTIVITY):
            self._pending_activity[session_id] = now_iso()
        if changes:
            await self._update(session_id, **changes)
        elif session_id in self._pending_activity:
            await self._note_activity(session_id)
        if root and isinstance(event, UserMessage) and event.turn == 1:
            self.request_title(session_id)
        elif root and isinstance(event, TurnComplete):
            current = self._records.get(session_id, record)
            if title_due(event.turn) or current.name_source == "id":
                self.request_title(session_id)

    async def _still_running(self, session_id: str) -> bool:
        """After the stream closed: True to follow again; False (``exited`` if gone) to stop."""
        record = self._records.get(session_id)
        if record is None or record.ended or record.status == "stopping":
            return False
        try:
            running = await asyncio.to_thread(self.runtime.is_running, session_id)
        except (DockerError, OSError) as exc:
            log.warning("Cannot check whether session %s runs: %s", session_id, exc)
            return True
        if running:
            return True
        record = self._records.get(session_id)
        if record is None or record.ended or record.status == "stopping":
            return False  # stopped meanwhile: the stop records the outcome
        log.info("Session %s exited", session_id)
        await self._update(session_id, status="exited", ended_at=now_iso())
        return False

    # -- titles --------------------------------------------------------------

    def request_title(self, session_id: str) -> None:
        """Start a title job for the session, or have the running one run again."""
        if self.titler is None:
            return
        running = self._title_tasks.get(session_id)
        if running is not None and not running.done():
            self._title_again.add(session_id)
            return
        task = asyncio.create_task(self._title_job(session_id), name=f"title-{session_id}")
        self._title_tasks[session_id] = task
        self._background.add(task)

        def _done(done: asyncio.Task) -> None:
            self._background.discard(done)
            if self._title_tasks.get(session_id) is done:
                del self._title_tasks[session_id]

        task.add_done_callback(_done)

    def title_if_untitled(self, session_id: str) -> None:
        """Title a viewed session that still has only its ID (083 D4); returns at once.

        Nothing happens while a title job runs for the session, or within
        :data:`TITLE_VIEW_COOLDOWN` seconds of its last failed call. The record is read
        (from memory, else the store) in a background task, so a page or API response
        never waits for it.
        """
        if self.titler is None:
            return
        running = self._title_tasks.get(session_id)
        if running is not None and not running.done():
            return
        failed = self._title_failed_at.get(session_id)
        if failed is not None and self.clock() - failed < TITLE_VIEW_COOLDOWN:
            return
        self._background_task(self._title_if_untitled(session_id))

    async def _title_if_untitled(self, session_id: str) -> None:
        try:
            record = await self.get(session_id)
        except sqlite3.Error as exc:
            log.debug("No title for viewed session %s: %s", session_id, exc)
            return
        if record is not None and record.name_source == "id":
            self.request_title(session_id)

    async def _title_job(self, session_id: str) -> None:
        while True:
            self._title_again.discard(session_id)
            try:
                await self._title_once(session_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a failed title must not stop the tracker
                log.exception("Title job for session %s failed", session_id)
            if session_id not in self._title_again:
                return

    async def _title_once(self, session_id: str) -> None:
        """One title call for a session the user hasn't named (083 data flow, step 2)."""
        titler = self.titler
        if titler is None or not titler.enabled():
            return
        record = await self.get(session_id)
        if record is None or record.name_source == "user":
            return
        current = record.name if record.name_source == "model" else None
        try:
            title_input = await asyncio.to_thread(
                read_title_input, self.log_path(session_id), record.workspace, current
            )
        except OSError as exc:
            log.debug("No title for session %s: cannot read its log: %s", session_id, exc)
            return
        if title_input is None:
            return
        async with self._title_slots:
            result = await asyncio.to_thread(titler.generate, session_id, title_input)
        if result.title is None:
            if not result.keep and current is None:
                self._title_failed_at[session_id] = self.clock()
            return
        self._title_failed_at.pop(session_id, None)
        await self._apply_title(session_id, result.title)

    async def _apply_title(self, session_id: str, title: str) -> None:
        """Write a model title unless the user has named the session meanwhile (083 D17)."""
        record = self._records.get(session_id)
        if record is not None:
            # No await between this check and the in-memory change in ``_update``.
            if record.name_source == "user" or (
                record.name_source == "model" and record.name == title
            ):
                return
            await self._update(session_id, name=title, name_source="model")
            return
        updated = await asyncio.to_thread(self.store.set_model_name, session_id, title)
        if updated is not None:
            self._publish(updated)

    # -- writes --------------------------------------------------------------

    async def _update(
        self, session_id: str, *, strict: bool = False, **changes: object
    ) -> SessionDescriptor | None:
        """Apply ``changes`` plus any pending activity to the live record, then write it.

        With ``strict`` a failed write raises ``sqlite3.Error`` (the record stays changed in
        memory); otherwise it is logged and retried later (D12a).
        """
        record = self._records.get(session_id)
        if record is None:
            return None
        activity = self._pending_activity.pop(session_id, None)
        if activity is not None:
            changes.setdefault("last_activity_at", activity)
            flush = self._flush_tasks.pop(session_id, None)
            if flush is not None:
                flush.cancel()
        if "last_activity_at" in changes:
            self._activity_written[session_id] = self.clock()
        return await self._put(msgspec.structs.replace(record, **changes), strict=strict)

    async def _note_activity(self, session_id: str) -> None:
        """Write pending activity now, or at the end of the session's window (D11)."""
        if session_id in self._flush_tasks:
            return
        written = self._activity_written.get(session_id)
        wait = 0.0 if written is None else written + self.activity_interval - self.clock()
        if wait <= 0:
            await self._update(session_id)
            return
        self._flush_tasks[session_id] = asyncio.create_task(
            self._flush_activity(session_id, wait), name=f"activity-{session_id}"
        )

    async def _flush_activity(self, session_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        self._flush_tasks.pop(session_id, None)
        if session_id in self._pending_activity:
            await self._update(session_id)

    async def _put(self, record: SessionDescriptor, *, strict: bool = False) -> SessionDescriptor:
        """Make ``record`` current in memory, publish it and write it to the store.

        A failed terminal write is retried in the background; with ``strict`` the error is
        raised as well.
        """
        session_id = record.session_id
        self._records[session_id] = record
        if record.ended:
            self._ports.pop(session_id, None)
            self._portless.discard(session_id)
            self._turn_failed.discard(session_id)
            self._activity_written.pop(session_id, None)
        self._publish(record)
        try:
            written = await self._persist(session_id, strict=strict)
        except sqlite3.Error:
            if record.ended:
                self._background_task(self._retry_terminal(session_id))
            raise
        if not written and record.ended:
            self._background_task(self._retry_terminal(session_id))
        return record

    async def _persist(self, session_id: str, *, strict: bool = False) -> bool:
        """Write the current record; drop an ended one from memory once written.

        The terminal write of a session in ``_discard`` deletes its row instead (D19a).
        A failed write returns False, or raises ``sqlite3.Error`` with ``strict``.
        """
        lock = self._write_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            record = self._records.get(session_id)
            if record is None:
                return True
            discard = record.ended and session_id in self._discard
            try:
                if discard:
                    await asyncio.to_thread(self.store.delete, session_id)
                else:
                    await asyncio.to_thread(self.store.save, record)
            except sqlite3.Error as exc:
                log.warning("Cannot write session %s: %s", session_id, exc)
                if strict:
                    raise
                return False
            if record.ended and self._records.get(session_id) is record:
                del self._records[session_id]
                self._write_locks.pop(session_id, None)
                self._discard.discard(session_id)
            return True

    async def _retry_terminal(self, session_id: str) -> None:
        while session_id in self._records:
            await asyncio.sleep(TERMINAL_RETRY_INTERVAL)
            if await self._persist(session_id):
                return

    # -- tasks ---------------------------------------------------------------

    def _background_task(self, coro: Coroutine) -> None:
        """Run ``coro`` as a task that :meth:`close` cancels."""
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _spawn(self, session_id: str, coro: Coroutine) -> None:
        task = asyncio.create_task(coro, name=f"session-{session_id}")
        self._tasks[session_id] = task

        def _done(done: asyncio.Task) -> None:
            if self._tasks.get(session_id) is done:
                del self._tasks[session_id]
            if not done.cancelled() and done.exception() is not None:
                log.error("Session task for %s failed", session_id, exc_info=done.exception())

        task.add_done_callback(_done)

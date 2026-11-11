"""Session tracker: the orchestrator's live view of every session.

:class:`SessionTracker` owns every write to the ``sessions`` table. It creates the row on
``POST /api/sessions``, runs the container start in the background, follows each running
agent's ``/stream`` read-only (as the live viewer does) and folds the events into the
record. Records of sessions that have not ended are kept in memory and are authoritative
(D12a); reads of ended sessions go to the store. Every change is passed to the registered
listeners.

Docker is reached only through a :class:`Runtime`, so tests can substitute a fake.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Coroutine
from typing import Protocol

import msgspec
import websockets
from archie_shared.events import SessionStatus, decode_event
from archie_shared.schemas import NexusConfig
from archie_shared.session import SessionDescriptor, container_name, split_id
from websockets.exceptions import WebSocketException

from archie_orchestrator import docker, lifecycle
from archie_orchestrator.docker import DockerError, RunningContainer
from archie_orchestrator.lifecycle import PreparedStart
from archie_orchestrator.store import SessionStore, id_start_time, import_log, now_iso

log = logging.getLogger(__name__)

RESUBSCRIBE_DELAY = 1.0
"""Seconds before following a session's stream again after it closed while running."""
TERMINAL_RETRY_INTERVAL = 5.0
"""Seconds between retries of a failed terminal write (D12a)."""

_STREAM_ERRORS = (OSError, TimeoutError, WebSocketException)


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
    """

    def __init__(self, store: SessionStore, runtime: Runtime) -> None:
        self.store = store
        self.runtime = runtime
        # Every record that hasn't ended, plus ended ones whose terminal write is pending.
        self._records: dict[str, SessionDescriptor] = {}
        self._ports: dict[str, int] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._background: set[asyncio.Task] = set()
        self._write_locks: dict[str, asyncio.Lock] = {}
        self._listeners: list[Callable[[SessionDescriptor], None]] = []

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Migrate the store, load live records, reconcile with Docker and follow sessions."""
        await asyncio.to_thread(self.store.migrate)
        for record in await asyncio.to_thread(self.store.list, False):
            self._records[record.session_id] = record
        await self._reconcile()
        for session_id in list(self._ports):
            self._spawn(session_id, self._follow(session_id))

    async def close(self) -> None:
        """Cancel every background task. Records keep their status (the sessions run on)."""
        tasks = [*self._tasks.values(), *self._background]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._background.clear()

    async def _reconcile(self) -> None:
        """Make every running container's record live (D13), adopting unknown ones.

        A Docker failure is logged and leaves the records as they are.
        """
        try:
            running = await asyncio.to_thread(self.runtime.list_running)
        except (DockerError, OSError) as exc:
            log.warning("Cannot list running sessions: %s", exc)
            return
        for container in running:
            session_id = container.session_id
            record = self._records.get(session_id)
            if record is None:
                record = await asyncio.to_thread(self.store.get, session_id)
            if record is None:
                record = await asyncio.to_thread(self._adopt, session_id)
                log.info("Adopted running session %s", session_id)
            if container.port is not None:
                self._ports[session_id] = container.port
            await self._put(msgspec.structs.replace(record, status="idle", ended_at=None))

    def _adopt(self, session_id: str) -> SessionDescriptor:
        """A record for a running container with no row: from its log, else from its ID."""
        path = self.store.db.parent / "sessions" / f"{session_id}.jsonl"
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

    # -- listeners -----------------------------------------------------------

    def add_listener(self, listener: Callable[[SessionDescriptor], None]) -> None:
        """Call ``listener`` with the full record after every change."""
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[SessionDescriptor], None]) -> None:
        """Stop calling a listener added with :meth:`add_listener`."""
        if listener in self._listeners:
            self._listeners.remove(listener)

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
            name_fixed=name is not None,
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

    # -- following the agent stream ------------------------------------------

    async def _follow(self, session_id: str) -> None:
        """Fold the agent's ``/stream`` into the record until the session ends."""
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
                        if isinstance(frame, str):
                            await self._on_frame(session_id, frame)
            except _STREAM_ERRORS as exc:
                log.info("Session %s stream closed: %s", session_id, exc)
            if not await self._still_running(session_id):
                return
            await asyncio.sleep(RESUBSCRIBE_DELAY)

    async def _on_frame(self, session_id: str, frame: str) -> None:
        try:
            event = decode_event(frame)
        except (msgspec.DecodeError, msgspec.ValidationError, ValueError) as exc:
            log.debug("Tracker: skipping undecodable frame for %s: %s", session_id, exc)
            return
        record = self._records.get(session_id)
        if record is None or record.ended:
            return
        if isinstance(event, SessionStatus):
            status = "idle" if event.state == "done" else event.state
            changes = {
                key: value
                for key, value in (
                    ("status", status),
                    ("model_key", event.model_key),
                    ("question", event.question),
                )
                if getattr(record, key) != value
            }
            if changes:
                await self._update(session_id, **changes)

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
        log.info("Session %s exited", session_id)
        await self._update(session_id, status="exited", ended_at=now_iso())
        return False

    # -- writes --------------------------------------------------------------

    async def _update(self, session_id: str, **changes: object) -> SessionDescriptor | None:
        record = self._records.get(session_id)
        if record is None:
            return None
        return await self._put(msgspec.structs.replace(record, **changes))

    async def _put(self, record: SessionDescriptor) -> SessionDescriptor:
        """Make ``record`` current in memory, publish it and write it to the store."""
        session_id = record.session_id
        self._records[session_id] = record
        if record.ended:
            self._ports.pop(session_id, None)
        self._publish(record)
        if not await self._persist(session_id) and record.ended:
            task = asyncio.create_task(self._retry_terminal(session_id))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        return record

    async def _persist(self, session_id: str) -> bool:
        """Write the current record; drop an ended one from memory once written."""
        lock = self._write_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            record = self._records.get(session_id)
            if record is None:
                return True
            try:
                await asyncio.to_thread(self.store.save, record)
            except sqlite3.Error as exc:
                log.warning("Cannot write session %s: %s", session_id, exc)
                return False
            if record.ended and self._records.get(session_id) is record:
                del self._records[session_id]
                self._write_locks.pop(session_id, None)
            return True

    async def _retry_terminal(self, session_id: str) -> None:
        while session_id in self._records:
            await asyncio.sleep(TERMINAL_RETRY_INTERVAL)
            if await self._persist(session_id):
                return

    # -- tasks ---------------------------------------------------------------

    def _spawn(self, session_id: str, coro: Coroutine) -> None:
        task = asyncio.create_task(coro, name=f"session-{session_id}")
        self._tasks[session_id] = task

        def _done(done: asyncio.Task) -> None:
            if self._tasks.get(session_id) is done:
                del self._tasks[session_id]
            if not done.cancelled() and done.exception() is not None:
                log.error("Session task for %s failed", session_id, exc_info=done.exception())

        task.add_done_callback(_done)

"""Shared fakes for the session store, tracker and session API tests.

``FakeRuntime`` stands in for Docker (082 D16); ``FakeAgent`` stands in for an agent's
``/stream`` socket when ``websockets.connect`` is patched; ``make_tracker`` builds a
``SessionTracker`` over a migrated store in the test's ARCHIE_HOME_DIR with records
already live, as the lifespan would leave them, without starting any task.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import threading
from collections.abc import Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from archie_orchestrator.docker import RunningContainer
from archie_orchestrator.lifecycle import PreparedStart
from archie_orchestrator.store import SessionStore, db_path, id_start_time
from archie_orchestrator.tracker import Runtime, SessionTracker
from archie_shared.config import home_dir
from archie_shared.session import SessionDescriptor, container_name, split_id

SID = "proj-01abc12345"
PORT = 32771


def record(session_id: str = SID, *, status: str = "idle", **fields) -> SessionDescriptor:
    """A session record with defaults derived from the ID."""
    values = {
        "workspace": split_id(session_id)[0],
        "backend": "native",
        "name": session_id,
        "started_at": id_start_time(session_id) or "2026-01-01T00:00:00+00:00",
    }
    values.update(fields)
    return SessionDescriptor(session_id=session_id, status=status, **values)


def running(session_id: str = SID, port: int | None = PORT) -> RunningContainer:
    """A ``docker ps`` entry for a session."""
    return RunningContainer(
        session_id=session_id, container_name=container_name(session_id), port=port
    )


class FakeRuntime:
    """In-memory :class:`Runtime`; records calls and fails on request.

    Args:
        containers: What ``list_running`` returns.
        list_error: Raised by ``list_running`` instead.
        port: What ``run`` returns.
        run_error: Raised by ``run`` instead.
        prepare_error: Raised by ``prepare`` instead.
        alive: What ``is_running`` returns; a successful ``stop`` sets it False.
        stop_error: Raised by ``stop`` instead.
        log_removed: What a successful ``stop`` returns (the log had no prompt).
    """

    def __init__(
        self,
        containers: Iterable[RunningContainer] = (),
        *,
        list_error: Exception | None = None,
        port: int = PORT,
        run_error: Exception | None = None,
        prepare_error: Exception | None = None,
        alive: bool = True,
        stop_error: Exception | None = None,
        log_removed: bool = False,
    ) -> None:
        self.containers = list(containers)
        self.list_error = list_error
        self.port = port
        self.run_error = run_error
        self.prepare_error = prepare_error
        self.alive = alive
        self.stop_error = stop_error
        self.log_removed = log_removed
        self.stops: list[str] = []
        self.prepared: list[tuple[str, str]] = []
        self.runs: list[PreparedStart] = []
        self.checked: list[str] = []
        self.list_calls = 0
        # Set to hold ``run`` (or ``stop``) until the test releases it.
        self.run_gate: threading.Event | None = None
        self.stop_gate: threading.Event | None = None
        self._ids = itertools.count(1)

    def list_running(self) -> list[RunningContainer]:
        self.list_calls += 1
        if self.list_error is not None:
            raise self.list_error
        return list(self.containers)

    def prepare(self, workspace: str, config, backend: str) -> PreparedStart:
        self.prepared.append((workspace, backend))
        if self.prepare_error is not None:
            raise self.prepare_error
        session_id = f"{workspace}-01k{next(self._ids):07d}"
        return PreparedStart(
            session_id=session_id,
            container_name=container_name(session_id),
            workspace=workspace,
            backend=backend,
            command=["docker", "run"],
            nexus_home=home_dir(),
        )

    def run(self, prepared: PreparedStart) -> int:
        self.runs.append(prepared)
        if self.run_gate is not None:
            self.run_gate.wait(5)
        if self.run_error is not None:
            raise self.run_error
        return self.port

    def is_running(self, session_id: str) -> bool:
        self.checked.append(session_id)
        return self.alive

    def stop(self, session_id: str) -> bool:
        self.stops.append(session_id)
        if self.stop_gate is not None:
            self.stop_gate.wait(5)
        if self.stop_error is not None:
            raise self.stop_error
        self.alive = False
        return self.log_removed


def status_frame(state: str = "idle", model_key: str = "m1", question: dict | None = None) -> str:
    """An encoded ``session_status`` frame."""
    frame = {
        "type": "session_status",
        "id": "ss",
        "model_key": model_key,
        "git_branch": "main",
        "state": state,
    }
    if question is not None:
        frame["question"] = question
    return json.dumps(frame)


def handshake_frame(session_id: str = SID) -> str:
    return json.dumps(
        {"type": "handshake", "id": "h", "protocol_version": 2, "session_id": session_id}
    )


def user_frame(scope: str | None = None, content: str = "hi") -> str:
    """An encoded ``user_message`` frame (root unless ``scope`` is given)."""
    return json.dumps(
        {"type": "user_message", "id": "u", "turn": 1, "scope": scope, "content": content}
    )


def delta_frame(text: str = "x", scope: str | None = None) -> str:
    """An encoded ``text_delta`` frame: activity only."""
    return json.dumps(
        {
            "type": "text_delta",
            "id": "d",
            "turn": 1,
            "iteration": 1,
            "scope": scope,
            "request_id": "r",
            "text": text,
        }
    )


def turn_frame(kind: str, scope: str | None = None) -> str:
    """An encoded turn end: ``complete``, ``error`` or ``interrupted``."""
    frame: dict = {"type": f"turn_{kind}", "id": "t", "turn": 1, "scope": scope}
    if kind == "complete":
        frame["stop_reason"] = "end_turn"
    elif kind == "error":
        frame["message"] = "boom"
    return json.dumps(frame)


class FakeAgent:
    """Fake agent ``/stream`` sockets, one script per connection.

    A script is a list of frames (``str``) and pauses (``float`` seconds). Once every
    script has been used, further connections are refused.
    """

    def __init__(self, *scripts: list) -> None:
        self.scripts = list(scripts)
        self.urls: list[str] = []
        self.sent: list[str] = []

    def connect(self):
        agent = self

        @asynccontextmanager
        async def _connect(url, **kwargs):
            agent.urls.append(url)
            if not agent.scripts:
                raise ConnectionRefusedError("no agent")
            yield _Socket(agent, agent.scripts.pop(0))

        return _connect


class _Socket:
    def __init__(self, agent: FakeAgent, frames: list) -> None:
        self.agent = agent
        self.frames = frames

    async def send(self, msg):  # pragma: no cover - the tracker never sends
        self.agent.sent.append(msg)

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for frame in self.frames:
            if isinstance(frame, float):
                await asyncio.sleep(frame)
            else:
                yield frame


def make_tracker(
    live: dict[str, int | None] | None = None,
    *,
    records: Iterable[SessionDescriptor] = (),
    runtime: Runtime | None = None,
    home: Path | None = None,
) -> SessionTracker:
    """A tracker over a migrated store, with records already in place.

    Args:
        live: Running sessions (``idle``) and their agent ports (None: no port yet).
        records: Extra records written to the store; ones that haven't ended are live.
        runtime: The runtime (a fresh :class:`FakeRuntime` by default).
        home: Archie home (the test's ARCHIE_HOME_DIR by default).
    """
    store = SessionStore(db_path(home or home_dir()))
    store.migrate()
    tracker = SessionTracker(store, runtime or FakeRuntime())
    for item in records:
        store.save(item)
        if not item.ended:
            tracker._records[item.session_id] = item
    for session_id, port in (live or {}).items():
        item = tracker._records.get(session_id) or record(session_id)
        store.save(item)
        tracker._records[session_id] = item
        if port is not None:
            tracker._ports[session_id] = port
    return tracker


@contextmanager
def use_tracker(app, tracker: SessionTracker) -> Iterator[SessionTracker]:
    """Install ``tracker`` as ``app.state.tracker`` for the block (ASGI tests run no lifespan)."""
    missing = object()
    previous = getattr(app.state, "tracker", missing)
    app.state.tracker = tracker
    try:
        yield tracker
    finally:
        if previous is missing:
            del app.state.tracker
        else:
            app.state.tracker = previous


@contextmanager
def serve(
    app,
    live: dict[str, int | None] | None = None,
    *,
    records: Iterable[SessionDescriptor] = (),
    runtime: Runtime | None = None,
) -> Iterator[SessionTracker]:
    """``use_tracker`` with a tracker from ``make_tracker(live, records=..., runtime=...)``."""
    with use_tracker(app, make_tracker(live, records=records, runtime=runtime)) as tracker:
        yield tracker


@contextmanager
def use_runtime(app, runtime: Runtime) -> Iterator[Runtime]:
    """Install ``runtime`` as ``app.state.runtime`` for a lifespan test (D16)."""
    app.state.runtime = runtime
    try:
        yield runtime
    finally:
        del app.state.runtime

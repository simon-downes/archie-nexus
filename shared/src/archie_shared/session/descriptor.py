"""Session records and HTTP payload types.

SessionDescriptor: the orchestrator's session record, as served by ``/api/sessions``.
StatusPayload: typed /status HTTP response contract.

Note: StatusPayload defines the response contract for typed
client consumption (TUI, web client). The agent endpoints currently return
equivalent untyped dicts; these types will be wired in when a typed client
SDK is introduced.
"""

from typing import Literal

import msgspec

from archie_shared.events import PendingQuestion, SessionState
from archie_shared.session.identity import container_name

type SessionRecordStatus = Literal[
    "starting",
    "idle",
    "working",
    "blocked",
    "error",
    "stopping",
    "stopped",
    "exited",
    "failed",
]
"""Orchestrator lifecycle merged with the agent's turn state."""

type SessionBackend = Literal["native", "kiro"]

type NameSource = Literal["id", "model", "user"]
"""Where a session's ``name`` came from: its ID, the title model, or the user."""

TERMINAL_STATUSES: frozenset[str] = frozenset({"stopped", "exited", "failed"})
"""Statuses of an ended session; its record no longer changes except name and unseen."""


class SessionDescriptor(msgspec.Struct):
    """One session's record, kept by the orchestrator in ``archie.db``.

    Attributes:
        session_id: The session identifier ({workspace}-{ulid_prefix}).
        workspace: Workspace directory name (from ``split_id`` at creation).
        backend: ``native`` (Archie's own loop) or ``kiro``.
        name: Display name: the user's name, else a model-written title, else the
            session ID (see ``name_source``).
        status: Lifecycle and turn state (see ``SessionRecordStatus``).
        started_at: ISO 8601 UTC start time.
        name_source: Where ``name`` came from: ``id`` (the session ID), ``model`` (a
            title from the orchestrator's title model) or ``user`` (never overwritten by
            the model).
        error: Start failure message, for ``failed`` sessions.
        question: The pending question while ``blocked``.
        unseen: True when a root turn ended since a client last cleared it.
        model_key: Model reported by the agent; None until the first ``session_status``.
        last_activity_at: ISO 8601 UTC time of the latest agent activity.
        ended_at: ISO 8601 UTC time the session ended; set with a terminal status.
        container_name: Docker container name (archie-{session_id}); None once ended.
            Computed from the session ID and status, never stored.
    """

    session_id: str
    workspace: str
    backend: SessionBackend
    name: str
    status: SessionRecordStatus
    started_at: str
    name_source: NameSource = "id"
    error: str | None = None
    question: PendingQuestion | None = None
    unseen: bool = False
    model_key: str | None = None
    last_activity_at: str | None = None
    ended_at: str | None = None
    container_name: str | None = None

    def __post_init__(self) -> None:
        self.container_name = (
            None if self.status in TERMINAL_STATUSES else container_name(self.session_id)
        )

    @property
    def ended(self) -> bool:
        """True when the session has a terminal status."""
        return self.status in TERMINAL_STATUSES


class StatusPayload(msgspec.Struct):
    """Typed /status HTTP response.

    When status is "starting" (503), other fields may be absent/None.
    """

    status: str
    model: str | None = None
    session_id: str | None = None
    turn_count: int | None = None
    turn_active: bool | None = None
    state: SessionState | None = None
    question: PendingQuestion | None = None

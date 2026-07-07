"""Session runtime descriptors and HTTP payload types.

SessionDescriptor: typed representation of a running session (from docker ps).
StatusPayload: typed /status HTTP response contract.
HistoryTurn: typed /history HTTP response element contract.

Note: StatusPayload and HistoryTurn define the response contracts for typed
client consumption (TUI, web client). The agent endpoints currently return
equivalent untyped dicts; these types will be wired in when a typed client
SDK is introduced.
"""

import msgspec

from archie_shared.types import ContentBlock


class SessionDescriptor(msgspec.Struct):
    """A running archie session (typed replacement for untyped docker ps dict).

    Attributes:
        session_id: The session identifier ({project}-{ulid_prefix}).
        container_name: Docker container name (archie-{session_id}).
        port: Published host port, or None if not yet mapped.
        raw_docker_status: Raw Status string from docker ps (for display).
    """

    session_id: str
    container_name: str
    port: int | None = None
    raw_docker_status: str = ""


class StatusPayload(msgspec.Struct):
    """Typed /status HTTP response.

    When status is "starting" (503), other fields may be absent/None.
    """

    status: str
    model: str | None = None
    session_id: str | None = None
    turn_count: int | None = None
    turn_active: bool | None = None


class HistoryTurn(msgspec.Struct):
    """One turn in the /history HTTP response."""

    turn_index: int
    role: str
    content: list[ContentBlock]

"""Session contract — identity, persistence, and runtime descriptors.

Shared between CLI, agent, and future web client.
"""

from archie_shared.session.descriptor import (
    HistoryTurn,
    SessionDescriptor,
    StatusPayload,
)
from archie_shared.session.identity import (
    CONTAINER_PREFIX,
    SESSION_PATTERN,
    container_name,
    generate_session_id,
    parse_container_name,
    split_id,
)
from archie_shared.session.log import (
    MessageEntry,
    MessageMetadata,
    write_entry,
)

__all__ = [
    "CONTAINER_PREFIX",
    "HistoryTurn",
    "MessageEntry",
    "MessageMetadata",
    "SESSION_PATTERN",
    "SessionDescriptor",
    "StatusPayload",
    "container_name",
    "generate_session_id",
    "parse_container_name",
    "split_id",
    "write_entry",
]

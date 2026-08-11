"""Session contract — identity, persistence, and runtime descriptors.

Shared between CLI, agent, and future web client.
"""

from archie_shared.session.accounting import (
    scope_costs,
    scope_direct_costs,
    scope_inclusive_costs,
)
from archie_shared.session.descriptor import (
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
    "MessageEntry",
    "MessageMetadata",
    "SESSION_PATTERN",
    "SessionDescriptor",
    "StatusPayload",
    "container_name",
    "generate_session_id",
    "parse_container_name",
    "scope_costs",
    "scope_direct_costs",
    "scope_inclusive_costs",
    "split_id",
    "write_entry",
]

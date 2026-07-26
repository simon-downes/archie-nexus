"""Session identity — single source of truth for ID format and container naming.

ID format: {workspace}-{ulid_timestamp_10chars_lowercase}
Container name: archie-{session_id}

The ULID timestamp prefix gives chronological sorting without randomness.
"""

import re

from ulid import ULID

CONTAINER_PREFIX = "archie-"

# Matches: archie-{workspace_with_hyphens}-{10_crockford_base32_chars}
# Greedy on workspace (anchors on trailing 10-char ULID timestamp).
_CROCKFORD = r"[0-9a-hjkmnp-tv-z]"
SESSION_PATTERN = re.compile(
    rf"^{re.escape(CONTAINER_PREFIX)}(.+)-({_CROCKFORD}{{10}})$", re.IGNORECASE
)


def generate_session_id(workspace: str) -> str:
    """Generate a session ID: {workspace}-{ulid_timestamp}.

    Uses the first 10 characters of a ULID (the timestamp component),
    giving millisecond-precision chronological sorting without randomness.

    Args:
        workspace: Workspace name (e.g. from detect_project_dir().name on the host).
    """
    ulid_str = str(ULID())[:10].lower()
    return f"{workspace}-{ulid_str}"


def container_name(session_id: str) -> str:
    """Build the Docker container name from a session ID."""
    return f"{CONTAINER_PREFIX}{session_id}"


def parse_container_name(name: str) -> str | None:
    """Extract the session ID from a container name.

    Returns:
        The session_id if the name matches the archie pattern, else None.
    """
    match = SESSION_PATTERN.match(name)
    if not match:
        return None
    return name.removeprefix(CONTAINER_PREFIX)


def split_id(session_id: str) -> tuple[str, str]:
    """Split a session ID into (workspace, ulid_prefix).

    The ULID prefix is the last 10 characters; everything before the final
    hyphen is the workspace name (which may itself contain hyphens).
    """
    # Last 10 chars after the final hyphen
    last_hyphen = session_id.rfind("-")
    if last_hyphen == -1:
        return session_id, ""
    return session_id[:last_hyphen], session_id[last_hyphen + 1 :]

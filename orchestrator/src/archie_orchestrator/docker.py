"""Docker subprocess helpers for the orchestrator.

Discovers running archie sessions by parsing `docker ps` output and
querying port mappings via `docker port`.
"""

import json
import subprocess

from archie_shared.session import (
    CONTAINER_PREFIX,
    SessionDescriptor,
    parse_container_name,
)

_CONTAINER_PORT = "8080"


def _query_port(name: str) -> str | None:
    """Query the mapped host port for a container. Returns port string or None."""
    result = subprocess.run(
        ["docker", "port", name, _CONTAINER_PORT],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    # Output is like "127.0.0.1:32771" — extract the port from the first line
    line = result.stdout.strip().splitlines()[0]
    return line.rsplit(":", 1)[-1]


def list_sessions() -> list[SessionDescriptor]:
    """List running archie containers as typed SessionDescriptors.

    Identifies archie-nexus containers by name pattern via parse_container_name.
    """
    result = subprocess.run(
        ["docker", "ps", "--filter", f"name={CONTAINER_PREFIX}", "--format", "{{json .}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []

    sessions = []
    for line in result.stdout.strip().splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = data.get("Names", "")
        session_id = parse_container_name(name)
        if session_id is None:
            continue
        port_str = _query_port(name)
        sessions.append(
            SessionDescriptor(
                session_id=session_id,
                container_name=name,
                port=int(port_str) if port_str else None,
                raw_docker_status=data.get("Status", ""),
            )
        )
    return sessions

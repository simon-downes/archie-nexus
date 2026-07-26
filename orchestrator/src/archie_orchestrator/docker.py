"""Docker subprocess helpers for the orchestrator.

Discovers running archie sessions by parsing `docker ps` output and
querying port mappings via `docker port`. Also provides helpers for
starting, stopping, and health-checking containers.
"""

import json
import subprocess
import time
import urllib.error
import urllib.request

from archie_shared.session import (
    CONTAINER_PREFIX,
    SessionDescriptor,
    parse_container_name,
)

CONTAINER_PORT = "8080"
IMAGE_TAG = "archie:latest"


# ---------------------------------------------------------------------------
# Low-level subprocess helpers
# ---------------------------------------------------------------------------


def _container_running(name: str) -> bool:
    """Return True if the named container is currently running."""
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _status_ok(port: str) -> bool:
    """Return True if the agent /status endpoint at the given port returns 200."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/status")
        with urllib.request.urlopen(req, timeout=1):
            return True
    except (urllib.error.URLError, OSError):
        return False


def check_image(tag: str = IMAGE_TAG) -> bool:
    """Return True if the Docker image exists locally."""
    result = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def run_container(cmd: list[str]) -> None:
    """Execute a `docker run` command.

    Args:
        cmd: Full docker run command list (starting with "docker").

    Raises:
        RuntimeError: If docker run returns a non-zero exit code.
    """
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"docker run failed:\n{result.stderr.strip()}")


def stop_container(name: str) -> None:
    """Stop a running container by name.

    Args:
        name: Docker container name.

    Raises:
        RuntimeError: If docker stop returns a non-zero exit code.
    """
    result = subprocess.run(
        ["docker", "stop", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker stop failed:\n{result.stderr.strip()}")


def wait_for_ready(
    name: str,
    docker_run_cmd: list[str],
    timeout: float = 30.0,
) -> str:
    """Wait for a container's agent to be ready. Returns the host port string.

    Polls container liveness, port publication, and agent /status endpoint.

    Args:
        name: Container name to wait on.
        docker_run_cmd: The original docker run command (used in error messages to
            suggest a debug re-run without --rm).
        timeout: Maximum seconds to wait before raising.

    Raises:
        RuntimeError: If the container crashes or the timeout is exceeded.
    """
    deadline = time.monotonic() + timeout
    port: str | None = None

    while time.monotonic() < deadline:
        if not _container_running(name):
            debug_cmd = [a for a in docker_run_cmd if a != "--rm"]
            raise RuntimeError(
                f"Container '{name}' exited during startup (removed by --rm).\n"
                f"To debug, re-run without --rm:\n"
                f"  {' '.join(debug_cmd)}\n"
                f"Then inspect with: docker logs {name}"
            )
        if port is None:
            port = _query_port(name)
        if port and _status_ok(port):
            return port
        time.sleep(0.5)

    raise RuntimeError(
        f"Container '{name}' did not become ready within {timeout:.0f}s.\n"
        f"Check logs: docker logs {name}"
    )


def _query_port(name: str) -> str | None:
    """Query the mapped host port for a container. Returns port string or None."""
    result = subprocess.run(
        ["docker", "port", name, CONTAINER_PORT],
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

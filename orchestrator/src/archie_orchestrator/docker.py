"""Docker subprocess helpers for the orchestrator.

Discovers running archie sessions by parsing `docker ps` output and
querying port mappings via `docker port`. Also provides helpers for
starting, stopping, and health-checking containers.
"""

import json
import logging
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
# Container-side username. Fixed at image build time (Dockerfile ARG USERNAME),
# independent of the host user running the orchestrator.
CONTAINER_USER = "archie"
IMAGE_TAG = "archie:latest"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DockerError(Exception):
    """Raised when a Docker CLI command fails."""

    def __init__(self, command: str | list, stderr: str, returncode: int) -> None:
        self.command = command if isinstance(command, str) else " ".join(str(c) for c in command)
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f"Docker command failed ({self.command}): {stderr.strip()}")


# ---------------------------------------------------------------------------
# Low-level subprocess helpers
# ---------------------------------------------------------------------------


def _container_running(name: str) -> bool:
    """Return True if the named container is currently running."""
    cmd = ["docker", "inspect", "-f", "{{.State.Running}}", name]
    log.debug("docker %s", " ".join(cmd[1:]))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
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
        DockerError: If docker run returns a non-zero exit code.
    """
    # Log only the docker subcommand — never the full argv, which contains
    # -e/-v pairs that may include secrets (credentials, tokens).
    log.debug("docker run (%d args)", len(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise DockerError(
            command=cmd[:3],
            stderr=result.stderr,
            returncode=result.returncode,
        )


def stop_container(name: str) -> None:
    """Stop a running container by name.

    Args:
        name: Docker container name.

    Raises:
        DockerError: If docker stop returns a non-zero exit code.
    """
    log.debug("docker stop %s", name)
    result = subprocess.run(
        ["docker", "stop", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DockerError(
            command=["docker", "stop", name],
            stderr=result.stderr,
            returncode=result.returncode,
        )


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
        DockerError: If the container crashes or the timeout is exceeded.
    """
    deadline = time.monotonic() + timeout
    port: str | None = None

    while time.monotonic() < deadline:
        if not _container_running(name):
            debug_cmd = [a for a in docker_run_cmd if a != "--rm"]
            raise DockerError(
                command=["docker", "inspect", name],
                stderr=(
                    f"Container '{name}' exited during startup (removed by --rm).\n"
                    f"To debug, re-run without --rm:\n"
                    f"  {' '.join(debug_cmd)}\n"
                    f"Then inspect with: docker logs {name}"
                ),
                returncode=1,
            )
        if port is None:
            port = _query_port(name)
        if port and _status_ok(port):
            return port
        time.sleep(0.5)

    raise DockerError(
        command=["docker", "inspect", name],
        stderr=f"Container '{name}' did not become ready within {timeout:.0f}s.\nCheck logs: docker logs {name}",
        returncode=1,
    )


def _query_port(name: str) -> str | None:
    """Query the mapped host port for a container. Returns port string or None."""
    log.debug("docker port %s %s", name, CONTAINER_PORT)
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
    cmd = ["docker", "ps", "--filter", f"name={CONTAINER_PREFIX}", "--format", "{{json .}}"]
    log.debug("docker ps --filter name=%s", CONTAINER_PREFIX)
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
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

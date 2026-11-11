"""Session lifecycle management for the orchestrator.

Owns the creation and destruction of agent containers.
All functions are synchronous — callers should use asyncio.to_thread()
when invoking from an async context.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from archie_shared.config import home_dir
from archie_shared.events import UserMessage
from archie_shared.kiro import startup_error_path
from archie_shared.schemas import NexusConfig, expand_workspace_root
from archie_shared.session import container_name, generate_session_id
from archie_shared.session.log import SessionLog

from archie_orchestrator.docker import (
    CONTAINER_AGENT_DIR,
    CONTAINER_AGENTS_HOME,
    CONTAINER_GH_WRAPPER,
    CONTAINER_NEXUS_HOME,
    CONTAINER_PERSONA_DIR,
    CONTAINER_PLUGINS_DIR,
    CONTAINER_PORT,
    CONTAINER_SHARED_DIR,
    CONTAINER_WORKSPACE_DIR,
    IMAGE_TAG,
    DockerError,
    RunningContainer,
    check_image,
    list_sessions,
    run_container,
    stop_container,
    wait_for_ready,
)
from archie_orchestrator.mounts import resolve_mounts

# orchestrator/src/archie_orchestrator/lifecycle.py → repo root
REPO_ROOT = Path(__file__).resolve().parents[3]

log = logging.getLogger(__name__)

BACKENDS = ("native", "kiro")
# Kiro startup has its own 30 s deadline inside the agent (077 D18); leave room for it.
READY_TIMEOUT_S = {"native": 30.0, "kiro": 45.0}


def _remove_empty_session_log(session_id: str) -> bool:
    """Remove a stopped session log that contains no submitted prompt.

    Returns:
        True if the log was removed; False if there was none, it holds a prompt, or it
        could not be inspected or removed.
    """
    path = home_dir() / "sessions" / f"{session_id}.jsonl"
    if not path.exists():
        return False
    try:
        raw_lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        events = SessionLog(path).read()
    except Exception:  # noqa: BLE001 — preserve the log when inspection fails
        log.warning("Could not inspect session log; preserving %s", path, exc_info=True)
        return False
    if len(events) != len(raw_lines) or any(isinstance(event, UserMessage) for event in events):
        return False
    try:
        path.unlink()
    except OSError:
        log.warning("Could not remove empty session log %s", path, exc_info=True)
        return False
    return True


@dataclass(frozen=True)
class PreparedStart:
    """A validated session start, ready for :func:`run_start`.

    Attributes:
        session_id: The new session's ID.
        container_name: Docker container name for the session.
        workspace: Workspace directory name.
        backend: ``native`` or ``kiro``.
        command: The full ``docker run`` command.
        nexus_home: Host-side home directory mounted into the container.
    """

    session_id: str
    container_name: str
    workspace: str
    backend: str
    command: list[str]
    nexus_home: Path


def prepare_start(workspace: str, config: NexusConfig, *, backend: str = "native") -> PreparedStart:
    """Validate a session start and build its ID and ``docker run`` command.

    Synchronous and quick (one ``docker image inspect``); nothing is started.

    Args:
        workspace: Workspace directory name (resolved under workspace_root).
        config: Loaded NexusConfig (provides workspace_root and other settings).
        backend: ``native`` (Archie's own loop) or ``kiro`` (``ARCHIE_BACKEND=kiro``).

    Returns:
        The PreparedStart to pass to :func:`run_start`.

    Raises:
        ValueError: If the backend is unknown or the workspace directory does not exist
            under workspace_root.
        RuntimeError: If the image is missing locally.
    """
    if backend not in BACKENDS:
        raise ValueError(f"Invalid backend {backend!r}: expected 'native' or 'kiro'")
    # 1. Resolve and validate workspace path
    # Reject names with path separators or traversal sequences to prevent
    # a user-supplied value from escaping workspace_root.
    if "/" in workspace or "\\" in workspace or workspace in (".", ".."):
        raise ValueError(f"Invalid workspace name '{workspace}': must be a single directory name.")
    workspace_path = expand_workspace_root(config) / workspace
    # Resolve and confirm the result is still under workspace_root
    resolved = workspace_path.resolve()
    workspace_root_resolved = expand_workspace_root(config).resolve()
    if not str(resolved).startswith(str(workspace_root_resolved) + "/"):
        raise ValueError(f"Invalid workspace name '{workspace}': resolves outside workspace_root.")
    if not resolved.is_dir():
        raise ValueError(
            f"Workspace '{workspace}' not found under {expand_workspace_root(config)}.\n"
            f"Expected: {workspace_path}"
        )

    # 2. Check image exists
    if not check_image(IMAGE_TAG):
        raise RuntimeError(f"Image '{IMAGE_TAG}' not found locally.\nBuild it with: archie build")

    # 3. Generate session ID
    session_id = generate_session_id(workspace=workspace)
    cname = container_name(session_id)

    # 4. Ensure host-side home and agents dirs exist
    nexus_home = home_dir()
    nexus_home.mkdir(parents=True, exist_ok=True)

    agents_dir = Path.home() / ".agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    # 5. Use the fixed container-side paths from docker.py, independent of the
    #    host user running the orchestrator.
    container_home = CONTAINER_NEXUS_HOME
    container_agents = CONTAINER_AGENTS_HOME

    # 6. Construct docker run command (mirrors cli.py start command)
    agent_dir = REPO_ROOT / "agent"
    shared_dir = REPO_ROOT / "shared"
    persona_dir_host = REPO_ROOT / "persona"
    container_persona = "/opt/archie/persona"
    plugins_dir_host = REPO_ROOT / "plugins"
    configured_brain = os.environ.get("ARCHIE_BRAIN_DIR")

    docker_cmd = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        cname,
        "--add-host=host.docker.internal:host-gateway",
        "-p",
        f"127.0.0.1:0:{CONTAINER_PORT}",
        "-e",
        f"ARCHIE_HOME_DIR={container_home}",
        "-e",
        f"ARCHIE_PERSONA_DIR={container_persona}",
        "-e",
        f"ARCHIE_PLUGINS_DIR={CONTAINER_PLUGINS_DIR}",
        "-e",
        f"ARCHIE_SESSION_ID={session_id}",
        *(["-e", "ARCHIE_BACKEND=kiro"] if backend == "kiro" else []),
        *(["-e", f"ARCHIE_BRAIN_DIR={configured_brain}"] if configured_brain else []),
        "-v",
        f"{agent_dir}:{CONTAINER_AGENT_DIR}:rw",
        "-v",
        f"{shared_dir}:{CONTAINER_SHARED_DIR}:ro",
        "-v",
        f"{persona_dir_host}:{CONTAINER_PERSONA_DIR}:rw",
        "-v",
        f"{plugins_dir_host}:{CONTAINER_PLUGINS_DIR}:ro",
        # gh wrapper over the image's copy, so wrapper changes ship without a rebuild.
        "-v",
        f"{REPO_ROOT / 'container' / 'bin' / 'gh'}:{CONTAINER_GH_WRAPPER}:ro",
        "-v",
        f"{resolved}:{CONTAINER_WORKSPACE_DIR}:rw",
        "-v",
        f"{nexus_home}:{container_home}:rw",
        "-v",
        f"{agents_dir}:{container_agents}:rw",
        *[arg for mount in resolve_mounts(config.mounts) for arg in ("-v", mount)],
        "-w",
        "/workspace",
        IMAGE_TAG,
    ]

    return PreparedStart(
        session_id=session_id,
        container_name=cname,
        workspace=workspace,
        backend=backend,
        command=docker_cmd,
        nexus_home=nexus_home,
    )


def run_start(prepared: PreparedStart) -> int:
    """Run a prepared session's container and wait until its agent is ready.

    Synchronous — blocks until the container's agent is ready (up to 30 s; 45 s for Kiro).
    Use asyncio.to_thread() when calling from an async handler. A container that fails
    to become ready is stopped before the error is raised.

    Returns:
        The agent's published host port.

    Raises:
        DockerError: If docker run fails or the container does not become ready
            within the timeout (for Kiro, with the agent's recorded startup error).
    """
    cname = prepared.container_name
    docker_cmd = prepared.command
    backend = prepared.backend

    # Start container
    run_container(docker_cmd)

    # Wait for ready; raises RuntimeError on crash or timeout.
    # On failure, tear down the container we started so a stuck/timed-out
    # container is not left running (--rm only covers exit/crash).
    try:
        port_str = wait_for_ready(cname, docker_cmd, timeout=READY_TIMEOUT_S[backend])
    except RuntimeError:
        _cleanup_failed_start(cname)
        raise
    except DockerError as exc:
        if backend != "kiro":
            raise
        # The container runs with --rm, so the agent leaves the reason on the host mount.
        _cleanup_failed_start(cname)
        reason = _kiro_startup_error(prepared.nexus_home, prepared.session_id)
        if reason is None:
            raise
        raise DockerError(
            command=exc.command,
            stderr=f"{exc.stderr}\nKiro startup failed: {reason}",
            returncode=exc.returncode,
        ) from exc

    log.info("Session started: %s (workspace: %s)", prepared.session_id, prepared.workspace)
    return int(port_str)


def start_session(
    workspace: str, config: NexusConfig, *, backend: str = "native"
) -> RunningContainer:
    """Start a new agent container session and wait until it is ready.

    The composition of :func:`prepare_start` and :func:`run_start`. Synchronous; use
    asyncio.to_thread() when calling from an async handler.

    Returns:
        The RunningContainer with session_id, container_name and port.

    Raises:
        ValueError: If the workspace directory does not exist under workspace_root.
        RuntimeError: If the image is missing locally.
        DockerError: If docker run fails or the container does not become ready
            within the timeout.
    """
    prepared = prepare_start(workspace, config, backend=backend)
    port = run_start(prepared)
    return RunningContainer(
        session_id=prepared.session_id, container_name=prepared.container_name, port=port
    )


def _cleanup_failed_start(cname: str) -> None:
    try:
        stop_container(cname)
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup
        log.warning("Cleanup after failed start failed for %s: %s", cname, exc)


def _kiro_startup_error(nexus_home: Path, session_id: str) -> str | None:
    """The reason a Kiro session failed to start, if the agent recorded one."""
    path = startup_error_path(nexus_home, session_id)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def stop_session(session_id: str) -> None:
    """Stop a running session by exact session ID.

    Args:
        session_id: Exact session ID to stop (no prefix matching).

    Raises:
        KeyError: If no running session with that ID exists.
        RuntimeError: If docker stop fails.
    """
    sessions = list_sessions()
    target = next((s for s in sessions if s.session_id == session_id), None)
    if target is None:
        raise KeyError(f"No running session with ID '{session_id}'")
    stop_container(target.container_name)
    _remove_empty_session_log(session_id)
    log.info("Session stopped: %s", session_id)

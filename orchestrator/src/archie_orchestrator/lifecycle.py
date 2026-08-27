"""Session lifecycle management for the orchestrator.

Owns the creation and destruction of agent containers.
All functions are synchronous — callers should use asyncio.to_thread()
when invoking from an async context.
"""

import logging
import os
from pathlib import Path

from archie_shared.config import home_dir
from archie_shared.schemas import NexusConfig, expand_workspace_root
from archie_shared.session import (
    SessionDescriptor,
    container_name,
    generate_session_id,
)

from archie_orchestrator.docker import (
    CONTAINER_PORT,
    CONTAINER_USER,
    IMAGE_TAG,
    check_image,
    list_sessions,
    run_container,
    stop_container,
    wait_for_ready,
)

# orchestrator/src/archie_orchestrator/lifecycle.py → repo root
REPO_ROOT = Path(__file__).resolve().parents[3]

log = logging.getLogger(__name__)


def start_session(workspace: str, config: NexusConfig) -> SessionDescriptor:
    """Start a new agent container session for the given workspace.

    Synchronous — blocks until the container's agent is ready (up to 30s).
    Use asyncio.to_thread() when calling from an async handler.

    Args:
        workspace: Workspace directory name (resolved under workspace_root).
        config: Loaded NexusConfig (provides workspace_root and other settings).

    Returns:
        A SessionDescriptor with session_id, container_name, port, and status.

    Raises:
        ValueError: If the workspace directory does not exist under workspace_root.
        RuntimeError: If the image is missing locally.
        DockerError: If docker run fails or the container does not become ready
            within the timeout.
    """
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

    # 5. Derive container-side paths from the fixed container username
    #    (see CONTAINER_USER — not the host user running the orchestrator).
    container_home = f"/home/{CONTAINER_USER}/.nexus"
    container_agents = f"/home/{CONTAINER_USER}/.agents"

    # 6. Construct docker run command (mirrors cli.py start command)
    agent_dir = REPO_ROOT / "agent"
    shared_dir = REPO_ROOT / "shared"
    persona_dir_host = REPO_ROOT / "persona"
    container_persona = "/opt/archie/persona"
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
        f"ARCHIE_SESSION_ID={session_id}",
        *(["-e", f"ARCHIE_BRAIN_DIR={configured_brain}"] if configured_brain else []),
        "-v",
        f"{agent_dir}:/opt/archie/agent:rw",
        "-v",
        f"{shared_dir}:/opt/archie/shared:ro",
        "-v",
        f"{persona_dir_host}:{container_persona}:rw",
        "-v",
        f"{resolved}:/workspace:rw",
        "-v",
        f"{nexus_home}:{container_home}:rw",
        "-v",
        f"{agents_dir}:{container_agents}:rw",
        "-w",
        "/workspace",
        IMAGE_TAG,
    ]

    # 7. Start container
    run_container(docker_cmd)

    # 8. Wait for ready; raises RuntimeError on crash or timeout.
    #    On failure, tear down the container we started so a stuck/timed-out
    #    container is not left running (--rm only covers exit/crash).
    try:
        port_str = wait_for_ready(cname, docker_cmd)
    except RuntimeError:
        try:
            stop_container(cname)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            log.warning("Cleanup after failed start failed for %s: %s", cname, exc)
        raise

    log.info("Session started: %s (workspace: %s)", session_id, workspace)
    return SessionDescriptor(
        session_id=session_id,
        container_name=cname,
        port=int(port_str),
        raw_docker_status="",  # freshly started; real status available via GET /sessions
    )


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
    log.info("Session stopped: %s", session_id)

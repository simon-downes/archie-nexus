"""Session lifecycle management for the orchestrator.

Owns the creation and destruction of agent containers.
All functions are synchronous — callers should use asyncio.to_thread()
when invoking from an async context.
"""

import logging
import os
import secrets
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

from archie_shared.agent_sync import ORCHESTRATOR_URL_ENV, TOKEN_ENV
from archie_shared.config import AGENT_CONTAINER_ENV, home_dir
from archie_shared.events import UserMessage
from archie_shared.kiro import startup_error_path
from archie_shared.schemas import NexusConfig, expand_workspace_root, get_profile
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
    container_running,
    run_container,
    stop_container,
    wait_for_ready,
)
from archie_orchestrator.mounts import resolve_mounts
from archie_orchestrator.workspaces import resolve_workspace

# orchestrator/src/archie_orchestrator/lifecycle.py → repo root
REPO_ROOT = Path(__file__).resolve().parents[3]

log = logging.getLogger(__name__)

BACKENDS = ("native", "kiro")
# Kiro startup has its own 30 s deadline inside the agent (077 D18); leave room for it.
READY_TIMEOUT_S = {"native": 30.0, "kiro": 45.0}
AGENTS_MD = "AGENTS.md"


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
        command: The full ``docker run`` command. It names ``ARCHIE_AGENT_TOKEN`` without
            a value; :func:`run_start` passes the value in the docker CLI's environment.
        nexus_home: Host-side home directory mounted into the container.
        agent_token: The session's agent token (plan 090), or "" when there is none.
        scratch_dir: The per-session directory mounted at ``/workspace`` when the
            workspace is ``global.scratch_workspace`` (created by :func:`run_start`);
            None for every other workspace.
        agents_md: The scratch workspace's ``AGENTS.md``, copied into ``scratch_dir`` when
            it is a regular file; None when ``scratch_dir`` is.
    """

    session_id: str
    container_name: str
    workspace: str
    backend: str
    command: list[str]
    nexus_home: Path
    agent_token: str = field(default="", repr=False)
    scratch_dir: Path | None = None
    agents_md: Path | None = None


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
    # 1. Resolve and validate workspace path (no escaping workspace_root)
    resolved = resolve_workspace(expand_workspace_root(config), workspace)

    # 2. Check image exists
    if not check_image(IMAGE_TAG):
        raise RuntimeError(f"Image '{IMAGE_TAG}' not found locally.\nBuild it with: archie build")

    # 3. Generate session ID
    session_id = generate_session_id(workspace=workspace)
    cname = container_name(session_id)

    # A scratch workspace's sessions each get a fresh directory (093); run_start creates it.
    mounted = resolved
    scratch_dir = agents_md = None
    if workspace == config.global_.scratch_workspace:
        mounted = scratch_dir = resolved / session_id
        agents_md = resolved / AGENTS_MD

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
    # The agent fetches config and credentials from this orchestrator (plan 090).
    agent_token = secrets.token_urlsafe(32)
    orchestrator_url = f"http://host.docker.internal:{get_profile(config.orchestrator).port}"

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
        "-e",
        f"{AGENT_CONTAINER_ENV}=1",
        "-e",
        f"{ORCHESTRATOR_URL_ENV}={orchestrator_url}",
        # Name only: docker takes the value from its own environment (run_start), so the
        # token never appears in argv or in a DockerError (D2).
        "-e",
        TOKEN_ENV,
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
        f"{mounted}:{CONTAINER_WORKSPACE_DIR}:rw",
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
        agent_token=agent_token,
        scratch_dir=scratch_dir,
        agents_md=agents_md,
    )


def run_start(prepared: PreparedStart) -> int:
    """Run a prepared session's container and wait until its agent is ready.

    Synchronous — blocks until the container's agent is ready (up to 30 s; 45 s for Kiro).
    Use asyncio.to_thread() when calling from an async handler. A container that fails
    to become ready is stopped before the error is raised. A scratch session's directory
    is created first, and removed again if the start fails, the container isn't running
    and the directory is unused (see :func:`_remove_unused_scratch_dir`).

    Returns:
        The agent's published host port.

    Raises:
        DockerError: If docker run fails or the container does not become ready
            within the timeout (for Kiro, with the agent's recorded startup error).
        OSError: If the scratch directory can't be created or ``AGENTS.md`` copied.
    """
    if prepared.scratch_dir is None:
        return _run(prepared)
    _create_scratch_dir(prepared.scratch_dir, prepared.agents_md)
    try:
        return _run(prepared)
    except BaseException:
        _release_scratch_dir_after_failed_start(prepared)
        raise


def _run(prepared: PreparedStart) -> int:
    cname = prepared.container_name
    docker_cmd = prepared.command
    backend = prepared.backend

    # Start container; the agent token reaches it through the docker CLI's environment.
    run_container(
        docker_cmd, env={TOKEN_ENV: prepared.agent_token} if prepared.agent_token else None
    )

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


def _regular_file(path: Path) -> bool:
    """Whether ``path`` is a regular file, not following a final symlink."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _create_scratch_dir(directory: Path, agents_md: Path | None) -> None:
    """Create a scratch session's directory and copy the workspace's ``AGENTS.md`` in.

    Workspace files may have been written by a session, so a symlinked ``AGENTS.md``
    is not followed (it could name any host file); it is skipped with a warning.

    Raises:
        OSError: If the directory exists or can't be created, or the copy fails (the
            directory is removed again).
    """
    directory.mkdir()
    if agents_md is None or not _regular_file(agents_md):
        if agents_md is not None and agents_md.is_symlink():
            log.warning("Not copying %s into %s: it is a symlink", agents_md, directory)
        return
    try:
        shutil.copyfile(agents_md, directory / AGENTS_MD)
    except OSError:
        (directory / AGENTS_MD).unlink(missing_ok=True)
        directory.rmdir()
        raise


def _release_scratch_dir_after_failed_start(prepared: PreparedStart) -> None:
    """Remove a failed start's unused scratch directory unless its container still runs.

    A native readiness timeout leaves the container running, and the tracker can adopt
    it later, so the directory is kept unless Docker says the container isn't running.
    """
    try:
        running = container_running(prepared.container_name)
    except Exception as exc:  # noqa: BLE001 — unknown means possibly running: keep it
        log.warning("Keeping %s: cannot tell if its container runs: %s", prepared.scratch_dir, exc)
        return
    if running:
        log.warning("Keeping %s: its container is still running", prepared.scratch_dir)
        return
    _remove_unused_scratch_dir(prepared.scratch_dir, prepared.agents_md)


def _remove_unused_scratch_dir(directory: Path, agents_md: Path | None) -> bool:
    """Remove a scratch session's directory if the session left nothing in it.

    Unused means empty, or holding only an ``AGENTS.md`` that is a regular file with the
    same bytes as the workspace's current regular-file ``agents_md``. Anything else is
    kept. Nothing is removed recursively, and failures are logged, not raised.

    Returns:
        Whether the directory was removed.
    """
    try:
        entries = os.listdir(directory)
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("Cannot inspect scratch directory %s: %s", directory, exc)
        return False
    copy = directory / AGENTS_MD
    try:
        if entries == [AGENTS_MD]:
            if not (
                agents_md is not None
                and _regular_file(copy)
                and _regular_file(agents_md)
                and copy.read_bytes() == agents_md.read_bytes()
            ):
                return False
            copy.unlink()
        elif entries:
            return False
        directory.rmdir()
    except OSError as exc:
        log.warning("Cannot remove scratch directory %s: %s", directory, exc)
        return False
    log.info("Removed unused scratch directory %s", directory)
    return True


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


def stop_session(session_id: str) -> bool:
    """Stop a session's container by name, then remove its log if it holds no prompt.

    Runs ``docker stop archie-<session_id>`` directly, without a ``docker ps`` lookup
    (082 D19); a log with no submitted prompt is removed (D19a).

    Args:
        session_id: Exact session ID to stop (no prefix matching).

    Returns:
        Whether the session's log was removed.

    Raises:
        DockerError: If docker stop fails (the log is left alone).
    """
    stop_container(container_name(session_id))
    removed = _remove_empty_session_log(session_id)
    log.info("Session stopped: %s", session_id)
    return removed

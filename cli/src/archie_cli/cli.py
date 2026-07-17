"""Archie CLI — container lifecycle management."""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import click
from archie_shared.session import (
    SessionDescriptor,
    container_name,
    generate_session_id,
    parse_container_name,
)

from archie_cli.project import detect_project_dir

# Repo root: cli.py is at {repo}/cli/src/archie_cli/cli.py → parents[3] = repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
IMAGE_TAG = "archie:latest"
CONTAINER_PORT = "8080"


def check_docker() -> None:
    """Verify Docker daemon is reachable. Raises ClickException if not."""
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise click.ClickException(
            "Docker is not installed or not in PATH.\n"
            "Install Docker: https://docs.docker.com/get-docker/"
        ) from None
    if result.returncode != 0:
        raise click.ClickException(
            "Docker is not available. Ensure:\n"
            "  1. Docker daemon is running\n"
            "  2. Your user has permission to use Docker\n"
            f"\nDocker error: {result.stderr.strip()}"
        )


def check_image(tag: str) -> None:
    """Verify a Docker image exists locally. Raises ClickException if not."""
    result = subprocess.run(
        ["docker", "image", "inspect", tag], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise click.ClickException(f"Image '{tag}' not found.\nBuild it with: archie build")


def _container_running(name: str) -> bool:
    """Check if a container is running via docker inspect."""
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


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


def _status_ok(port: str) -> bool:
    """Check if the agent /status endpoint responds with 200."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/status")
        with urllib.request.urlopen(req, timeout=1):
            return True
    except (urllib.error.URLError, OSError):
        return False


def wait_for_ready(name: str, docker_run_cmd: list[str], timeout: float = 30.0) -> str:
    """Wait for a container's agent to be ready. Returns the host port.

    Polls container liveness, port publication, and agent /status endpoint.
    Raises ClickException on crash or timeout with debug guidance.
    """
    deadline = time.monotonic() + timeout
    port: str | None = None

    while time.monotonic() < deadline:
        if not _container_running(name):
            # Container crashed and --rm removed it. Guide the user to debug.
            debug_cmd = [a for a in docker_run_cmd if a != "--rm"]
            raise click.ClickException(
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

    raise click.ClickException(
        f"Container '{name}' did not become ready within {timeout:.0f}s.\n"
        f"Check logs: docker logs {name}"
    )


def list_sessions() -> list[SessionDescriptor]:
    """List running archie containers as typed SessionDescriptors.

    Identifies archie-nexus containers by name pattern via parse_container_name.
    """
    from archie_shared.session import CONTAINER_PREFIX

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
        data = json.loads(line)
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


@click.group()
def main():
    """Archie — personal AI platform."""


# Register subcommand groups
from archie_cli.auth import auth  # noqa: E402

main.add_command(auth)


@main.command()
@click.option("--no-cache", is_flag=True, help="Build without Docker layer cache.")
def build(no_cache: bool):
    """Build the archie agent Docker image."""
    dockerfile = REPO_ROOT / "Dockerfile"
    if not dockerfile.exists():
        raise click.ClickException(f"Dockerfile not found at {dockerfile}")

    username = os.environ.get("USER", "archie")
    uid = str(os.getuid())

    click.echo(f"Building image: {IMAGE_TAG}")
    click.echo(f"  User: {username} (UID {uid})")
    click.echo(f"  Dockerfile: {dockerfile}")
    click.echo()

    cmd = [
        "docker",
        "build",
        "--tag",
        IMAGE_TAG,
        "--build-arg",
        f"USERNAME={username}",
        "--build-arg",
        f"USER_UID={uid}",
        "-f",
        str(dockerfile),
    ]

    if no_cache:
        cmd.append("--no-cache")

    cmd.append(str(REPO_ROOT))

    result = subprocess.run(cmd, check=False)

    if result.returncode != 0:
        sys.exit(result.returncode)

    click.echo(f"\n✓ Image '{IMAGE_TAG}' built successfully.")


@main.command()
@click.option("-d", "--detach", is_flag=True, help="Start without attaching TUI")
def start(detach: bool):
    """Start a new agent session.

    By default, attaches an interactive TUI after the container is ready.
    Use -d/--detach to start headless (print connection info and exit).
    """
    check_docker()
    check_image(IMAGE_TAG)

    session_id = generate_session_id(project=detect_project_dir().name)
    cname = container_name(session_id)
    agent_dir = REPO_ROOT / "agent"
    shared_dir = REPO_ROOT / "shared"
    project_dir = str(detect_project_dir())

    # Ensure home dir exists (config.yaml is optional; dir must exist for mount)
    from archie_shared.config import home_dir

    nexus_home = home_dir()
    nexus_home.mkdir(parents=True, exist_ok=True)

    # Write default config if none exists
    config_file = nexus_home / "config.yaml"
    if not config_file.exists():
        config_file.write_text(
            'global:\n  model: "bedrock-claude-sonnet-4-6"\n'
            '  region: "eu-west-1"\n'
            '  project_root: "~/dev"\n'
        )

    # Determine container home path (symmetric with host)
    username = os.environ.get("USER", "archie")
    container_home = f"/home/{username}/.nexus"

    # Skills directory: host ~/.agents mounted rw at the container user's home so
    # discover_skills() (which scans Path.home()/.agents/skills) finds them.
    agents_dir = Path.home() / ".agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    container_agents = f"/home/{username}/.agents"

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
        f"ARCHIE_SESSION_ID={session_id}",
        "-v",
        f"{agent_dir}:/opt/archie/agent:rw",
        "-v",
        f"{shared_dir}:/opt/archie/shared:ro",
        "-v",
        f"{project_dir}:/workspace:rw",
        "-v",
        f"{nexus_home}:{container_home}:rw",
        "-v",
        f"{agents_dir}:{container_agents}:rw",
        "-w",
        "/workspace",
        IMAGE_TAG,
    ]

    result = subprocess.run(docker_cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise click.ClickException(f"Failed to start container:\n{result.stderr.strip()}")

    # Wait for agent to be ready
    port = wait_for_ready(cname, docker_cmd)

    # Always print session ID (visible in scrollback if TUI crashes)
    click.echo(f"Session: {session_id}")

    if detach:
        click.echo(f"Container: {cname}")
        click.echo(f"Agent: http://127.0.0.1:{port}")
    else:
        from archie_cli.tui.app import ArchieApp

        app = ArchieApp(host="127.0.0.1", port=int(port))
        app.run()


@main.command(name="ls")
def ls_cmd():
    """List running agent sessions."""
    check_docker()

    sessions = list_sessions()
    if not sessions:
        click.echo("No running sessions.")
        return

    # Header
    click.echo(f"{'SESSION ID':<40} {'STATUS':<20} {'PORT'}")
    click.echo(f"{'-' * 40} {'-' * 20} {'-' * 6}")

    for s in sessions:
        port_str = str(s.port) if s.port else "-"
        click.echo(f"{s.session_id:<40} {s.raw_docker_status:<20} {port_str}")


@main.command()
@click.argument("session_id", required=False)
def shell(session_id: str | None):
    """Open an interactive bash shell in a running session.

    Supports prefix matching on session ID. If no session specified or match
    is ambiguous, displays a picker.
    """
    check_docker()

    sessions = list_sessions()
    if not sessions:
        raise click.ClickException("No running sessions. Start one with: archie start")

    # Resolve target session
    if session_id is None:
        if len(sessions) == 1:
            target = sessions[0]
        else:
            target = _pick_session(sessions)
    else:
        matches = [s for s in sessions if s.session_id.startswith(session_id)]
        if len(matches) == 0:
            raise click.ClickException(
                f"No session matching '{session_id}'.\nRun 'archie ls' to see available sessions."
            )
        elif len(matches) == 1:
            target = matches[0]
        else:
            click.echo(f"Multiple sessions match '{session_id}':")
            target = _pick_session(matches)

    # Check if /workspace exists in the container before using -w
    check = subprocess.run(
        ["docker", "exec", target.container_name, "test", "-d", "/workspace"],
        capture_output=True,
        check=False,
    )

    if check.returncode == 0:
        exec_cmd = ["docker", "exec", "-it", "-w", "/workspace", target.container_name, "bash"]
    else:
        click.echo("Warning: /workspace not found in container. Rebuild image with: archie build")
        exec_cmd = ["docker", "exec", "-it", target.container_name, "bash"]

    result = subprocess.run(exec_cmd, check=False)
    sys.exit(result.returncode)


@main.command()
@click.argument("session_id", required=False)
def attach(session_id: str | None):
    """Attach an interactive TUI to a running session.

    Supports prefix matching on session ID. If no session specified or match
    is ambiguous, displays a picker.
    """
    check_docker()

    sessions = list_sessions()
    if not sessions:
        raise click.ClickException("No running sessions. Start one with: archie start")

    # Resolve target session (same logic as shell)
    if session_id is None:
        if len(sessions) == 1:
            target = sessions[0]
        else:
            target = _pick_session(sessions)
    else:
        matches = [s for s in sessions if s.session_id.startswith(session_id)]
        if len(matches) == 0:
            raise click.ClickException(
                f"No session matching '{session_id}'.\nRun 'archie ls' to see available sessions."
            )
        elif len(matches) == 1:
            target = matches[0]
        else:
            click.echo(f"Multiple sessions match '{session_id}':")
            target = _pick_session(matches)

    port = target.port
    if not port:
        raise click.ClickException(
            f"Session '{target.session_id}' has no published port.\n"
            "It may still be starting. Try again shortly."
        )

    from archie_cli.tui.app import ArchieApp

    app = ArchieApp(host="127.0.0.1", port=int(port))
    app.run()


@main.command()
@click.argument("session_id", required=False)
def stop(session_id: str | None):
    """Stop a running agent session.

    The container is destroyed (--rm) but the session JSONL log is preserved
    on the host at ~/.nexus/sessions/{session_id}.jsonl.

    Supports prefix matching on session ID.
    """
    check_docker()

    sessions = list_sessions()
    if not sessions:
        raise click.ClickException("No running sessions.")

    # Resolve target session
    if session_id is None:
        if len(sessions) == 1:
            target = sessions[0]
        else:
            target = _pick_session(sessions)
    else:
        matches = [s for s in sessions if s.session_id.startswith(session_id)]
        if len(matches) == 0:
            raise click.ClickException(
                f"No session matching '{session_id}'.\nRun 'archie ls' to see available sessions."
            )
        elif len(matches) == 1:
            target = matches[0]
        else:
            click.echo(f"Multiple sessions match '{session_id}':")
            target = _pick_session(matches)

    result = subprocess.run(
        ["docker", "stop", target.container_name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise click.ClickException(f"Failed to stop container:\n{result.stderr.strip()}")

    click.echo(f"✓ Stopped session: {target.session_id}")
    click.echo(f"  Log retained at: ~/.nexus/sessions/{target.session_id}.jsonl")


def _pick_session(sessions: list[SessionDescriptor]) -> SessionDescriptor:
    """Display a numbered list and prompt the user to choose."""
    click.echo()
    for i, s in enumerate(sessions, 1):
        port_str = f" (port {s.port})" if s.port else ""
        click.echo(f"  {i}. {s.session_id}{port_str}")
    click.echo()

    while True:
        try:
            choice = click.prompt("Select session", type=int)
            if 1 <= choice <= len(sessions):
                return sessions[choice - 1]
            click.echo(f"  Enter a number between 1 and {len(sessions)}")
        except (ValueError, click.Abort):
            raise SystemExit(1) from None

"""Archie CLI — orchestrator protocol client."""

import os
import subprocess
import sys
from pathlib import Path

import click
import httpx
import uvicorn
from archie_shared.session import SessionDescriptor

from archie_cli.project import detect_project_dir

# Repo root: cli.py is at {repo}/cli/src/archie_cli/cli.py → parents[3] = repo root
# Needed by `archie build` (direct Docker command — not routed via orchestrator).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_IMAGE_TAG = "archie:latest"


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
    dockerfile = _REPO_ROOT / "Dockerfile"
    if not dockerfile.exists():
        raise click.ClickException(f"Dockerfile not found at {dockerfile}")

    username = os.environ.get("USER", "archie")
    uid = str(os.getuid())

    click.echo(f"Building image: {_IMAGE_TAG}")
    click.echo(f"  User: {username} (UID {uid})")
    click.echo(f"  Dockerfile: {dockerfile}")
    click.echo()

    cmd = [
        "docker",
        "build",
        "--tag",
        _IMAGE_TAG,
        "--build-arg",
        f"USERNAME={username}",
        "--build-arg",
        f"USER_UID={uid}",
        "-f",
        str(dockerfile),
    ]

    if no_cache:
        cmd.append("--no-cache")

    cmd.append(str(_REPO_ROOT))

    result = subprocess.run(cmd, check=False)

    if result.returncode != 0:
        sys.exit(result.returncode)

    click.echo(f"\n✓ Image '{_IMAGE_TAG}' built successfully.")


@main.command()
@click.option("-d", "--detach", is_flag=True, help="Start without attaching TUI")
@click.option("-w", "--workspace", default=None, help="Workspace name (default: detected from cwd)")
def start(detach: bool, workspace: str | None):
    """Start a new agent session.

    By default, attaches an interactive TUI after the container is ready.
    Use -d/--detach to start headless (print connection info and exit).
    """
    from archie_shared.schemas import expand_workspace_root, load_nexus_config

    if workspace is None:
        config = load_nexus_config()
        workspace = detect_project_dir(
            workspace_root=expand_workspace_root(config)
        ).name

    # Ensure home dir and default config exist (pre-flight for fresh installs)
    from archie_shared.config import home_dir

    nexus_home = home_dir()
    nexus_home.mkdir(parents=True, exist_ok=True)
    config_file = nexus_home / "config.yaml"
    if not config_file.exists():
        config_file.write_text(
            'global:\n  model: "bedrock-claude-sonnet-4-6"\n'
            '  region: "eu-west-1"\n'
            '  workspace_root: "~/dev"\n'
        )

    url = _orchestrator_url()
    try:
        response = httpx.post(
            f"{url}/sessions",
            json={"workspace": workspace},
            timeout=60.0,
        )
    except httpx.ConnectError:
        raise click.ClickException(
            f"Cannot connect to the orchestrator at {url}.\n"
            "Start it first with: archie serve"
        ) from None

    if response.status_code == 400:
        raise click.ClickException(response.json().get("error", "Bad request"))
    if response.status_code != 200:
        raise click.ClickException(
            f"Orchestrator error ({response.status_code}): "
            f"{response.json().get('error', 'unknown error')}"
        )

    import msgspec

    try:
        descriptor = msgspec.json.decode(response.content, type=SessionDescriptor)
    except msgspec.DecodeError as exc:
        raise click.ClickException(
            f"Unexpected response from orchestrator: {exc}"
        ) from None

    # Always print session ID (visible in scrollback if TUI crashes)
    click.echo(f"Session: {descriptor.session_id}")

    if detach:
        click.echo(f"Container: {descriptor.container_name}")
        click.echo(f"Agent: http://127.0.0.1:{descriptor.port}")
    else:
        from archie_cli.tui.app import ArchieApp

        ws_url, api_url = _session_urls(descriptor.session_id)
        app = ArchieApp(ws_url=ws_url, api_url=api_url, container_name=descriptor.container_name)
        app.run()


def _fetch_sessions(url: str) -> list[SessionDescriptor]:
    """Fetch running sessions from the orchestrator. Raises ClickException on error."""
    try:
        response = httpx.get(f"{url}/sessions", timeout=5.0)
        response.raise_for_status()
    except httpx.ConnectError:
        raise click.ClickException(
            f"Cannot connect to the orchestrator at {url}.\n"
            "Start it first with: archie serve"
        ) from None
    except httpx.HTTPStatusError as exc:
        raise click.ClickException(
            f"Orchestrator returned an error: {exc.response.status_code}"
        ) from None

    import msgspec

    try:
        return msgspec.json.decode(response.content, type=list[SessionDescriptor])
    except msgspec.DecodeError as exc:
        raise click.ClickException(
            f"Unexpected response from orchestrator: {exc}"
        ) from None


def _resolve_prefix(
    sessions: list[SessionDescriptor],
    session_id: str | None,
) -> SessionDescriptor:
    """Resolve a session by prefix match or picker. Raises ClickException on failure."""
    if not sessions:
        raise click.ClickException("No running sessions. Start one with: archie start")
    if session_id is None:
        return sessions[0] if len(sessions) == 1 else _pick_session(sessions)
    matches = [s for s in sessions if s.session_id.startswith(session_id)]
    if len(matches) == 0:
        raise click.ClickException(
            f"No session matching '{session_id}'.\nRun 'archie ls' to see available sessions."
        )
    if len(matches) == 1:
        return matches[0]
    click.echo(f"Multiple sessions match '{session_id}':")
    return _pick_session(matches)


def _session_urls(session_id: str) -> tuple[str, str]:
    """Return (ws_url, api_url) for a session via the orchestrator proxy."""
    base = _orchestrator_url()
    ws_base = base.replace("http://", "ws://")
    return (
        f"{ws_base}/sessions/{session_id}/stream",
        f"{base}/sessions/{session_id}",
    )


def _orchestrator_url() -> str:
    """Return the base URL for the orchestrator from config."""
    from archie_shared.schemas import load_nexus_config

    cfg = load_nexus_config()
    return f"http://{cfg.orchestrator.host}:{cfg.orchestrator.port}"


@main.command()
def serve():
    """Start the archie orchestrator (foreground HTTP server).

    Binds to 127.0.0.1:7600 by default. Override via ~/.nexus/config.yaml:

    \b
    orchestrator:
      host: 127.0.0.1
      port: 7600
    """
    from archie_shared.schemas import load_nexus_config

    cfg = load_nexus_config()
    host = cfg.orchestrator.host
    port = cfg.orchestrator.port

    click.echo(f"Starting archie orchestrator on {host}:{port}")
    uvicorn.run("archie_orchestrator.app:app", host=host, port=port)


@main.command(name="ls")
def ls_cmd():
    """List running agent sessions."""
    url = _orchestrator_url()
    try:
        response = httpx.get(f"{url}/sessions", timeout=5.0)
        response.raise_for_status()
    except httpx.ConnectError:
        raise click.ClickException(
            f"Cannot connect to the orchestrator at {url}.\n"
            "Start it first with: archie serve"
        ) from None
    except httpx.HTTPStatusError as exc:
        raise click.ClickException(
            f"Orchestrator returned an error: {exc.response.status_code}\n"
            f"Check 'archie serve' output for details."
        ) from None

    import msgspec
    from archie_shared.session import SessionDescriptor

    sessions = msgspec.json.decode(response.content, type=list[SessionDescriptor])

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

    Resolves the session via the orchestrator, then execs directly.
    Supports prefix matching on session ID. If no session specified or match
    is ambiguous, displays a picker.
    """
    url = _orchestrator_url()
    sessions = _fetch_sessions(url)
    target = _resolve_prefix(sessions, session_id)

    exec_cmd = ["docker", "exec", "-it", "-w", "/workspace", target.container_name, "bash"]
    result = subprocess.run(exec_cmd, check=False)
    sys.exit(result.returncode)


@main.command()
@click.argument("session_id", required=False)
def attach(session_id: str | None):
    """Attach an interactive TUI to a running session.

    Supports prefix matching on session ID. If no session specified or match
    is ambiguous, displays a picker.
    """
    url = _orchestrator_url()
    sessions = _fetch_sessions(url)
    target = _resolve_prefix(sessions, session_id)

    ws_url, api_url = _session_urls(target.session_id)

    from archie_cli.tui.app import ArchieApp

    app = ArchieApp(ws_url=ws_url, api_url=api_url, container_name=target.container_name)
    app.run()


@main.command()
@click.argument("session_id", required=False)
def stop(session_id: str | None):
    """Stop a running agent session.

    The container is destroyed (--rm) but the session JSONL log is preserved
    on the host at ~/.nexus/sessions/{session_id}.jsonl.

    Supports prefix matching on session ID.
    """
    url = _orchestrator_url()

    # Fetch running sessions from orchestrator for prefix resolution
    try:
        sessions_response = httpx.get(f"{url}/sessions", timeout=5.0)
        sessions_response.raise_for_status()
    except httpx.ConnectError:
        raise click.ClickException(
            f"Cannot connect to the orchestrator at {url}.\n"
            "Start it first with: archie serve"
        ) from None
    except httpx.HTTPStatusError as exc:
        raise click.ClickException(
            f"Orchestrator returned an error: {exc.response.status_code}"
        ) from None

    import msgspec

    try:
        sessions = msgspec.json.decode(sessions_response.content, type=list[SessionDescriptor])
    except msgspec.DecodeError as exc:
        raise click.ClickException(
            f"Unexpected response from orchestrator: {exc}"
        ) from None

    if not sessions:
        raise click.ClickException("No running sessions.")

    # Client-side prefix resolution (same logic as shell/attach)
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

    # Stop via orchestrator
    try:
        stop_response = httpx.delete(
            f"{url}/sessions/{target.session_id}", timeout=15.0
        )
    except httpx.ConnectError:
        raise click.ClickException(
            f"Cannot connect to the orchestrator at {url}.\n"
            "Start it first with: archie serve"
        ) from None

    if stop_response.status_code == 404:
        raise click.ClickException(
            f"Session '{target.session_id}' is no longer running."
        )
    if stop_response.status_code != 200:
        raise click.ClickException(
            f"Failed to stop session: {stop_response.json().get('error', 'unknown error')}"
        )

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

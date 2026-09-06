"""Archie CLI — orchestrator protocol client."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import click
import httpx
import uvicorn
from archie_shared.session import SessionDescriptor, split_id

from archie_cli.project import detect_project_dir

# Repo root: cli.py is at {repo}/cli/src/archie_cli/cli.py → parents[3] = repo root
# Needed by `archie build` (direct Docker command — not routed via orchestrator).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_IMAGE_TAG = "archie:latest"


@click.group()
def main():
    """Archie — personal AI platform."""


# Ensure ConfigError surfaces as a clean ClickException rather than a raw traceback.
_orig_main_invoke = main.invoke


def _main_invoke(ctx):
    from archie_shared.config import ConfigError as _ConfigError

    try:
        return _orig_main_invoke(ctx)
    except _ConfigError as exc:
        raise click.ClickException(str(exc)) from None


main.invoke = _main_invoke


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

    # Container username is fixed to "archie" (matches Dockerfile ARG default and
    # orchestrator CONTAINER_USER) so mount targets under /home/archie always
    # resolve. UID is matched to the host user for file ownership on Linux bind
    # mounts. (git's "dubious ownership" guard is handled separately in
    # entrypoint.sh via safe.directory, since macOS mounts present as root.)
    username = "archie"
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
@click.argument("workspace", required=False, default=None)
def start(detach: bool, workspace: str | None):
    """Start a new agent session.

    WORKSPACE may be a plain workspace name ('myproject') or prefixed with a
    profile ('gpu-box/myproject'). Defaults to the current project directory.

    By default, attaches an interactive TUI after the container is ready.
    Use -d/--detach to start headless (print connection info and exit).
    """
    from archie_shared.schemas import expand_workspace_root, get_profile, load_nexus_config

    config = load_nexus_config()

    if workspace is None:
        ws_name = detect_project_dir(workspace_root=expand_workspace_root(config)).name
        profile = get_profile(config.orchestrator)
    else:
        profile, ws_name = _resolve_target(workspace, config)

    # Ensure home dir and default config exist (pre-flight for fresh installs)
    from archie_shared.config import home_dir

    nexus_home = home_dir()
    nexus_home.mkdir(parents=True, exist_ok=True)
    config_file = nexus_home / "config.yaml"
    if not config_file.exists():
        config_file.write_text(
            'global:\n  model: "bedrock-openai-gpt-5-6-luna"\n'
            '  region: "eu-west-1"\n'
            '  workspace_root: "~/dev"\n'
        )

    url = _profile_url(profile)
    try:
        response = httpx.post(
            f"{url}/sessions",
            json={"workspace": ws_name},
            timeout=60.0,
        )
    except httpx.HTTPError as exc:
        raise click.ClickException(
            f"Cannot reach the orchestrator at {url}: {exc}\nStart it first with: archie serve"
        ) from None

    if response.status_code == 400:
        raise click.ClickException(_error_body(response, "Bad request"))
    if response.status_code != 200:
        raise click.ClickException(
            f"Orchestrator error ({response.status_code}): {_error_body(response)}"
        )

    import msgspec

    try:
        descriptor = msgspec.json.decode(response.content, type=SessionDescriptor)
    except msgspec.DecodeError as exc:
        raise click.ClickException(f"Unexpected response from orchestrator: {exc}") from None

    # Always print session ID (visible in scrollback if TUI crashes)
    click.echo(f"Session: {descriptor.session_id}")

    if detach:
        click.echo(f"Container: {descriptor.container_name}")
        click.echo(f"Agent: http://{profile.host}:{descriptor.port}")
    else:
        from archie_cli.tui.app import ArchieApp

        base = _profile_url(profile)
        ws_base = base.replace("http://", "ws://")
        ws_url = f"{ws_base}/sessions/{descriptor.session_id}/stream"
        api_url = f"{base}/sessions/{descriptor.session_id}"
        app = ArchieApp(ws_url=ws_url, api_url=api_url, container_name=descriptor.container_name)
        app.run()


def _fetch_sessions(url: str) -> list[SessionDescriptor]:
    """Fetch running sessions from the orchestrator. Raises ClickException on error."""
    try:
        response = httpx.get(f"{url}/sessions", timeout=5.0)
        response.raise_for_status()
    except httpx.ConnectError:
        raise click.ClickException(
            f"Cannot connect to the orchestrator at {url}.\nStart it first with: archie serve"
        ) from None
    except httpx.HTTPStatusError as exc:
        raise click.ClickException(
            f"Orchestrator returned an error: {exc.response.status_code}"
        ) from None

    import msgspec

    try:
        return msgspec.json.decode(response.content, type=list[SessionDescriptor])
    except msgspec.DecodeError as exc:
        raise click.ClickException(f"Unexpected response from orchestrator: {exc}") from None


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


def _profile_url(profile) -> str:
    """Return the base HTTP URL for an OrchestratorProfile."""
    return f"http://{profile.host}:{profile.port}"


def _error_body(response: httpx.Response, default: str = "unknown error") -> str:
    """Safely extract an error message from a response body.

    Falls back to a truncated raw body when the response is not JSON, so a
    non-JSON error page never raises an uncaught traceback.
    """
    try:
        return response.json().get("error", default)
    except (ValueError, AttributeError):
        text = response.text.strip()
        return text[:200] if text else default


def _parse_archie_host(env_host: str):
    """Parse ARCHIE_HOST='host:port' or 'host' into an OrchestratorProfile.

    Raises click.ClickException on malformed port.
    """
    from archie_shared.schemas import OrchestratorProfile

    host, _, raw_port = env_host.partition(":")
    if raw_port:
        try:
            port = int(raw_port)
        except ValueError:
            raise click.ClickException(
                f"Invalid ARCHIE_HOST value '{env_host}': port must be an integer."
            ) from None
    else:
        port = 7600
    return OrchestratorProfile(host=host, port=port)


def _resolve_target(arg: str, config) -> tuple:
    """Parse a 'profile/value' or 'value' positional argument.

    Respects ARCHIE_HOST env var (overrides profile resolution for all commands).
    When ARCHIE_HOST is set and arg contains a '/', the profile prefix is stripped
    so the remainder is used as the workspace/session value.

    Returns:
        (OrchestratorProfile, value) where value is the workspace or session ID remainder.

    Raises:
        click.ClickException: If the profile name is unknown or ARCHIE_HOST is malformed.
    """
    from archie_shared.schemas import get_profile

    env_host = os.environ.get("ARCHIE_HOST", "")
    if env_host:
        # Strip profile prefix if present — host comes from env, value is the remainder
        _, sep, remainder = arg.partition("/")
        value = remainder if sep else arg
        return _parse_archie_host(env_host), value

    if "/" in arg:
        profile_name, value = arg.split("/", 1)
        profile = config.orchestrator.profiles.get(profile_name)
        if profile is None:
            raise click.ClickException(
                f"Unknown profile: '{profile_name}'.\n"
                "Add it to ~/.nexus/config.yaml under orchestrator.profiles."
            )
        return profile, value

    return get_profile(config.orchestrator), arg


@main.command()
def serve():
    """Start the archie orchestrator (foreground HTTP server).

    Binds to 127.0.0.1:7600 by default. Override via ~/.nexus/config.yaml:

    \b
    orchestrator:
      profiles:
        default:
          host: 0.0.0.0
          port: 7600
    """
    from archie_shared.schemas import get_profile, load_nexus_config

    cfg = load_nexus_config()
    profile = get_profile(cfg.orchestrator)
    host = profile.host
    port = profile.port

    from archie_orchestrator import configure_logging

    configure_logging()
    click.echo(f"Starting archie orchestrator on {host}:{port}")
    uvicorn.run("archie_orchestrator.app:app", host=host, port=port, log_config=None)


@main.command(name="migrate-sessions")
@click.option(
    "--sessions-dir",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=None,
    help="Directory containing session .jsonl files (default: ~/.nexus/sessions).",
)
@click.option(
    "--metrics-db",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Metrics database path (default: ~/.nexus/metrics.db).",
)
@click.option("--force", is_flag=True, help="Overwrite existing .legacy log backups.")
def migrate_sessions(sessions_dir: Path | None, metrics_db: Path | None, force: bool):
    """Migrate stopped session logs and rebuild the metrics index.

    Stop all sessions and the orchestrator before running this one-shot command.
    """
    from archie_orchestrator.metrics import reset_and_backfill
    from archie_shared.config import home_dir
    from archie_shared.session.migrate import migrate_session_logs

    sessions_dir = sessions_dir or home_dir() / "sessions"
    metrics_db = metrics_db or home_dir() / "metrics.db"
    if not sessions_dir.exists():
        raise click.ClickException(f"Session directory not found: {sessions_dir}")

    paths = sorted(sessions_dir.glob("*.jsonl"))
    try:
        stats = migrate_session_logs(paths, force=force)
        reset_and_backfill(metrics_db, paths)
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise click.ClickException(str(exc)) from None

    click.echo(
        f"Migrated {stats.logs_migrated} session logs; removed "
        f"{stats.shell_records_removed} shell records and "
        f"{stats.model_switch_records_removed} model-switch records; "
        f"rebuilt metrics at {metrics_db}"
    )


@main.command(name="ls")
@click.argument("profile", required=False, default=None)
def ls_cmd(profile: str | None):
    """List running agent sessions.

    With no argument, lists sessions across all configured profiles (plus the
    implicit default). With a profile name, lists sessions for that profile only.
    """
    from archie_shared.schemas import get_profile, load_nexus_config

    config = load_nexus_config()

    # ARCHIE_HOST env override: query that single address regardless of profiles
    env_host = os.environ.get("ARCHIE_HOST", "")
    if env_host:
        _print_sessions_for_profile(_profile_url(_parse_archie_host(env_host)), label="ARCHIE_HOST")
        return

    if profile is not None:
        # Single named profile
        prof = config.orchestrator.profiles.get(profile)
        if prof is None:
            raise click.ClickException(
                f"Unknown profile: '{profile}'.\n"
                "Add it to ~/.nexus/config.yaml under orchestrator.profiles."
            )
        _print_sessions_for_profile(_profile_url(prof), label=profile)
    else:
        # All profiles: explicit ones + implicit default
        all_profiles: dict[str, object] = {"default": get_profile(config.orchestrator)}
        for name, prof in config.orchestrator.profiles.items():
            all_profiles[name] = prof

        multiple = len(all_profiles) > 1
        for label, prof in all_profiles.items():
            _print_sessions_for_profile(_profile_url(prof), label=label, show_label=multiple)


def _print_sessions_for_profile(url: str, label: str, show_label: bool = True) -> None:
    """Fetch and print sessions for a single orchestrator URL."""
    import msgspec

    if show_label:
        click.echo(f"\n[{label}]")

    try:
        response = httpx.get(f"{url}/sessions", timeout=5.0)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        click.echo(f"  ✗ Error {exc.response.status_code} from {url}")
        return
    except httpx.HTTPError:
        click.echo(f"  ✗ Unreachable: {url}")
        return

    try:
        sessions = msgspec.json.decode(response.content, type=list[SessionDescriptor])
    except msgspec.DecodeError:
        click.echo(f"  ✗ Unexpected response from {url}")
        return

    if not sessions:
        click.echo("No running sessions.")
        return

    click.echo(f"{'SESSION ID':<40} {'WORKSPACE':<20} {'STATUS':<20} {'PORT'}")
    click.echo(f"{'-' * 40} {'-' * 20} {'-' * 20} {'-' * 6}")
    for s in sessions:
        click.echo(_format_session_row(s))


@main.command()
@click.argument("session_id", required=False)
def shell(session_id: str | None):
    """Open an interactive bash shell in a running session.

    Resolves the session via the orchestrator, then execs directly.
    Supports prefix matching on session ID. If no session specified or match
    is ambiguous, displays a picker.

    Note: shell always uses the default orchestrator profile for session resolution
    and runs docker exec locally. Profile-prefixed session IDs are not supported
    because docker exec requires local container access.
    """
    from archie_shared.schemas import get_profile, load_nexus_config

    config = load_nexus_config()
    profile = get_profile(config.orchestrator)
    url = _profile_url(profile)
    sessions = _fetch_sessions(url)
    target = _resolve_prefix(sessions, session_id)

    exec_cmd = ["docker", "exec", "-it", "-w", "/workspace", target.container_name, "bash"]
    result = subprocess.run(exec_cmd, check=False)
    sys.exit(result.returncode)


@main.command()
@click.argument("session_id", required=False)
def attach(session_id: str | None):
    """Attach an interactive TUI to a running session.

    SESSION_ID may be prefixed with a profile: 'gpu-box/session-id'.
    Supports prefix matching. If no session specified and only one is running,
    attaches automatically.
    """
    from archie_shared.schemas import get_profile, load_nexus_config

    config = load_nexus_config()

    if session_id is not None:
        profile, sid_prefix = _resolve_target(session_id, config)
    else:
        profile = get_profile(config.orchestrator)
        sid_prefix = session_id

    url = _profile_url(profile)
    sessions = _fetch_sessions(url)
    target = _resolve_prefix(sessions, sid_prefix)

    base = _profile_url(profile)
    ws_base = base.replace("http://", "ws://")
    ws_url = f"{ws_base}/sessions/{target.session_id}/stream"
    api_url = f"{base}/sessions/{target.session_id}"

    from archie_cli.tui.app import ArchieApp

    app = ArchieApp(ws_url=ws_url, api_url=api_url, container_name=target.container_name)
    app.run()


@main.command()
@click.argument("session_id", required=False)
def stop(session_id: str | None):
    """Stop a running agent session.

    SESSION_ID may be prefixed with a profile: 'gpu-box/session-id'.
    Supports prefix matching. If omitted and only one session is running,
    stops it automatically.

    The container is destroyed (--rm) but the session JSONL log is preserved
    on the host at ~/.nexus/sessions/{session_id}.jsonl.
    """
    from archie_shared.schemas import get_profile, load_nexus_config

    config = load_nexus_config()

    if session_id is not None:
        profile, sid_prefix = _resolve_target(session_id, config)
    else:
        profile = get_profile(config.orchestrator)
        sid_prefix = session_id

    url = _profile_url(profile)
    sessions = _fetch_sessions(url)

    if not sessions:
        raise click.ClickException("No running sessions.")

    target = _resolve_prefix(sessions, sid_prefix)

    # Stop via orchestrator
    try:
        stop_response = httpx.delete(f"{url}/sessions/{target.session_id}", timeout=15.0)
    except httpx.HTTPError as exc:
        raise click.ClickException(
            f"Cannot reach the orchestrator at {url}: {exc}\nStart it first with: archie serve"
        ) from None

    if stop_response.status_code == 404:
        raise click.ClickException(f"Session '{target.session_id}' is no longer running.")
    if stop_response.status_code != 200:
        raise click.ClickException(f"Failed to stop session: {_error_body(stop_response)}")

    click.echo(f"✓ Stopped session: {target.session_id}")
    click.echo(f"  Log retained at: ~/.nexus/sessions/{target.session_id}.jsonl")


def _format_session_row(session: SessionDescriptor, prefix: str = "") -> str:
    """Format one session consistently for lists and selection prompts."""
    workspace, _ = split_id(session.session_id)
    port_str = str(session.port) if session.port else "-"
    return (
        f"{prefix}{session.session_id:<40} {workspace:<20} "
        f"{session.raw_docker_status:<20} {port_str}"
    )


def _pick_session(sessions: list[SessionDescriptor]) -> SessionDescriptor:
    """Display a numbered session table and prompt the user to choose."""
    click.echo()
    click.echo(f"{'#':<4} {'SESSION ID':<40} {'WORKSPACE':<20} {'STATUS':<20} {'PORT'}")
    click.echo(f"{'-' * 4} {'-' * 40} {'-' * 20} {'-' * 20} {'-' * 6}")
    for i, s in enumerate(sessions, 1):
        click.echo(_format_session_row(s, prefix=f"{i:<4} "))
    click.echo()

    while True:
        try:
            choice = click.prompt("Select session", type=int)
            if 1 <= choice <= len(sessions):
                return sessions[choice - 1]
            click.echo(f"  Enter a number between 1 and {len(sessions)}")
        except (ValueError, click.Abort):
            raise SystemExit(1) from None

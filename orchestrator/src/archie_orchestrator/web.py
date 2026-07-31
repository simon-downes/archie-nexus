"""Web console route handlers for the orchestrator.

Provides a read-only HTML view of orchestrator state at GET /.
Localhost-only, no auth, no session control actions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path

from archie_shared.session import split_id
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.templating import Jinja2Templates

from archie_orchestrator.docker import list_sessions

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _uptime(start_time: datetime) -> str:
    """Return a human-readable uptime string (e.g. '2h 15m' or '3m')."""
    delta = datetime.now(UTC) - start_time
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _get_version() -> str:
    """Return the archie-orchestrator package version, or 'dev' if not installed."""
    try:
        return pkg_version("archie-orchestrator")
    except PackageNotFoundError:
        return "dev"


async def sessions_page(request: Request) -> HTMLResponse:
    """GET / — render the session list as HTML.

    Lists all currently running sessions with workspace name, status, and port.
    Auto-refreshes every 5 seconds via <meta http-equiv="refresh">.
    """
    sessions = list_sessions()

    # Convert SessionDescriptor structs to plain dicts for Jinja2 rendering.
    # split_id() returns (workspace, ulid_prefix); fall back to raw ID on error.
    session_data = []
    for s in sessions:
        workspace, _ = split_id(s.session_id)
        session_data.append(
            {
                "session_id": s.session_id,
                "workspace": workspace,
                "raw_docker_status": s.raw_docker_status,
                "port": s.port,
            }
        )

    return templates.TemplateResponse(
        request,
        "sessions.html",
        {
            "sessions": session_data,
            "uptime": _uptime(request.app.state.start_time),
            "version": _get_version(),
        },
    )

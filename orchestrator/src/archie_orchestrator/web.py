"""Web console route handlers for the orchestrator.

Provides a read-only HTML view of orchestrator state at GET /.
Localhost-only, no auth, no session control actions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from archie_shared import __version__
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
    """Return the shared Archie product version."""
    return __version__


def _auth_expiry(value: str | None) -> tuple[str, str]:
    if not value:
        return "—", "—"
    try:
        expiry = datetime.fromisoformat(value)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        expiry = expiry.astimezone(UTC)
    except (TypeError, ValueError):
        return value, "unknown"
    delta = expiry - datetime.now(UTC)
    seconds = int(abs(delta.total_seconds()))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours and len(parts) < 2:
        parts.append(f"{hours}h")
    if minutes and len(parts) < 2:
        parts.append(f"{minutes}m")
    relative = " ".join(parts) or "<1m"
    return expiry.strftime("%Y-%m-%d %H:%M:%S UTC"), (
        f"in {relative}" if delta.total_seconds() >= 0 else f"{relative} ago"
    )


def _auth_status_data(request: Request) -> dict[str, list[dict]]:
    service = request.app.state.auth_service
    grouped: dict[str, list[dict]] = {"oauth": [], "static": []}
    for name in service.provider_names():
        status = service.status(name)
        item = {
            "provider": status.provider,
            "state": status.state,
            "configured": status.configured,
            "error": status.error,
        }
        if status.auth_type == "oauth":
            item["expires"], item["relative"] = _auth_expiry(status.expires_at)
            item["can_refresh"] = status.configured
        grouped[status.auth_type].append(item)
    for items in grouped.values():
        items.sort(key=lambda item: item["provider"].lower())
    return grouped


async def auth_status_page(request: Request) -> HTMLResponse:
    """GET /auth/status in a browser — render redacted credential status."""
    grouped = _auth_status_data(request)
    return templates.TemplateResponse(
        request,
        "auth_status.html",
        {
            "grouped": grouped,
            "uptime": _uptime(request.app.state.start_time),
            "version": _get_version(),
        },
    )


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

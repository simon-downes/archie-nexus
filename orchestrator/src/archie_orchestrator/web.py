"""Web console route handlers for the orchestrator.

Provides read-only HTML views of orchestrator state: running sessions at GET /,
credential status at /ui/auth, and the session log viewer under /ui/sessions.
Localhost-only, no auth, no session control actions.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from archie_shared import __version__
from archie_shared.session import split_id
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.templating import Jinja2Templates

from archie_orchestrator.docker import list_sessions
from archie_orchestrator.session_index import SessionIndex, SessionSummary, sessions_dir

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

log = logging.getLogger(__name__)

VIEWER_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
    "script-src 'self'; connect-src 'self'"
)
MAX_ROWS = 200


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


# ---------------------------------------------------------------------------
# Session log viewer
# ---------------------------------------------------------------------------


def _viewer_response(request: Request, name: str, context: dict, status_code: int = 200):
    """Render a viewer template with the viewer Content-Security-Policy."""
    response = templates.TemplateResponse(
        request, name, {"version": _get_version(), **context}, status_code=status_code
    )
    response.headers["Content-Security-Policy"] = VIEWER_CSP
    return response


def _session_index(request: Request) -> SessionIndex:
    index = getattr(request.app.state, "session_index", None)
    if index is None:
        index = SessionIndex()
        request.app.state.session_index = index
    return index


def _active_ids() -> set[str] | None:
    """Return running session IDs, or None when Docker cannot be queried."""
    try:
        return {s.session_id for s in list_sessions()}
    except OSError as exc:
        log.warning("Active session lookup failed: %s", exc)
        return None


def _fmt_int(value: int) -> str:
    return f"{value:,}"


def _fmt_cost(value: float) -> str:
    return f"${value:.4f}"


def _fmt_started(summary: SessionSummary) -> str:
    if summary.started_at is None:
        return "—"
    return summary.started_at.strftime("%Y-%m-%d %H:%M")


def _row(summary: SessionSummary, active: set[str]) -> dict:
    return {
        "session_id": summary.session_id,
        "workspace": summary.workspace,
        "started": _fmt_started(summary),
        "models": summary.models,
        "input_tokens": _fmt_int(summary.input_tokens),
        "cache_read_tokens": _fmt_int(summary.cache_read_tokens),
        "cache_write_tokens": _fmt_int(summary.cache_write_tokens),
        "output_tokens": _fmt_int(summary.output_tokens),
        "cost": _fmt_cost(summary.cost_usd),
        "active": summary.session_id in active,
        "error": summary.error,
    }


async def session_list_page(request: Request) -> HTMLResponse:
    """GET /ui/sessions — persisted session logs, newest first."""
    q = request.query_params.get("q", "")
    index = _session_index(request)
    summaries, active = await asyncio.gather(
        asyncio.to_thread(index.summaries, sessions_dir()),
        asyncio.to_thread(_active_ids),
    )
    notices: list[str] = []
    if active is None:
        notices.append("Active status unavailable: Docker could not be queried.")
        active = set()
    rows = [_row(s, active) for s in summaries[:MAX_ROWS]]
    context = {"q": q, "rows": rows, "total": len(summaries), "notices": notices}
    return _viewer_response(request, "viewer/list.html", context)

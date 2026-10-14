"""Web console route handlers for the orchestrator.

Provides read-only HTML views of orchestrator state: running sessions at GET /,
credential status at /ui/auth, and the session log viewer under /ui/sessions.
Localhost-only, no auth, no session control actions.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import msgspec
import websockets
from archie_shared import __version__
from archie_shared.events import ErrorNotice, Handshake, SessionStatus, decode_event
from archie_shared.session import split_id
from archie_shared.session.log import EventIdConflict, LogAppendError, SessionLog
from markupsafe import escape
from starlette.requests import Request
from starlette.responses import HTMLResponse, StreamingResponse
from starlette.templating import Jinja2Templates

from archie_orchestrator.docker import list_sessions
from archie_orchestrator.proxy import _resolve_session
from archie_orchestrator.session_index import (
    SessionIndex,
    SessionSummary,
    sessions_dir,
    summarize_events,
)
from archie_orchestrator.session_search import matches, parse_query
from archie_orchestrator.transcript import Change, TranscriptBuilder, Usage, build

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

log = logging.getLogger(__name__)

VIEWER_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
    "script-src 'self'; connect-src 'self'"
)
MAX_ROWS = 200
PING_INTERVAL = 15.0
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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
    """GET /ui/sessions — persisted session logs, newest first, filtered by ``q``.

    An htmx request (``HX-Request``) gets only the ``#results`` fragment; htmx
    history restores (``HX-History-Restore-Request``) get the full page.
    """
    q = request.query_params.get("q", "")
    query = parse_query(q)
    index = _session_index(request)
    summaries, active = await asyncio.gather(
        asyncio.to_thread(index.summaries, sessions_dir()),
        asyncio.to_thread(_active_ids),
    )
    notices = list(query.notices)
    if active is None:
        notices.append("Active status unavailable: Docker could not be queried.")
        active = set()
    matched = [s for s in summaries if matches(s, query)]
    rows = [_row(s, active) for s in matched[:MAX_ROWS]]
    context = {"q": q, "rows": rows, "total": len(matched), "notices": notices}
    partial = (
        request.headers.get("hx-request") == "true"
        and request.headers.get("hx-history-restore-request") != "true"
    )
    template = "viewer/_results.html" if partial else "viewer/list.html"
    response = _viewer_response(request, template, context)
    response.headers["Vary"] = "HX-Request, HX-History-Restore-Request"
    return response


def session_log_path(session_id: str) -> Path | None:
    """Return the log path for a well-formed ID whose file exists, else None."""
    if not _SESSION_ID.match(session_id):
        return None
    directory = sessions_dir()
    path = directory / f"{session_id}.jsonl"
    if path.parent != directory or not path.is_file():
        return None
    return path


def _not_found(request: Request, session_id: str) -> HTMLResponse:
    return _viewer_response(
        request, "viewer/not_found.html", {"session_id": session_id}, status_code=404
    )


def _read_transcript(path: Path) -> tuple[TranscriptBuilder, SessionSummary, str | None]:
    try:
        events = SessionLog(path).read()
        error = None
    except (EventIdConflict, LogAppendError) as exc:
        log.warning("Unreadable session log %s: %s", path, exc)
        events, error = [], str(exc)
    return build(events), summarize_events(path.stem, events), error


async def session_detail_page(request: Request) -> HTMLResponse:
    """GET /ui/sessions/{session_id} — structured transcript of one session log."""
    session_id = request.path_params["session_id"]
    path = session_log_path(session_id)
    if path is None:
        return _not_found(request, session_id)
    (builder, summary, error), active = await asyncio.gather(
        asyncio.to_thread(_read_transcript, path), asyncio.to_thread(_active_ids)
    )
    context = {
        "session_id": session_id,
        "workspace": summary.workspace,
        "started": _fmt_started(summary),
        "models": summary.models,
        "usage": builder.usage if not error else Usage(),
        "turns": builder.turns,
        "preamble": builder.preamble,
        "last_event_id": builder.last_persisted_id,
        "active": session_id in (active or set()),
        "error": error,
    }
    return _viewer_response(request, "viewer/detail.html", context)


# ---------------------------------------------------------------------------
# Live updates (SSE relay of the agent /stream WebSocket)
# ---------------------------------------------------------------------------

_OOB_ROOT = re.compile(r"^(\s*<[a-zA-Z][a-zA-Z0-9]*)")
_LIVE_ONLY_STATUS = (Handshake, SessionStatus, ErrorNotice)


def _nodes_module():
    return templates.env.get_template("viewer/_nodes.html").module


def _with_oob(fragment: str, value: str) -> str:
    """Add ``hx-swap-oob`` to the root element of a rendered node."""
    return _OOB_ROOT.sub(lambda m: f'{m.group(1)} hx-swap-oob="{value}"', fragment, count=1)


def render_change(change: Change) -> str:
    """Render one transcript Change as an htmx fragment for live.js."""
    nodes = _nodes_module()
    if change.kind == "append":
        html = str(nodes.node(change.node))
        if change.parent_id is None:
            return html  # live.js appends it to #transcript
        return f'<div hx-swap-oob="beforeend:#{change.parent_id}">{html}</div>'
    if change.kind == "replace":
        node = change.node
        if getattr(node, "kind", None) == "turn":
            html = str(nodes.turn_footer(node))
        else:
            html = str(nodes.node(node))
        # Explicit selector: a finished stream is replaced by a node with a new id.
        return _with_oob(html, f"outerHTML:#{change.target_id}")
    if change.kind == "append_text":
        return f'<span hx-swap-oob="beforeend:#{change.target_id}">{escape(change.text)}</span>'
    return f'<div id="{change.target_id}" hx-swap-oob="delete"></div>'


def _sse(event: str, data: str, event_id: str | None = None) -> str:
    lines = [f"event: {event}"]
    if event_id:
        lines.append(f"id: {event_id}")
    lines.extend(f"data: {line}" for line in data.split("\n"))
    return "\n".join(lines) + "\n\n"


def _live_notice(text: str) -> str:
    return f'<p class="chrome live-notice">{escape(text)}</p>'


def _fragments_for(event, builder: TranscriptBuilder) -> list[str]:
    changes = builder.apply(event)
    persisted_id = event.id if event.persist else None
    return [
        _sse("fragment", render_change(change), persisted_id if i == len(changes) - 1 else None)
        for i, change in enumerate(changes)
    ]


async def _resolve_live(session_id: str):
    try:
        return await asyncio.to_thread(_resolve_session, session_id)
    except (KeyError, ValueError, OSError) as exc:
        log.info("Live view: session %s not available: %s", session_id, exc)
        return None


async def _live_events(session_id: str, path: Path, cursor: str | None) -> AsyncIterator[str]:
    session = await _resolve_live(session_id)
    if session is None:
        yield _sse("end", _live_notice("Session is not running."))
        return

    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{session.port}/stream", open_timeout=10
        ) as backend:
            log.info("Live view connected: %s", session_id)
            frames: asyncio.Queue = asyncio.Queue()
            done = object()

            async def reader() -> None:
                try:
                    async for frame in backend:
                        await frames.put(frame)
                finally:
                    await frames.put(done)

            task = asyncio.create_task(reader())
            try:
                # The agent appends to the log before broadcasting, so anything
                # missing from this read arrives on the socket (deduped by id).
                events = await asyncio.to_thread(lambda: SessionLog(path).read())
                builder = TranscriptBuilder()
                ids = [e.id for e in events]
                if cursor is not None and cursor not in ids:
                    yield _sse("reload", "")
                    return
                start = ids.index(cursor) + 1 if cursor is not None else 0
                for event in events[:start]:
                    builder.apply(event)
                for event in events[start:]:
                    for message in _fragments_for(event, builder):
                        yield message

                while True:
                    try:
                        frame = await asyncio.wait_for(frames.get(), PING_INTERVAL)
                    except TimeoutError:
                        yield ": ping\n\n"
                        continue
                    if frame is done:
                        break
                    if not isinstance(frame, str):
                        continue
                    try:
                        event = decode_event(frame)
                    except (msgspec.DecodeError, msgspec.ValidationError, ValueError) as exc:
                        log.warning("Live view: skipping undecodable frame: %s", exc)
                        continue
                    if isinstance(event, _LIVE_ONLY_STATUS):
                        continue
                    for message in _fragments_for(event, builder):
                        yield message
            finally:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 - socket teardown
                    pass
    except (ConnectionRefusedError, OSError, websockets.InvalidHandshake) as exc:
        log.warning("Live view: agent unreachable for %s: %s", session_id, exc)
        yield _sse("end", _live_notice("Session is not running."))
        return
    log.info("Live view disconnected: %s", session_id)
    yield _sse("end", _live_notice("Session ended."))


async def session_live(request: Request):
    """GET /ui/sessions/{session_id}/live — server-sent transcript fragments."""
    session_id = request.path_params["session_id"]
    path = session_log_path(session_id)
    if path is None:
        return _not_found(request, session_id)
    cursor = request.headers.get("last-event-id") or request.query_params.get("after") or None
    return StreamingResponse(
        _live_events(session_id, path, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

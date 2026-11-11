"""Web console route handlers for the orchestrator.

Provides read-only HTML views of orchestrator state: running sessions at GET /,
credential status at /auth, and the session log viewer under /sessions.
Localhost-only, no auth, no session control actions.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import msgspec
import websockets
from archie_shared import __version__
from archie_shared.events import ErrorNotice, Handshake, SessionStatus, decode_event
from archie_shared.session.accounting import UsageTotals
from archie_shared.transcript import (
    Change,
    SubagentNode,
    ToolNode,
    TranscriptBuilder,
    TurnNode,
    build,
    subagent_status_text,
    tool_crumb,
    tool_preview,
)
from markupsafe import escape
from starlette.requests import Request
from starlette.responses import HTMLResponse, StreamingResponse
from starlette.templating import Jinja2Templates

from archie_orchestrator import transcript as transcript_html
from archie_orchestrator.proxy import AgentEndpoint, resolve_session
from archie_orchestrator.session_index import (
    SessionIndex,
    SessionSummary,
    read_events,
    sessions_dir,
    summarize_events,
)
from archie_orchestrator.session_search import matches, parse_query

if TYPE_CHECKING:
    from archie_orchestrator.tracker import SessionTracker

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
templates.env.filters["tokens"] = lambda value: f"{value:,}"
templates.env.filters["usd"] = lambda value: f"${value:.4f}"
templates.env.globals.update(
    # Looked up at call time so tests can patch the module's render_markdown.
    markdown=lambda text: transcript_html.render_markdown(text),
    tool_summary=transcript_html.tool_summary_html,
    tool_extra=transcript_html.tool_extra_html,
    tool_sections=transcript_html.tool_sections_html,
    tool_preview=tool_preview,
    tool_crumb=tool_crumb,
    compaction_text=transcript_html.compaction_text,
    subagent_status=subagent_status_text,
)

log = logging.getLogger(__name__)

VIEWER_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
    "script-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
MAX_ROWS = 200
PING_INTERVAL = 15.0
LIVE_QUEUE_SIZE = 1024  # matches the agent's per-client queue (session_bus.py)
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
    """GET /auth — render redacted credential status."""
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

    Lists every session that has not ended with its workspace and status, from the
    tracker's records (no Docker call). Auto-refreshes every 5 seconds via
    <meta http-equiv="refresh">.
    """
    records = await request.app.state.tracker.list()
    session_data = [
        {"session_id": r.session_id, "workspace": r.workspace, "status": r.status} for r in records
    ]

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


def _render_viewer(name: str, context: dict) -> str:
    """Render a viewer template to a string (safe to call from a worker thread)."""
    return templates.get_template(name).render({"version": _get_version(), **context})


def _session_index(request: Request) -> SessionIndex:
    # Created on first use rather than in the lifespan so the viewer works in
    # tests that drive the app without running startup.
    index = getattr(request.app.state, "session_index", None)
    if index is None:
        index = SessionIndex()
        request.app.state.session_index = index
    return index


def _active_ids(tracker: SessionTracker) -> set[str]:
    """Return the IDs of sessions that have not ended (from memory, no Docker call)."""
    return tracker.live_ids()


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
        "input_tokens": summary.input_tokens,
        "cache_read_tokens": summary.cache_read_tokens,
        "cache_write_tokens": summary.cache_write_tokens,
        "output_tokens": summary.output_tokens,
        "cost_usd": summary.cost_usd,
        "active": summary.session_id in active,
        "error": summary.error,
    }


async def session_list_page(request: Request) -> HTMLResponse:
    """GET /sessions — persisted session logs, newest first, filtered by ``q``.

    An htmx request (``HX-Request``) gets only the ``#results`` fragment; htmx
    history restores (``HX-History-Restore-Request``) get the full page.
    """
    q = request.query_params.get("q", "")
    query = parse_query(q)
    index = _session_index(request)
    summaries = await asyncio.to_thread(index.summaries, sessions_dir())
    active = _active_ids(request.app.state.tracker)
    notices = list(query.notices)
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


def _render_detail(session_id: str, path: Path, active: bool) -> str:
    """Read, build and render one transcript page. Runs in a worker thread."""
    events, error = read_events(path)
    builder = build(events)
    summary = summarize_events(session_id, events)
    return _render_viewer(
        "viewer/detail.html",
        {
            "session_id": session_id,
            "workspace": summary.workspace,
            "started": _fmt_started(summary),
            "models": summary.models,
            "usage": builder.usage,
            "turns": builder.turns,
            "preamble": builder.preamble,
            "last_event_id": builder.last_persisted_id,
            "active": active,
            "error": error,
        },
    )


async def session_detail_page(request: Request) -> HTMLResponse:
    """GET /sessions/{session_id} — structured transcript of one session log.

    Markdown, highlighting and template rendering of a large log take a few
    hundred milliseconds, so the whole render runs off the event loop.
    """
    session_id = request.path_params["session_id"]
    path = session_log_path(session_id)
    if path is None:
        return _not_found(request, session_id)
    active = session_id in _active_ids(request.app.state.tracker)
    body = await asyncio.to_thread(_render_detail, session_id, path, active)
    return HTMLResponse(body, headers={"Content-Security-Policy": VIEWER_CSP})


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
        if isinstance(node, UsageTotals):
            html = str(nodes.usage(node))
        elif isinstance(node, TurnNode):
            html = str(nodes.turn_footer(node))
        elif isinstance(node, SubagentNode):
            html = str(nodes.subagent_row(node))
        elif isinstance(node, ToolNode):
            # Only the row and its overlay: the subagents below stay in place.
            html = str(nodes.tool_head(node))
            return _with_oob(html, f"outerHTML:#{node.head_id}")
        else:
            html = str(nodes.node(node))
        # Explicit selector: a finished stream is replaced by a node with a new id.
        return _with_oob(html, f"outerHTML:#{change.target_id}")
    if change.kind == "append_text":
        return f'<span hx-swap-oob="beforeend:#{change.target_id}">{escape(change.text)}</span>'
    # Removed by live.js before the swap: an htmx OOB delete logs an error when
    # the target is absent, which is the normal case here.
    return f'<template data-remove-id="{change.target_id}"></template>'


_SSE_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def _sse(event: str, data: str, event_id: str | None = None) -> str:
    """Encode one SSE message. CR, LF and CRLF all end an SSE line, so data is
    split on all three; content can never start a new field or message."""
    lines = [f"event: {event}"]
    if event_id:
        lines.append(f"id: {event_id}")
    lines.extend(f"data: {line}" for line in _SSE_LINE_BREAK.split(data))
    return "\n".join(lines) + "\n\n"


def _live_notice(text: str) -> str:
    return f'<p class="chrome live-notice">{escape(text)}</p>'


def _message_for(event, builder: TranscriptBuilder) -> str | None:
    """All fragments one event causes, as a single SSE message.

    Persisted events carry their id, so a reconnect cursor never splits an event.
    """
    changes = builder.apply(event)
    if not changes:
        return None
    html = "\n".join(render_change(change) for change in changes)
    return _sse("fragment", html, event.id if event.persist else None)


async def _resolve_live(tracker: SessionTracker, session_id: str) -> AgentEndpoint | None:
    try:
        return resolve_session(tracker, session_id)
    except (KeyError, ValueError) as exc:
        log.info("Live view: session %s not available: %s", session_id, exc)
        return None


async def _live_events(
    tracker: SessionTracker, session_id: str, path: Path, cursor: str | None
) -> AsyncIterator[str]:
    session = await _resolve_live(tracker, session_id)
    if session is None:
        yield _sse("end", _live_notice("Session is not running."))
        return

    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{session.port}/stream", open_timeout=10
        ) as backend:
            log.info("Live view connected: %s", session_id)
            frames: asyncio.Queue = asyncio.Queue(maxsize=LIVE_QUEUE_SIZE)
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
                events, error = await asyncio.to_thread(read_events, path)
                if error is not None:
                    yield _sse("end", _live_notice(f"This log could not be read: {error}"))
                    return
                builder = TranscriptBuilder()
                ids = [e.id for e in events]
                if cursor is not None and cursor not in ids:
                    yield _sse("reload", "")
                    return
                start = ids.index(cursor) + 1 if cursor is not None else 0
                for event in events[:start]:
                    builder.apply(event)
                for event in events[start:]:
                    message = _message_for(event, builder)
                    if message:
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
                    message = _message_for(event, builder)
                    if message:
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
    """GET /sessions/{session_id}/live — server-sent transcript fragments."""
    session_id = request.path_params["session_id"]
    path = session_log_path(session_id)
    if path is None:
        return _not_found(request, session_id)
    cursor = request.headers.get("last-event-id") or request.query_params.get("after") or None
    return StreamingResponse(
        _live_events(request.app.state.tracker, session_id, path, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

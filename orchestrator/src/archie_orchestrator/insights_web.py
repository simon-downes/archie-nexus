"""Usage insights pages under ``/ui/insights``.

Each page syncs the metrics index (without waiting if another sync holds the
lock), runs one :mod:`archie_orchestrator.insights` query, and renders it
with the session log viewer's base template and Content-Security-Policy.
Chart data is embedded as ``<script type="application/json">`` blocks that
``static/insights/insights.js`` draws with the vendored Chart.js.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse

from archie_orchestrator import insights
from archie_orchestrator.metrics import connect, default_index, is_current
from archie_orchestrator.web import _SESSION_ID, _viewer_response

log = logging.getLogger(__name__)

FILTER_KEYS = ("since", "until", "workspace", "model", "scope")
UPDATING = "Index is updating; figures may be incomplete."

VIEWS = (
    ("overview", "/ui/insights", "Overview"),
    ("sessions", "/ui/insights/sessions", "Sessions"),
    ("models", "/ui/insights/models", "Models"),
    ("tools", "/ui/insights/tools", "Tools"),
    ("subagents", "/ui/insights/subagents", "Subagents & skills"),
    ("errors", "/ui/insights/errors", "Errors"),
)
SINCE_PRESETS = (
    ("7d", "Last 7 days"),
    ("30d", "Last 30 days"),
    ("90d", "Last 90 days"),
    ("all", "All time"),
)


def _parse(request: Request) -> tuple[insights.Filters, dict[str, str], list[str]]:
    """Parse filters from the query string, dropping (with a notice) each invalid value.

    ``from`` (a date input) overrides the ``since`` preset when it is set.
    """
    raw = {key: request.query_params.get(key, "").strip() for key in (*FILTER_KEYS, "from")}
    notices: list[str] = []
    for key in (*FILTER_KEYS, "from"):
        arg = "since" if key == "from" else key
        try:
            insights.parse_filters(**{arg: raw[key]})
        except insights.InsightsError as exc:
            notices.append(f"Ignored {exc}.")
            raw[key] = ""
    values = {key: raw[key] for key in FILTER_KEYS}
    if raw["from"]:
        values["since"] = raw["from"]
    return insights.parse_filters(**values), raw, notices


def _sync_and_query(query: Callable) -> tuple[object, list[str]]:
    """Sync without blocking, then run ``query(conn)``; runs in a worker thread."""
    notices: list[str] = []
    index = default_index()
    try:
        if index.sync(wait=False) is None:
            notices.append(UPDATING)
    except Exception as exc:  # noqa: BLE001 — show the page from the existing index
        log.warning("Metrics index sync failed: %s", exc)
        notices.append(f"Index sync failed: {exc}")
    if not is_current(index.db_path):
        if UPDATING not in notices:
            notices.append(UPDATING)
        return None, notices
    try:
        conn = connect(index.db_path)
    except sqlite3.OperationalError:  # archived since the sync (migrate-sessions)
        return None, [*notices, UPDATING]
    try:
        return query(conn), notices
    finally:
        conn.close()


def _link(path: str, raw: dict[str, str], **extra) -> str:
    params = {k: v for k, v in {**raw, **extra}.items() if v}
    return f"{path}?{urlencode(params)}" if params else path


async def _page(
    request: Request,
    view: str,
    template: str,
    query: Callable[[object, insights.Filters], dict],
    *,
    extra_notices: list[str] = (),
    **context,
) -> HTMLResponse:
    filters, raw, notices = _parse(request)
    notices.extend(extra_notices)

    def run(conn):
        return {"data": query(conn, filters), "options": insights.filter_options(conn)}

    result, sync_notices = await asyncio.to_thread(_sync_and_query, run)
    result = result or {
        "data": None,
        "options": {"workspaces": [], "models": [], "scopes": list(insights.SCOPES)},
    }
    return _viewer_response(
        request,
        template,
        {
            "view": view,
            "views": VIEWS,
            "raw": raw,
            "since_presets": SINCE_PRESETS,
            "notices": [*notices, *sync_notices],
            "link": lambda path, **extra: _link(path, raw, **extra),
            **result,
            **context,
        },
    )


async def overview_page(request: Request) -> HTMLResponse:
    """GET /ui/insights — totals, daily cost by model, top workspaces and models."""
    return await _page(request, "overview", "insights/overview.html", insights.overview)


async def sessions_page(request: Request) -> HTMLResponse:
    """GET /ui/insights/sessions — sortable session table."""
    sort = request.query_params.get("sort", "started")
    notices = []
    if sort not in insights.SESSION_SORTS:
        notices.append(f"Ignored invalid sort: {sort}.")
        sort = "started"
    return await _page(
        request,
        "sessions",
        "insights/sessions.html",
        lambda conn, f: insights.sessions(conn, f, sort=sort),
        extra_notices=notices,
        sort=sort,
        sorts=insights.SESSION_SORTS,
    )


async def session_page(request: Request) -> HTMLResponse:
    """GET /ui/insights/sessions/{session_id} — one session's turns, tools and subagents."""
    session_id = request.path_params["session_id"]
    if not _SESSION_ID.match(session_id):
        return _not_found(request, session_id)

    def run(conn):
        try:
            return insights.session_detail(conn, session_id)
        except KeyError:
            return None

    data, notices = await asyncio.to_thread(_sync_and_query, run)
    if data is None and UPDATING not in notices:
        return _not_found(request, session_id)
    _, raw, _ = _parse(request)
    return _viewer_response(
        request,
        "insights/session.html",
        {
            "view": "sessions",
            "views": VIEWS,
            "raw": raw,
            "notices": notices,
            "data": data,
            "session_id": session_id,
            "link": lambda path, **extra: _link(path, raw, **extra),
        },
    )


def _not_found(request: Request, session_id: str) -> HTMLResponse:
    return _viewer_response(
        request,
        "insights/not_found.html",
        {"session_id": session_id, "views": VIEWS, "view": "sessions"},
        status_code=404,
    )


async def models_page(request: Request) -> HTMLResponse:
    """GET /ui/insights/models — usage, latency and errors per model."""
    return await _page(request, "models", "insights/models.html", insights.models)


async def tools_page(request: Request) -> HTMLResponse:
    """GET /ui/insights/tools — calls, errors, duration and size per tool."""
    return await _page(request, "tools", "insights/tools.html", insights.tools)


async def subagents_page(request: Request) -> HTMLResponse:
    """GET /ui/insights/subagents — subagent types and skill loads."""
    return await _page(request, "subagents", "insights/subagents.html", insights.subagents)


async def errors_page(request: Request) -> HTMLResponse:
    """GET /ui/insights/errors — request, turn and subagent errors."""
    return await _page(request, "errors", "insights/errors.html", insights.errors)

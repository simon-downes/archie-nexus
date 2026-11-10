"""Starlette application for the archie orchestrator.

The JSON API is mounted at ``API_PREFIX`` (``/api``); see :mod:`archie_orchestrator.api`.
Pages are served at the root, except ``/health`` (liveness probe) and
``/static/*`` (CSS, JS and vendored assets).
"""

import asyncio
import html
import logging
import traceback
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
from archie_shared.protocol import API_PREFIX
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from archie_orchestrator import api, configure_logging, insights_web
from archie_orchestrator.auth import AuthError, AuthService
from archie_orchestrator.config_source import config_source
from archie_orchestrator.credential_refresh import credential_refresh_loop
from archie_orchestrator.docker import list_sessions
from archie_orchestrator.metrics import MetricsIndex, default_index
from archie_orchestrator.proxy import get_active_ws_connections
from archie_orchestrator.web import (
    auth_status_page,
    session_detail_page,
    session_list_page,
    session_live,
    sessions_page,
)

_STATIC_DIR = Path(__file__).parent / "static"

log = logging.getLogger(__name__)


class CallbackAccessFilter(logging.Filter):
    """Prevent OAuth callback query parameters from entering access logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if "/auth/callback/" in str(record.msg):
            return record.name != "uvicorn.access"
        return True


log.addFilter(CallbackAccessFilter())
logging.getLogger("uvicorn.access").addFilter(CallbackAccessFilter())

METRICS_SYNC_INTERVAL = 15.0


async def _metrics_sync_loop(index: MetricsIndex) -> None:
    """Sync the metrics index from the session logs every METRICS_SYNC_INTERVAL seconds."""
    while True:
        try:
            await asyncio.to_thread(index.sync)
        except Exception as exc:  # noqa: BLE001 — the loop must survive a bad sync
            log.warning("Metrics index sync failed: %s", exc)
        await asyncio.sleep(METRICS_SYNC_INTERVAL)


# ---------------------------------------------------------------------------
# Lifespan — validate config at startup; it is re-read on use afterwards
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: Starlette):
    """Validate config at startup and wire services to the live config source."""
    configure_logging()
    app.state.start_time = datetime.now(UTC)
    # Fail fast on an invalid config at startup; later reads go through
    # config_source so edits apply without restarting the orchestrator.
    config_source.get()
    app.state.auth_service = AuthService(config_source.latest)
    # list_sessions() shells out to docker (blocking) — keep it off the loop.
    sessions = await asyncio.to_thread(list_sessions)
    log.info("Discovered %d running sessions", len(sessions))

    # Keep the metrics index in step with the session logs.
    metrics_task = asyncio.create_task(_metrics_sync_loop(default_index()))

    def _log_metrics_task_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("Metrics writer task exited unexpectedly: %s", exc)

    metrics_task.add_done_callback(_log_metrics_task_done)

    refresh_task = asyncio.create_task(credential_refresh_loop(app.state.auth_service))
    refresh_task.add_done_callback(_log_metrics_task_done)

    yield

    for task in (metrics_task, refresh_task):
        task.cancel()
    for task in (metrics_task, refresh_task):
        try:
            await task
        except asyncio.CancelledError:
            pass

    active = get_active_ws_connections()
    log.info("Orchestrator stopping (%d active connections)", active)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def health(request: Request) -> JSONResponse:
    """Liveness probe — always returns 200 OK."""
    return JSONResponse({"status": "ok"})


async def auth_callback(request: Request) -> Response:
    service = request.app.state.auth_service
    provider_name = request.path_params["provider"]
    state = request.query_params.get("state")
    try:
        flow = service.find_flow(provider_name, state or "")
    except AuthError:
        return Response(
            "<h1>Authentication failed</h1><p>Invalid or expired flow.</p>",
            status_code=400,
            media_type="text/html",
        )
    expected_uri = service.redirect_uri(
        f"{request.url.scheme}://{request.url.netloc}", provider_name
    )
    if flow.redirect_uri != expected_uri:
        flow.status = "failed"
        flow.error = "Callback URI mismatch"
        return Response(
            "<h1>Authentication failed</h1><p>Callback URI mismatch.</p>",
            status_code=400,
            media_type="text/html",
        )
    if request.query_params.get("error") or not request.query_params.get("code"):
        flow.status = "failed"
        flow.error = "Provider authorization failed"
        flow.expires_at = datetime.now(UTC) + service.flows.retention
        return Response(
            "<h1>Authentication failed</h1><p>Authorization was not completed.</p>",
            status_code=400,
            media_type="text/html",
        )
    try:
        flow.credential_status = await service.complete(flow, request.query_params["code"])
    except AuthError as exc:
        flow.status = "failed"
        flow.error = str(exc)
        flow.expires_at = datetime.now(UTC) + service.flows.retention
        return Response(
            f"<h1>Authentication failed</h1><p>{html.escape(str(exc))}</p>",
            status_code=exc.status_code,
            media_type="text/html",
        )
    except (httpx.HTTPError, ValueError, TypeError):
        flow.status = "failed"
        flow.error = "Token exchange failed"
        flow.expires_at = datetime.now(UTC) + service.flows.retention
        return Response(
            "<h1>Authentication failed</h1><p>Token exchange failed.</p>",
            status_code=502,
            media_type="text/html",
        )
    flow.status = "succeeded"
    flow.expires_at = datetime.now(UTC) + service.flows.retention
    return Response(
        "<h1>Authentication successful</h1><p>You can close this window.</p>",
        media_type="text/html",
    )


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch unexpected exceptions, log with origin, return 500."""
    tb = traceback.extract_tb(exc.__traceback__)
    origin = f"{tb[-1].filename}:{tb[-1].lineno}" if tb else "unknown"
    log.error("Unhandled %s (at %s)", type(exc).__name__, origin)
    # Do not leak internal exception detail to the client.
    return JSONResponse(
        {"error": "Internal server error"},
        status_code=500,
    )


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/", sessions_page, methods=["GET"]),
        Route("/ui/auth", auth_status_page, methods=["GET"]),
        Route("/ui/sessions", session_list_page, methods=["GET"]),
        Route("/ui/sessions/{session_id}", session_detail_page, methods=["GET"]),
        Route("/ui/sessions/{session_id}/live", session_live, methods=["GET"]),
        Route("/ui/insights", insights_web.overview_page, methods=["GET"]),
        Route("/ui/insights/sessions", insights_web.sessions_page, methods=["GET"]),
        Route("/ui/insights/sessions/{session_id}", insights_web.session_page, methods=["GET"]),
        Route("/ui/insights/models", insights_web.models_page, methods=["GET"]),
        Route("/ui/insights/tools", insights_web.tools_page, methods=["GET"]),
        Route("/ui/insights/subagents", insights_web.subagents_page, methods=["GET"]),
        Route("/ui/insights/errors", insights_web.errors_page, methods=["GET"]),
        Route("/auth/callback/{provider}", auth_callback, methods=["GET"]),
        Route("/health", health),
        Mount(API_PREFIX, routes=api.routes),
        Mount("/static", app=StaticFiles(directory=str(_STATIC_DIR)), name="static"),
    ],
    exception_handlers={Exception: unhandled_exception_handler},
)

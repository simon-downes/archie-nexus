"""Starlette application for the archie orchestrator.

Two route modules make up the table: the JSON API in :mod:`archie_orchestrator.api`,
mounted at ``API_PREFIX`` (``/api``), and the pages in :mod:`archie_orchestrator.pages`,
served at the root. The root also has ``/health`` (liveness probe) and ``/static/*``
(CSS, JS and vendored assets).
"""

import asyncio
import logging
import traceback
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from archie_shared.config import home_dir
from archie_shared.protocol import API_PREFIX
from archie_shared.schemas import expand_workspace_root
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from archie_orchestrator import api, configure_logging, pages
from archie_orchestrator.auth import AuthService
from archie_orchestrator.config_source import config_source
from archie_orchestrator.credential_refresh import credential_refresh_loop
from archie_orchestrator.metrics import MetricsIndex, default_index
from archie_orchestrator.proxy import get_active_ws_connections
from archie_orchestrator.store import SessionStore, db_path
from archie_orchestrator.titles import Titler
from archie_orchestrator.tracker import DockerRuntime, SessionTracker

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
    # Migrate archie.db, reconcile with Docker and follow running sessions before the
    # metrics loop takes its lock (D13). Tests inject a fake runtime (D16).
    runtime = getattr(app.state, "runtime", None) or DockerRuntime()
    # Titles read the last valid config, so an invalid edit doesn't stop them (083 D10);
    # so does the workspace root that session locations are read under (084 D29).
    tracker = SessionTracker(
        SessionStore(db_path(home_dir())),
        runtime,
        titler=Titler(config_source.latest),
        workspace_root=lambda: expand_workspace_root(config_source.latest()),
    )
    await tracker.start()
    app.state.tracker = tracker
    log.info("Discovered %d running sessions", len(tracker.live_ids()))

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
    await tracker.close()

    active = get_active_ws_connections()
    log.info("Orchestrator stopping (%d active connections)", active)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def health(request: Request) -> JSONResponse:
    """Liveness probe — always returns 200 OK."""
    return JSONResponse({"status": "ok"})


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
        *pages.routes,
        Route("/health", health),
        Mount(API_PREFIX, routes=api.routes),
        Mount("/static", app=StaticFiles(directory=str(_STATIC_DIR)), name="static"),
    ],
    exception_handlers={Exception: unhandled_exception_handler},
)

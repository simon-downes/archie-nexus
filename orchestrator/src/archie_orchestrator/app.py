"""Starlette application for the archie orchestrator.

Routes:
  GET    /                                — web console (session list)
  GET    /health                          — liveness probe
  GET    /sessions                        — list running archie sessions
  POST   /sessions                        — start a new session
  DELETE /sessions/{session_id}           — stop a running session
  GET    /sessions/{session_id}/status    — proxy to session /status
  WS     /sessions/{session_id}/stream    — bidirectional WebSocket relay
  GET    /static/*                        — static files (CSS, etc.)
"""

import asyncio
import html
import logging
import sqlite3
import traceback
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import msgspec
from archie_shared.config import home_dir
from archie_shared.schemas import load_nexus_config
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

from archie_orchestrator import configure_logging
from archie_orchestrator.auth import AuthError, AuthService
from archie_orchestrator.docker import DockerError, list_sessions
from archie_orchestrator.lifecycle import start_session, stop_session
from archie_orchestrator.metrics import MetricsWriter
from archie_orchestrator.proxy import (
    get_active_ws_connections,
    proxy_events,
    proxy_status,
    proxy_stream,
)
from archie_orchestrator.web import sessions_page

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

# ---------------------------------------------------------------------------
# Lifespan — load config once at startup
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: Starlette):
    """Load NexusConfig at startup and store on app state."""
    configure_logging()
    app.state.start_time = datetime.now(UTC)
    app.state.config = load_nexus_config()
    app.state.auth_service = AuthService(app.state.config)
    # list_sessions() shells out to docker (blocking) — keep it off the loop.
    sessions = await asyncio.to_thread(list_sessions)
    log.info("Discovered %d running sessions", len(sessions))

    # Start metrics writer background task
    db_path = home_dir() / "metrics.db"
    writer = MetricsWriter(db_path)
    app.state.metrics_writer = writer
    metrics_task = asyncio.create_task(writer.run())

    def _log_metrics_task_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("Metrics writer task exited unexpectedly: %s", exc)

    metrics_task.add_done_callback(_log_metrics_task_done)

    yield

    metrics_task.cancel()
    try:
        await metrics_task
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


async def sessions_get(request: Request) -> Response:
    """Return running archie sessions as a JSON array of SessionDescriptors."""
    result = list_sessions()
    content = msgspec.json.encode(result)
    return Response(content, media_type="application/json")


async def sessions_post(request: Request) -> Response:
    """Start a new agent session.

    Request body: {"workspace": "<name>"}

    Returns:
        200 + SessionDescriptor on success.
        400 if workspace key is missing or directory not found.
        500 on Docker/startup failure.
    """
    try:
        body = await request.body()
        data = msgspec.json.decode(body, type=dict)
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    workspace = data.get("workspace")
    if not workspace or not isinstance(workspace, str):
        return JSONResponse(
            {"error": "Missing or invalid 'workspace' field in request body"},
            status_code=400,
        )

    config = request.app.state.config

    try:
        descriptor = await asyncio.to_thread(start_session, workspace, config)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except (DockerError, RuntimeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    content = msgspec.json.encode(descriptor)
    return Response(content, media_type="application/json")


async def session_delete(request: Request) -> Response:
    """Stop a running session by exact session ID.

    Returns:
        200 + {"stopped": "<session_id>"} on success.
        404 if no session with that ID is running.
    """
    session_id = request.path_params["session_id"]

    try:
        await asyncio.to_thread(stop_session, session_id)
    except KeyError:
        return JSONResponse(
            {"error": f"No running session with ID '{session_id}'"},
            status_code=404,
        )
    except (DockerError, RuntimeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"stopped": session_id})


# ---------------------------------------------------------------------------
# Metrics API
# ---------------------------------------------------------------------------


def _query_metrics(
    db_path: Path,
    session_id: str | None = None,
    since: str | None = None,
) -> dict:
    """Query aggregated metrics from SQLite. Returns a metrics dict.

    Args:
        db_path: Path to the SQLite database.
        session_id: If given, restrict to rows for this session.
        since: Optional ISO date string (YYYY-MM-DD). Filters to rows with
            timestamp >= since.

    Returns:
        Dict with total_cost, total_requests, token totals, and by_model breakdown.
    """
    empty = {
        "total_cost": 0.0,
        "total_requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "by_model": {},
    }

    if not db_path.exists():
        return empty

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        log.error("Metrics DB open failed: %s", exc)
        return empty

    try:
        # Build WHERE clauses
        conditions = []
        params: list = []
        if session_id is not None:
            conditions.append("session_id = ?")
            params.append(session_id)
        if since is not None:
            conditions.append("timestamp >= ?")
            params.append(since)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        # Aggregate totals
        row = conn.execute(
            f"SELECT "
            f"  COALESCE(SUM(cost_usd), 0.0) AS total_cost, "
            f"  COUNT(*) AS total_requests, "
            f"  COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            f"  COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            f"  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
            f"  COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens "
            f"FROM requests {where}",
            params,
        ).fetchone()

        if row is None or row["total_requests"] == 0:
            if session_id is not None:
                raise KeyError(session_id)
            return empty

        # by_model breakdown
        model_rows = conn.execute(
            f"SELECT model_key AS model, "
            f"  COALESCE(SUM(cost_usd), 0.0) AS cost, "
            f"  COUNT(*) AS requests "
            f"FROM requests {where} "
            f"GROUP BY model",
            params,
        ).fetchall()

        by_model = {r["model"]: {"cost": r["cost"], "requests": r["requests"]} for r in model_rows}

        return {
            "total_cost": row["total_cost"],
            "total_requests": row["total_requests"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "cache_read_tokens": row["cache_read_tokens"],
            "cache_write_tokens": row["cache_write_tokens"],
            "by_model": by_model,
        }
    except KeyError:
        raise
    except sqlite3.Error as exc:
        log.error("Metrics DB query failed: %s", exc)
        return empty
    finally:
        conn.close()


async def get_metrics(request: Request) -> Response:
    """GET /metrics — aggregate metrics across all sessions.

    Optional query params:
        since=<ISO 8601>  — filter to entries with timestamp >= this value.
            Interpreted as UTC (stored timestamps are UTC). Accepts a date
            (YYYY-MM-DD) or a full ISO datetime. Non-ISO values return 400.
    """
    since = request.query_params.get("since")
    if since is not None:
        try:
            datetime.fromisoformat(since)
        except ValueError:
            return JSONResponse(
                {
                    "error": "Invalid 'since' — expected ISO 8601 (e.g. 2026-01-31 "
                    "or 2026-01-31T12:00:00), interpreted as UTC."
                },
                status_code=400,
            )
    db_path = home_dir() / "metrics.db"
    result = await asyncio.to_thread(_query_metrics, db_path, since=since)
    return Response(msgspec.json.encode(result), media_type="application/json")


async def get_session_metrics(request: Request) -> Response:
    """GET /sessions/{session_id}/metrics — metrics for a single session.

    Returns 404 if no metrics have been recorded for that session.
    """
    session_id = request.path_params["session_id"]
    db_path = home_dir() / "metrics.db"
    try:
        result = await asyncio.to_thread(_query_metrics, db_path, session_id=session_id)
    except KeyError:
        return JSONResponse(
            {"error": f"No metrics found for session '{session_id}'"},
            status_code=404,
        )
    return Response(msgspec.json.encode(result), media_type="application/json")


async def auth_providers(request: Request) -> Response:
    service = request.app.state.auth_service
    return Response(msgspec.json.encode(service.providers()), media_type="application/json")


async def auth_status(request: Request) -> Response:
    service = request.app.state.auth_service
    result = [service.status(name) for name in service.provider_names()]
    return Response(msgspec.json.encode(result), media_type="application/json")


async def auth_credential_put(request: Request) -> Response:
    service = request.app.state.auth_service
    name = request.path_params["provider"]
    try:
        status = service.replace_static(name, await request.body())
    except AuthError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
    return Response(msgspec.json.encode(status), media_type="application/json")


async def auth_login(request: Request) -> Response:
    service = request.app.state.auth_service
    provider = request.path_params["provider"]
    origin = f"{request.url.scheme}://{request.url.netloc}"
    try:
        redirect_uri = service.redirect_uri(origin, provider)
        flow, authorization_url = await service.start_login(provider, redirect_uri)
    except AuthError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
    except (httpx.HTTPError, ValueError, TypeError):
        return JSONResponse({"error": "OAuth provider unavailable"}, status_code=502)
    from archie_shared.credentials.api import OAuthLoginResponse

    return Response(
        msgspec.json.encode(OAuthLoginResponse(flow.flow_id, authorization_url, redirect_uri)),
        media_type="application/json",
    )


async def auth_refresh(request: Request) -> Response:
    try:
        result = await request.app.state.auth_service.refresh(request.path_params["provider"])
    except AuthError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
    except (httpx.HTTPError, ValueError, TypeError):
        return JSONResponse({"error": "OAuth provider unavailable"}, status_code=502)
    return Response(msgspec.json.encode(result), media_type="application/json")


async def auth_flow(request: Request) -> Response:
    try:
        result = request.app.state.auth_service.flow_status(request.path_params["flow_id"])
    except AuthError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
    return Response(msgspec.json.encode(result), media_type="application/json")


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


async def auth_credential_delete(request: Request) -> Response:
    service = request.app.state.auth_service
    try:
        status = service.delete(request.path_params["provider"])
    except AuthError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
    return Response(msgspec.json.encode(status), media_type="application/json")


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
        Route("/health", health),
        Route("/sessions", sessions_get, methods=["GET"]),
        Route("/sessions", sessions_post, methods=["POST"]),
        Route("/sessions/{session_id}", session_delete, methods=["DELETE"]),
        Route("/sessions/{session_id}/status", proxy_status, methods=["GET"]),
        Route("/sessions/{session_id}/events", proxy_events, methods=["GET"]),
        Route("/sessions/{session_id}/metrics", get_session_metrics, methods=["GET"]),
        Route("/metrics", get_metrics, methods=["GET"]),
        Route("/auth/providers", auth_providers, methods=["GET"]),
        Route("/auth/status", auth_status, methods=["GET"]),
        Route("/auth/login/{provider}", auth_login, methods=["POST"]),
        Route("/auth/refresh/{provider}", auth_refresh, methods=["POST"]),
        Route("/auth/callback/{provider}", auth_callback, methods=["GET"]),
        Route("/auth/flow/{flow_id}", auth_flow, methods=["GET"]),
        Route("/auth/credential/{provider}", auth_credential_put, methods=["PUT"]),
        Route("/auth/credential/{provider}", auth_credential_delete, methods=["DELETE"]),
        WebSocketRoute("/sessions/{session_id}/stream", proxy_stream),
        Mount("/static", app=StaticFiles(directory=str(_STATIC_DIR)), name="static"),
    ],
    exception_handlers={Exception: unhandled_exception_handler},
)

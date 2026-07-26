"""Starlette application for the archie orchestrator.

Routes:
  GET    /health                          — liveness probe
  GET    /sessions                        — list running archie sessions
  POST   /sessions                        — start a new session
  DELETE /sessions/{session_id}           — stop a running session
  GET    /sessions/{session_id}/status    — proxy to session /status
  GET    /sessions/{session_id}/history   — proxy to session /history
  POST   /sessions/{session_id}/shell     — proxy to session /shell
  WS     /sessions/{session_id}/stream    — bidirectional WebSocket relay
"""

import asyncio
import logging
import sqlite3
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

import msgspec
from archie_shared.config import home_dir
from archie_shared.schemas import load_nexus_config
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute

from archie_orchestrator import configure_logging
from archie_orchestrator.docker import DockerError, list_sessions
from archie_orchestrator.lifecycle import start_session, stop_session
from archie_orchestrator.metrics import MetricsWriter
from archie_orchestrator.proxy import (
    get_active_ws_connections,
    proxy_history,
    proxy_shell,
    proxy_status,
    proxy_stream,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lifespan — load config once at startup
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: Starlette):
    """Load NexusConfig at startup and store on app state."""
    configure_logging()
    app.state.config = load_nexus_config()
    sessions = list_sessions()
    log.info("Discovered %d running sessions", len(sessions))

    # Start metrics writer background task
    db_path = home_dir() / "metrics.db"
    writer = MetricsWriter(db_path)
    app.state.metrics_writer = writer
    metrics_task = asyncio.create_task(writer.run())

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
            f"  COALESCE(SUM(cost), 0.0) AS total_cost, "
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
            f"SELECT model, "
            f"  COALESCE(SUM(cost), 0.0) AS cost, "
            f"  COUNT(*) AS requests "
            f"FROM requests {where} "
            f"GROUP BY model",
            params,
        ).fetchall()

        by_model = {
            r["model"]: {"cost": r["cost"], "requests": r["requests"]}
            for r in model_rows
        }

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
        since=YYYY-MM-DD  — filter to entries captured on or after this date.
    """
    since = request.query_params.get("since")
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


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch unexpected exceptions, log with origin, return 500."""
    tb = traceback.extract_tb(exc.__traceback__)
    origin = f"{tb[-1].filename}:{tb[-1].lineno}" if tb else "unknown"
    log.error("Unhandled error: %s (at %s)", exc, origin)
    return JSONResponse(
        {"error": "Internal server error", "detail": str(exc)},
        status_code=500,
    )


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/health", health),
        Route("/sessions", sessions_get, methods=["GET"]),
        Route("/sessions", sessions_post, methods=["POST"]),
        Route("/sessions/{session_id}", session_delete, methods=["DELETE"]),
        Route("/sessions/{session_id}/status", proxy_status, methods=["GET"]),
        Route("/sessions/{session_id}/history", proxy_history, methods=["GET"]),
        Route("/sessions/{session_id}/shell", proxy_shell, methods=["POST"]),
        Route("/sessions/{session_id}/metrics", get_session_metrics, methods=["GET"]),
        Route("/metrics", get_metrics, methods=["GET"]),
        WebSocketRoute("/sessions/{session_id}/stream", proxy_stream),
    ],
    exception_handlers={Exception: unhandled_exception_handler},
)

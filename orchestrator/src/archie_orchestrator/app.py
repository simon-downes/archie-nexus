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
import traceback
from contextlib import asynccontextmanager

import msgspec
from archie_shared.schemas import load_nexus_config
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute

from archie_orchestrator import configure_logging
from archie_orchestrator.docker import DockerError, list_sessions
from archie_orchestrator.lifecycle import start_session, stop_session
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
    yield
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
        WebSocketRoute("/sessions/{session_id}/stream", proxy_stream),
    ],
    exception_handlers={Exception: unhandled_exception_handler},
)

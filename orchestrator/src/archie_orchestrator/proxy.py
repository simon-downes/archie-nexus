"""Reverse-proxy helpers for the orchestrator.

Forwards HTTP and WebSocket traffic from the orchestrator to the target
session's agent container. The orchestrator is the single ingress for all
client↔session traffic (D8).
"""

from __future__ import annotations

import asyncio
import logging
import traceback

import httpx
import websockets
from archie_shared.session import SessionDescriptor
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.websockets import WebSocket, WebSocketDisconnect

from archie_orchestrator.docker import list_sessions

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Metrics frame inspection markers
# ---------------------------------------------------------------------------

# Cheap string checks match actual json.dumps output (space after colon).
_METRICS_MARKERS = ('"type":"llm_request"', '"type": "llm_request"')

# ---------------------------------------------------------------------------
# Active WebSocket connection counter (used by app.py shutdown log)
# ---------------------------------------------------------------------------

_active_ws_connections: int = 0


def get_active_ws_connections() -> int:
    """Return the current number of active WebSocket proxy connections."""
    return _active_ws_connections


# ---------------------------------------------------------------------------
# Session resolution
# ---------------------------------------------------------------------------


def _resolve_session(session_id: str) -> SessionDescriptor:
    """Find a running session by exact ID.

    Raises:
        KeyError: No session with that ID is running.
        ValueError: Session is running but has no port yet (still starting).
    """
    sessions = list_sessions()
    for s in sessions:
        if s.session_id == session_id:
            if s.port is None:
                raise ValueError(f"Session '{session_id}' has no port (still starting?)")
            return s
    raise KeyError(f"No session with ID '{session_id}'")


# ---------------------------------------------------------------------------
# HTTP forwarding
# ---------------------------------------------------------------------------


async def forward_http(
    session: SessionDescriptor,
    path: str,
    method: str = "GET",
    body: bytes = b"",
    content_type: str | None = None,
) -> Response:
    """Forward an HTTP request to a session container and return the response.

    Args:
        session: Resolved SessionDescriptor (must have a port).
        path: URL path on the agent (e.g. "/status").
        method: HTTP method ("GET" or "POST").
        body: Request body bytes (for POST).
        content_type: Content-Type header to forward with POST requests.

    Returns:
        A Starlette Response containing the proxied content.
    """
    target = f"http://127.0.0.1:{session.port}{path}"
    try:
        async with httpx.AsyncClient() as client:
            if method == "GET":
                resp = await client.get(target, timeout=5.0)
            else:
                headers = {}
                if content_type:
                    headers["content-type"] = content_type
                resp = await client.post(target, content=body, headers=headers, timeout=5.0)
    except httpx.ConnectError:
        return JSONResponse(
            {"error": f"Session '{session.session_id}' is unreachable"},
            status_code=502,
        )
    except httpx.TimeoutException:
        return JSONResponse(
            {"error": f"Session '{session.session_id}' timed out"},
            status_code=504,
        )

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )


# ---------------------------------------------------------------------------
# HTTP route handlers
# ---------------------------------------------------------------------------


async def proxy_status(request: Request) -> Response:
    """GET /sessions/{session_id}/status — proxy to session /status."""
    session_id = request.path_params["session_id"]
    try:
        session = _resolve_session(session_id)
    except KeyError:
        return JSONResponse({"error": f"No session with ID '{session_id}'"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return await forward_http(session, "/status")


async def proxy_events(request: Request) -> Response:
    """Proxy canonical event replay, forwarding the cursor unchanged."""
    session_id = request.path_params["session_id"]
    try:
        session = _resolve_session(session_id)
    except KeyError:
        return JSONResponse({"error": f"No session with ID '{session_id}'"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    after = request.query_params.get("after")
    path = "/events" + (f"?after={after}" if after is not None else "")
    return await forward_http(session, path)


# ---------------------------------------------------------------------------
# WebSocket proxy
# ---------------------------------------------------------------------------


async def proxy_stream(websocket: WebSocket) -> None:
    """WS /sessions/{session_id}/stream — bidirectional relay to session /stream."""
    session_id = websocket.path_params["session_id"]

    try:
        session = _resolve_session(session_id)
    except KeyError:
        await websocket.close(code=4004, reason="Session not found")
        return
    except ValueError:
        await websocket.close(code=4003, reason="Session not ready")
        return

    target_url = f"ws://127.0.0.1:{session.port}/stream"
    await websocket.accept()

    # Grab the metrics queue from app state (None if writer not wired).
    # websocket.app and .app.state always exist in Starlette, so only the
    # final attribute (metrics_writer) needs a guard.
    _writer = getattr(websocket.app.state, "metrics_writer", None)
    metrics_queue = _writer.queue if _writer is not None else None

    log.info("Client connected: %s", session_id)

    global _active_ws_connections
    _active_ws_connections += 1
    try:
        async with websockets.connect(target_url, open_timeout=10) as backend:

            async def client_to_backend() -> None:
                try:
                    async for msg in websocket.iter_text():
                        await backend.send(msg)
                except WebSocketDisconnect:
                    pass

            async def backend_to_client() -> None:
                async for msg in backend:
                    if isinstance(msg, str):
                        # Cheap marker check — enqueue metrics events without blocking relay
                        if metrics_queue is not None and any(
                            marker in msg for marker in _METRICS_MARKERS
                        ):
                            try:
                                metrics_queue.put_nowait((session_id, msg))
                            except Exception:  # noqa: BLE001 — metrics must never affect relay
                                pass
                        await websocket.send_text(msg)
                    else:
                        await websocket.send_bytes(msg)

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client_to_backend()),
                    asyncio.create_task(backend_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    log.warning("WS relay task error for %s: %s", session_id, exc)
            # Surface (log) any exception from the task that completed first,
            # otherwise backend relay errors are silently swallowed.
            for task in done:
                try:
                    task.result()
                except (asyncio.CancelledError, WebSocketDisconnect):
                    pass
                except Exception as exc:  # noqa: BLE001
                    log.warning("WS relay task error for %s: %s", session_id, exc)

    except (ConnectionRefusedError, OSError) as exc:
        log.warning("Backend unreachable for %s: %s", session_id, exc)
        try:
            await websocket.close(code=4002, reason="Session unreachable")
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001 — catch-all for unexpected errors
        tb = traceback.extract_tb(exc.__traceback__)
        origin = f"{tb[-1].filename}:{tb[-1].lineno}" if tb else "unknown"
        log.error("WS proxy error for %s: %s (at %s)", session_id, exc, origin)
        try:
            await websocket.close(code=1011, reason="Internal error")
        except Exception:
            pass
    finally:
        _active_ws_connections -= 1
        log.info("Client disconnected: %s", session_id)

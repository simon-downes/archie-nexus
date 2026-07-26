"""Reverse-proxy helpers for the orchestrator.

Forwards HTTP and WebSocket traffic from the orchestrator to the target
session's agent container. The orchestrator is the single ingress for all
client↔session traffic (D8).
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import websockets
from archie_shared.session import SessionDescriptor
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.websockets import WebSocket, WebSocketDisconnect

from archie_orchestrator.docker import list_sessions

log = logging.getLogger(__name__)


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


async def proxy_history(request: Request) -> Response:
    """GET /sessions/{session_id}/history — proxy to session /history."""
    session_id = request.path_params["session_id"]
    try:
        session = _resolve_session(session_id)
    except KeyError:
        return JSONResponse({"error": f"No session with ID '{session_id}'"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return await forward_http(session, "/history")


async def proxy_shell(request: Request) -> Response:
    """POST /sessions/{session_id}/shell — proxy to session /shell."""
    session_id = request.path_params["session_id"]
    try:
        session = _resolve_session(session_id)
    except KeyError:
        return JSONResponse({"error": f"No session with ID '{session_id}'"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    body = await request.body()
    content_type = request.headers.get("content-type")
    return await forward_http(session, "/shell", method="POST", body=body, content_type=content_type)


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

    try:
        async with websockets.connect(target_url) as backend:

            async def client_to_backend() -> None:
                try:
                    async for msg in websocket.iter_text():
                        await backend.send(msg)
                except WebSocketDisconnect:
                    pass

            async def backend_to_client() -> None:
                async for msg in backend:
                    if isinstance(msg, str):
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
                except (asyncio.CancelledError, Exception):
                    pass

    except (ConnectionRefusedError, OSError):
        try:
            await websocket.close(code=4002, reason="Session unreachable")
        except Exception:
            pass

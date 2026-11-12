"""Reverse-proxy helpers for the orchestrator.

Forwards HTTP and WebSocket traffic from the orchestrator to the target
session's agent container. The orchestrator is the single ingress for all
client↔session traffic (D8).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import websockets
from archie_shared.events import encode_event
from archie_shared.session import TERMINAL_STATUSES
from archie_shared.session.log import (
    CursorNotFound,
    EventIdConflict,
    LogAppendError,
    SessionLog,
)
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

if TYPE_CHECKING:
    from archie_orchestrator.tracker import SessionTracker

log = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class AgentEndpoint:
    """Where a running session's agent listens on the host loopback."""

    session_id: str
    port: int


def resolve_session(tracker: SessionTracker, session_id: str) -> AgentEndpoint:
    """Find a running session's agent by exact ID, from the tracker's memory (no Docker).

    Raises:
        KeyError: No session with that ID is running.
        ValueError: Session is running but has no port yet (still starting).
    """
    if tracker.live(session_id) is None:
        raise KeyError(f"No session with ID '{session_id}'")
    port = tracker.port(session_id)
    if port is None:
        raise ValueError(f"Session '{session_id}' has no port (still starting?)")
    return AgentEndpoint(session_id=session_id, port=port)


# ---------------------------------------------------------------------------
# HTTP forwarding
# ---------------------------------------------------------------------------


async def forward_http(
    session: AgentEndpoint,
    path: str,
    method: str = "GET",
    body: bytes = b"",
    content_type: str | None = None,
) -> Response:
    """Forward an HTTP request to a session container and return the response.

    Args:
        session: The resolved agent endpoint.
        path: URL path on the agent (e.g. "/events").
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
    except httpx.TimeoutException:
        return JSONResponse(
            {"error": f"Session '{session.session_id}' timed out"},
            status_code=504,
        )
    except httpx.TransportError:
        return JSONResponse(
            {"error": f"Session '{session.session_id}' is unreachable"},
            status_code=502,
        )

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )


# ---------------------------------------------------------------------------
# HTTP route handlers
# ---------------------------------------------------------------------------


def _read_log(path: Path, after: str | None) -> str:
    """A session log's canonical events after ``after`` as NDJSON, as the agent serves them."""
    return "".join(encode_event(event) + "\n" for event in SessionLog(path).read(after))


async def _ended_events(
    tracker: SessionTracker, session_id: str, after: str | None
) -> Response | None:
    """Replay an ended session's events from its log (082 D21); None if it hasn't ended.

    A session with no log (a ``failed`` start) has no events: 200 with an empty body.
    """
    try:
        record = await tracker.get(session_id)
    except sqlite3.Error as exc:
        log.error("Session store read failed: %s", exc)
        return JSONResponse({"error": "Session store unavailable"}, status_code=500)
    if record is None or record.status not in TERMINAL_STATUSES:
        return None
    try:
        body = await asyncio.to_thread(_read_log, tracker.log_path(session_id), after)
    except CursorNotFound:
        return JSONResponse({"error": "cursor_not_found", "cursor": after}, status_code=409)
    except (LogAppendError, EventIdConflict) as exc:
        log.warning("Failed to replay the log of %s: %s", session_id, exc)
        return JSONResponse({"error": str(exc)}, status_code=500)
    return Response(body, media_type="application/x-ndjson")


async def proxy_events(request: Request) -> Response:
    """Replay canonical events: proxied to a running agent, from the log once ended.

    The ``after`` cursor is forwarded unchanged; an ended session answers like the agent
    (NDJSON, 409 ``cursor_not_found`` for an unknown cursor). 404 for an unknown ID and
    503 for a session still starting.

    A load without ``after`` (a client opening the session, not resuming) titles a
    session that still has only its ID, in the background (plan 083 D4).
    """
    session_id = request.path_params["session_id"]
    tracker = request.app.state.tracker
    after = request.query_params.get("after")
    if after is None:
        tracker.title_if_untitled(session_id)
    try:
        session = resolve_session(tracker, session_id)
    except KeyError:
        ended = await _ended_events(tracker, session_id, after)
        if ended is not None:
            return ended
        return JSONResponse({"error": f"No session with ID '{session_id}'"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    path = "/events" + (f"?after={after}" if after is not None else "")
    return await forward_http(session, path)


# ---------------------------------------------------------------------------
# WebSocket proxy
# ---------------------------------------------------------------------------


async def proxy_stream(websocket: WebSocket) -> None:
    """WS /api/sessions/{session_id}/stream — bidirectional relay to session /stream."""
    session_id = websocket.path_params["session_id"]

    try:
        session = resolve_session(websocket.app.state.tracker, session_id)
    except KeyError:
        await websocket.close(code=4004, reason="Session not found")
        return
    except ValueError:
        await websocket.close(code=4003, reason="Session not ready")
        return

    target_url = f"ws://127.0.0.1:{session.port}/stream"
    await websocket.accept()

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
                except ConnectionClosed:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("WS relay task error for %s: %s", session_id, exc)

    except (ConnectionRefusedError, OSError) as exc:
        log.warning("Backend unreachable for %s: %s", session_id, exc)
        try:
            await websocket.close(code=4002, reason="Session unreachable")
        except Exception:
            pass
    except (InvalidHandshake, ConnectionClosed) as exc:
        log.info("Backend disconnected for %s: %s", session_id, exc)
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

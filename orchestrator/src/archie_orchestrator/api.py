"""JSON API for the orchestrator, mounted at ``API_PREFIX`` (``/api``).

Callers are code: the CLI, the TUI and agents. Every handler returns JSON (or
relays the session WebSocket); none renders a template. Paths in :data:`routes`
are relative to the mount:

  GET    /sessions                        — session records (?include=ended for all)
  POST   /sessions                        — start a new session (202, starting)
  GET    /sessions/feed                   — SSE: a snapshot, then every session change
  GET    /sessions/{session_id}           — one session record
  PATCH  /sessions/{session_id}           — rename a session or mark it seen
  DELETE /sessions/{session_id}           — stop a running session (200, stopped)
  GET    /sessions/{session_id}/events    — proxy to session /events
  GET    /sessions/{session_id}/metrics   — metrics for one session
  WS     /sessions/{session_id}/stream    — bidirectional WebSocket relay
  GET    /metrics                         — aggregate metrics
  GET    /auth/providers                  — provider definitions
  GET    /auth/status                     — redacted credential status
  GET    /auth/flow/{flow_id}             — OAuth flow status
  POST   /auth/login/{provider}           — start OAuth login
  POST   /auth/refresh/{provider}         — refresh OAuth credentials
  PUT    /auth/credential/{provider}      — replace a static credential
  DELETE /auth/credential/{provider}      — remove a credential
"""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import httpx
import msgspec
from archie_shared.config import ConfigError, home_dir
from archie_shared.session import SessionDescriptor
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute

from archie_orchestrator import store
from archie_orchestrator.auth import AuthError
from archie_orchestrator.config_source import config_source
from archie_orchestrator.docker import DockerError
from archie_orchestrator.lifecycle import BACKENDS
from archie_orchestrator.proxy import proxy_events, proxy_stream
from archie_orchestrator.tracker import FeedSubscription, SessionConflictError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


NAME_MAX_CHARS = 80
PATCH_FIELDS = ("name", "unseen")
FEED_PING_INTERVAL = 15.0
"""Seconds without an event before the sessions feed sends a ``: ping`` comment."""
_INVALID_NAME = (
    f"Invalid 'name': expected a non-blank string of at most {NAME_MAX_CHARS} characters"
)


def _json(value: object, status_code: int = 200) -> Response:
    return Response(
        msgspec.json.encode(value), status_code=status_code, media_type="application/json"
    )


def _valid_name(value: object) -> str | None:
    """A session name trimmed, or None if it isn't a non-blank string of at most 80 chars."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or len(name) > NAME_MAX_CHARS:
        return None
    return name


async def sessions_get(request: Request) -> Response:
    """Return session records as a JSON array of SessionDescriptors, newest first.

    Sessions that have not ended by default; ``?include=ended`` returns every record.
    Served from the tracker's memory (and the store for ended ones), never Docker.
    """
    include = request.query_params.get("include")
    if include not in (None, "ended"):
        return JSONResponse({"error": "Invalid 'include': expected 'ended'"}, status_code=400)
    try:
        records = await request.app.state.tracker.list(include_ended=include == "ended")
    except sqlite3.Error as exc:
        log.error("Session store read failed: %s", exc)
        return JSONResponse({"error": "Session store unavailable"}, status_code=500)
    return _json(records)


async def sessions_feed(request: Request) -> Response:
    """Stream session records as server-sent events (082 D20).

    On connect one ``snapshot`` event, ``data: {"sessions": [...]}``, holds what
    ``GET /api/sessions`` would return for the same ``include``. Then every change the
    tracker publishes (D11) is one ``session`` event with the full SessionDescriptor,
    whatever the filter, so a client sees a session's final terminal record. A ``: ping``
    comment is sent after :data:`FEED_PING_INTERVAL` seconds without an event. The stream
    ends if the client falls too far behind; it reconnects to a fresh snapshot.

    Returns:
        200 ``text/event-stream``; 400 for an ``include`` other than ``ended``; 500 if
        the store can't be read for the snapshot.
    """
    include = request.query_params.get("include")
    if include not in (None, "ended"):
        return JSONResponse({"error": "Invalid 'include': expected 'ended'"}, status_code=400)
    tracker = request.app.state.tracker
    # Subscribe first, so a change made while the snapshot is read isn't lost (D20).
    subscription = tracker.subscribe_feed()
    try:
        records = await tracker.list(include_ended=include == "ended")
    except sqlite3.Error as exc:
        subscription.close()
        log.error("Session store read failed: %s", exc)
        return JSONResponse({"error": "Session store unavailable"}, status_code=500)
    return StreamingResponse(
        _feed_events(subscription, records),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _feed_message(event: str, value: object) -> str:
    # Compact JSON escapes every line break, so the data is always a single line.
    return f"event: {event}\ndata: {msgspec.json.encode(value).decode()}\n\n"


async def _feed_events(
    subscription: FeedSubscription, records: list[SessionDescriptor]
) -> AsyncIterator[str]:
    """The feed body; closes the subscription when the stream ends or is cancelled."""
    try:
        yield _feed_message("snapshot", {"sessions": records})
        while True:
            try:
                record = await asyncio.wait_for(subscription.get(), FEED_PING_INTERVAL)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if record is None:
                log.info("Sessions feed: subscriber dropped after falling behind")
                return
            yield _feed_message("session", record)
    finally:
        subscription.close()


async def session_get(request: Request) -> Response:
    """Return one session record, or 404 for an unknown ID."""
    session_id = request.path_params["session_id"]
    try:
        record = await request.app.state.tracker.get(session_id)
    except sqlite3.Error as exc:
        log.error("Session store read failed: %s", exc)
        return JSONResponse({"error": "Session store unavailable"}, status_code=500)
    if record is None:
        return JSONResponse({"error": f"No session with ID '{session_id}'"}, status_code=404)
    return _json(record)


async def sessions_post(request: Request) -> Response:
    """Start a new agent session.

    Request body: ``{"workspace": "<name>", "backend": "native" | "kiro", "name": "<name>"}``
    (``backend`` and ``name`` optional). The container starts in the background; poll
    ``GET /api/sessions/{id}`` until the status leaves ``starting``.

    Returns:
        202 + the ``starting`` SessionDescriptor.
        400 for a bad body, workspace, backend or name (not a string, blank after
        trimming, or over 80 characters).
        500 for a config error, a missing image or a failed record insert.
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

    backend = data.get("backend", "native")
    if backend not in BACKENDS:
        return JSONResponse(
            {"error": f"Invalid backend {backend!r}: expected 'native' or 'kiro'"},
            status_code=400,
        )

    name = None
    if data.get("name") is not None:
        name = _valid_name(data["name"])
        if name is None:
            return JSONResponse({"error": _INVALID_NAME}, status_code=400)

    try:
        config = config_source.get()
    except ConfigError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    try:
        record = await request.app.state.tracker.create(
            workspace, backend=backend, name=name, config=config
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except (DockerError, RuntimeError, OSError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    except sqlite3.Error as exc:
        log.error("Session record insert failed: %s", exc)
        return JSONResponse({"error": "Session store unavailable"}, status_code=500)

    return _json(record, status_code=202)


async def session_patch(request: Request) -> Response:
    """Rename a session or mark it seen; allowed on ended sessions too (082 D18).

    Request body: a JSON object with ``name`` and/or ``unseen``. ``name`` is a string
    (stored trimmed; sets ``name_fixed``) or null (resets the name to the session ID);
    ``unseen`` must be false.

    Returns:
        200 + the updated SessionDescriptor.
        400 for a body that isn't such an object (unknown keys, no keys, an invalid name,
        ``unseen`` other than false).
        404 for an unknown ID.
        500 if the change could not be written.
    """
    session_id = request.path_params["session_id"]
    try:
        data = msgspec.json.decode(await request.body(), type=dict)
    except Exception:
        return JSONResponse({"error": "Invalid JSON body: expected an object"}, status_code=400)
    unknown = sorted(set(data) - set(PATCH_FIELDS))
    if unknown:
        return JSONResponse(
            {"error": f"Unknown fields: {', '.join(unknown)}; expected 'name' or 'unseen'"},
            status_code=400,
        )
    if not data:
        return JSONResponse({"error": "Expected 'name' or 'unseen'"}, status_code=400)

    changes: dict[str, object] = {}
    if "name" in data:
        if data["name"] is None:
            changes["name"] = None
        else:
            name = _valid_name(data["name"])
            if name is None:
                return JSONResponse({"error": _INVALID_NAME}, status_code=400)
            changes["name"] = name
    if "unseen" in data:
        if data["unseen"] is not False:
            return JSONResponse(
                {"error": "Invalid 'unseen': only false is accepted"}, status_code=400
            )
        changes["unseen"] = False

    try:
        record = await request.app.state.tracker.patch(session_id, **changes)
    except KeyError:
        return JSONResponse({"error": f"No session with ID '{session_id}'"}, status_code=404)
    except sqlite3.Error as exc:
        log.error("Session store write failed: %s", exc)
        return JSONResponse({"error": "Session store unavailable"}, status_code=500)
    return _json(record)


async def session_delete(request: Request) -> Response:
    """Stop a running session by exact session ID and wait for it (082 D19).

    Returns:
        200 + the ``stopped`` SessionDescriptor. A session that never received a prompt
        leaves no record afterwards (D19a).
        404 for an unknown ID.
        409 for a session that is ``starting``, ``stopping`` or has ended.
        500 if the container could not be stopped (its previous status is restored).
    """
    session_id = request.path_params["session_id"]
    try:
        record = await request.app.state.tracker.stop(session_id)
    except KeyError:
        return JSONResponse({"error": f"No session with ID '{session_id}'"}, status_code=404)
    except SessionConflictError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except (DockerError, RuntimeError, OSError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    except sqlite3.Error as exc:
        log.error("Session store read failed: %s", exc)
        return JSONResponse({"error": "Session store unavailable"}, status_code=500)
    return _json(record)


# ---------------------------------------------------------------------------
# Metrics API
# ---------------------------------------------------------------------------


def _query_metrics(
    db_path: Path,
    session_id: str | None = None,
    since: str | None = None,
) -> dict:
    """Query aggregated metrics from ``metrics_requests``. Returns a metrics dict.

    Args:
        db_path: Path to ``archie.db``.
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
            f"FROM metrics_requests {where}",
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
            f"FROM metrics_requests {where} "
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
    """GET /api/metrics — aggregate metrics across all sessions.

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
    result = await asyncio.to_thread(_query_metrics, store.db_path(home_dir()), since=since)
    return Response(msgspec.json.encode(result), media_type="application/json")


async def get_session_metrics(request: Request) -> Response:
    """GET /api/sessions/{session_id}/metrics — metrics for a single session.

    Returns 404 if no metrics have been recorded for that session.
    """
    session_id = request.path_params["session_id"]
    try:
        result = await asyncio.to_thread(
            _query_metrics, store.db_path(home_dir()), session_id=session_id
        )
    except KeyError:
        return JSONResponse(
            {"error": f"No metrics found for session '{session_id}'"},
            status_code=404,
        )
    return Response(msgspec.json.encode(result), media_type="application/json")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


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


async def auth_credential_delete(request: Request) -> Response:
    service = request.app.state.auth_service
    try:
        status = service.delete(request.path_params["provider"])
    except AuthError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
    return Response(msgspec.json.encode(status), media_type="application/json")


routes = [
    Route("/sessions", sessions_get, methods=["GET"]),
    Route("/sessions", sessions_post, methods=["POST"]),
    Route("/sessions/feed", sessions_feed, methods=["GET"]),  # before /sessions/{session_id}
    Route("/sessions/{session_id}", session_get, methods=["GET"]),
    Route("/sessions/{session_id}", session_patch, methods=["PATCH"]),
    Route("/sessions/{session_id}", session_delete, methods=["DELETE"]),
    Route("/sessions/{session_id}/events", proxy_events, methods=["GET"]),
    Route("/sessions/{session_id}/metrics", get_session_metrics, methods=["GET"]),
    WebSocketRoute("/sessions/{session_id}/stream", proxy_stream),
    Route("/metrics", get_metrics, methods=["GET"]),
    Route("/auth/providers", auth_providers, methods=["GET"]),
    Route("/auth/status", auth_status, methods=["GET"]),
    Route("/auth/flow/{flow_id}", auth_flow, methods=["GET"]),
    Route("/auth/login/{provider}", auth_login, methods=["POST"]),
    Route("/auth/refresh/{provider}", auth_refresh, methods=["POST"]),
    Route("/auth/credential/{provider}", auth_credential_put, methods=["PUT"]),
    Route("/auth/credential/{provider}", auth_credential_delete, methods=["DELETE"]),
]

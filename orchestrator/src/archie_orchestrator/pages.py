"""Page routes for the orchestrator, served at the root.

Callers are browsers. Every handler returns HTML (or server-sent HTML
fragments); none returns JSON. htmx fragments are served by their page: the page
URL returns a fragment when ``HX-Request`` is set, and a fragment with no page
of its own is a sub-path of its page (``/sessions/{id}/live``).

  GET /                                 — running sessions
  GET /sessions                         — session log viewer (list and search)
  GET /sessions/{session_id}            — session transcript
  GET /sessions/{session_id}/live       — live transcript fragments (SSE)
  GET /insights[/sessions[/{id}]|/models|/tools|/subagents|/errors] — usage insights
  GET /auth                             — credential status
  GET /auth/callback/{provider}         — OAuth callback (the browser lands here)
"""

import html
from datetime import UTC, datetime

import httpx
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from archie_orchestrator import insights_web
from archie_orchestrator.auth import AuthError
from archie_orchestrator.web import (
    auth_status_page,
    session_detail_page,
    session_list_page,
    session_live,
    sessions_page,
)


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


routes = [
    Route("/", sessions_page, methods=["GET"]),
    Route("/sessions", session_list_page, methods=["GET"]),
    Route("/sessions/{session_id}", session_detail_page, methods=["GET"]),
    Route("/sessions/{session_id}/live", session_live, methods=["GET"]),
    Route("/insights", insights_web.overview_page, methods=["GET"]),
    Route("/insights/sessions", insights_web.sessions_page, methods=["GET"]),
    Route("/insights/sessions/{session_id}", insights_web.session_page, methods=["GET"]),
    Route("/insights/models", insights_web.models_page, methods=["GET"]),
    Route("/insights/tools", insights_web.tools_page, methods=["GET"]),
    Route("/insights/subagents", insights_web.subagents_page, methods=["GET"]),
    Route("/insights/errors", insights_web.errors_page, methods=["GET"]),
    Route("/auth", auth_status_page, methods=["GET"]),
    Route("/auth/callback/{provider}", auth_callback, methods=["GET"]),
]

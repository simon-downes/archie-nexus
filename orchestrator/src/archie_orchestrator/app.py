"""Starlette application for the archie orchestrator.

Routes:
  GET /health   — liveness probe; returns {"status": "ok"}
  GET /sessions — list running archie sessions as JSON-serialized SessionDescriptors
"""

import msgspec
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from archie_orchestrator.docker import list_sessions


async def health(request: Request) -> JSONResponse:
    """Liveness probe — always returns 200 OK."""
    return JSONResponse({"status": "ok"})


async def sessions(request: Request) -> Response:
    """Return running archie sessions as a JSON array of SessionDescriptors."""
    result = list_sessions()
    content = msgspec.json.encode(result)
    return Response(content, media_type="application/json")


app = Starlette(
    routes=[
        Route("/health", health),
        Route("/sessions", sessions),
    ]
)

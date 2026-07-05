"""Archie agent — Starlette application."""

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


async def status(request: Request) -> JSONResponse:
    """Health check endpoint."""
    return JSONResponse({"status": "ok"})


app = Starlette(
    routes=[
        Route("/status", status, methods=["GET"]),
    ],
)

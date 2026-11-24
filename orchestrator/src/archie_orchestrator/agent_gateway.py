"""Restricted HTTP gateway for agent-originated orchestrator requests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

import httpx

log = logging.getLogger(__name__)

GATEWAY_PORT_OFFSET = 1
UPSTREAM_TIMEOUT_SECONDS = 5.0
MAX_REQUEST_BODY_BYTES = 10 * 1024 * 1024
_HOP_BY_HOP_HEADERS = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailers",
        b"transfer-encoding",
        b"upgrade",
    }
)


def gateway_port(orchestrator_port: int) -> int:
    """Return the deterministic agent gateway port for an orchestrator port."""
    port = orchestrator_port + GATEWAY_PORT_OFFSET
    if port > 65535:
        raise ValueError(
            f"Agent gateway port exceeds 65535 for orchestrator port {orchestrator_port}"
        )
    return port


def _forward_headers(headers: Iterable[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """Remove headers that must not cross the gateway boundary."""
    values = list(headers)
    nominated = {
        header_value.strip().lower()
        for name, header_value in values
        if name.lower() == b"connection"
        for header_value in header_value.split(b",")
    }
    return [
        (name, value)
        for name, value in values
        if name.lower() not in _HOP_BY_HOP_HEADERS
        and name.lower() != b"host"
        and name.lower() not in nominated
    ]


def _response_headers(response: httpx.Response) -> list[tuple[bytes, bytes]]:
    """Return safe response headers for the body representation HTTPX produced."""
    headers = _forward_headers(response.headers.raw)
    if response.headers.get("content-encoding"):
        headers = [
            (name, value)
            for name, value in headers
            if name.lower() not in {b"content-encoding", b"content-length"}
        ]
    return headers


def _url_host(host: str) -> str:
    """Format an IPv4/hostname or IPv6 host for an HTTP URL."""
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def upstream_url(host: str, port: int) -> str:
    """Build the loopback/main-listener URL used by the gateway."""
    return f"http://{_url_host(host)}:{port}"


class RequestBodyTooLargeError(Exception):
    """The gateway request exceeded its bounded body size."""


class AgentGateway:
    """ASGI gateway exposing only health and agent-specific orchestrator routes."""

    def __init__(
        self,
        upstream: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = UPSTREAM_TIMEOUT_SECONDS,
    ) -> None:
        self._upstream = upstream.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout, trust_env=False)
        for header in _HOP_BY_HOP_HEADERS:
            self._client.headers.pop(header.decode(), None)
        self._owns_client = client is None

    async def close(self) -> None:
        """Close the production HTTP client."""
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _allowed(scope: dict[str, Any]) -> bool:
        """Return whether an ASGI HTTP request is inside the gateway allowlist."""
        if scope["type"] != "http":
            return False
        path = scope["path"]
        return (scope["method"] == "GET" and path == "/health") or path.startswith("/api/agent/")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if not self._allowed(scope):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await self._respond(send, 404, b'{"error":"not found"}')
            return

        try:
            body = await asyncio.wait_for(
                self._read_body(receive), timeout=UPSTREAM_TIMEOUT_SECONDS
            )
            query = scope.get("query_string", b"").decode("ascii")
        except TimeoutError:
            await self._respond(send, 408, b'{"error":"request body timeout"}')
            return
        except RequestBodyTooLargeError:
            await self._respond(send, 413, b'{"error":"request body too large"}')
            return
        except UnicodeDecodeError:
            await self._respond(send, 400, b'{"error":"invalid query"}')
            return

        path = scope["path"]
        target = f"{self._upstream}{path}"
        if query:
            target += "?" + query
        try:
            response = await self._client.request(
                scope["method"],
                target,
                headers=_forward_headers(scope.get("headers", [])),
                content=body,
            )
        except httpx.HTTPError:
            log.warning("Agent gateway upstream unavailable: %s %s", scope["method"], path)
            await self._respond(send, 502, b'{"error":"upstream unavailable"}')
            return

        await send(
            {
                "type": "http.response.start",
                "status": response.status_code,
                "headers": _response_headers(response),
            }
        )
        await send({"type": "http.response.body", "body": response.content})

    @staticmethod
    async def _read_body(receive: Any) -> bytes:
        """Read an ASGI request body, supporting the normal chunked form."""
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunks.append(message.get("body", b""))
            if sum(map(len, chunks)) > MAX_REQUEST_BODY_BYTES:
                raise RequestBodyTooLargeError
            if not message.get("more_body", False):
                break
        return b"".join(chunks)

    @staticmethod
    async def _respond(send: Any, status: int, body: bytes) -> None:
        """Send a small JSON response without logging its contents."""
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

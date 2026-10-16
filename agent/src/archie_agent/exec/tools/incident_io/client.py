"""Async, GET-only incident.io REST client with sanitized provider errors."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from archie_shared.credentials.models import IncidentIoCredential
from archie_shared.credentials.store import get_credential

from archie_agent.exec.tools import ToolError

BASE_URL = "https://api.incident.io"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0, write=10.0, pool=10.0)
_DEADLINE_SECONDS = 60.0
_MAX_PAGES = 20


class IncidentIoError(ToolError):
    """Base class for incident.io tool failures."""


class IncidentIoConfigurationError(IncidentIoError):
    pass


class IncidentIoPolicyError(IncidentIoError):
    pass


class IncidentIoInputError(IncidentIoError):
    pass


class IncidentIoAuthenticationError(IncidentIoError):
    pass


class IncidentIoAuthorizationError(IncidentIoError):
    pass


class IncidentIoNotFoundError(IncidentIoError):
    pass


class IncidentIoRateLimitError(IncidentIoError):
    def __init__(self, retry_after_seconds: int | None):
        self.retry_after_seconds = retry_after_seconds
        suffix = (
            f"; retry after {retry_after_seconds} seconds"
            if retry_after_seconds is not None
            else ""
        )
        super().__init__(f"incident.io rate limit reached{suffix}")


class IncidentIoRequestRejectedError(IncidentIoError):
    pass


class IncidentIoResponseError(IncidentIoError):
    pass


class IncidentIoTransportError(IncidentIoError):
    pass


def parse_retry_after(value: Any) -> int | None:
    """Return a non-negative integer number of seconds, or None."""
    if not isinstance(value, str) or not value.strip().isdigit():
        return None
    return int(value.strip())


def _error_codes(response: httpx.Response) -> str:
    """Return provider error codes (never messages) for a sanitized error."""
    try:
        body = response.json()
    except ValueError:
        return ""
    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, list):
        return ""
    codes = sorted(
        {
            str(item["code"])[:64]
            for item in errors
            if isinstance(item, dict) and isinstance(item.get("code"), str)
        }
    )
    return f" ({', '.join(codes[:5])})" if codes else ""


def _credential() -> str:
    try:
        value = get_credential("incident_io", IncidentIoCredential)
    except Exception as exc:
        raise IncidentIoConfigurationError(
            "incident.io credentials are unavailable or invalid"
        ) from exc
    if not value or not isinstance(value.api_key, str) or not value.api_key.strip():
        raise IncidentIoConfigurationError("incident.io credentials are incomplete")
    return value.api_key.strip()


class IncidentIoClient:
    """Read-only client. It deliberately exposes no way to send a non-GET request."""

    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "archie-incident-io-tool/1.0",
        }
        self._transport = transport

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=BASE_URL,
                headers=self._headers,
                timeout=_TIMEOUT,
                transport=self._transport,
            ) as client:
                response = await client.get(path, params=params)
        except httpx.RequestError as exc:
            raise IncidentIoTransportError("incident.io request could not be completed") from exc
        status = response.status_code
        if status == 401:
            raise IncidentIoAuthenticationError("incident.io authentication failed")
        if status == 403:
            raise IncidentIoAuthorizationError("incident.io authorization denied")
        if status == 404:
            raise IncidentIoNotFoundError("incident.io resource was not found")
        if status == 429:
            raise IncidentIoRateLimitError(parse_retry_after(response.headers.get("Retry-After")))
        if status in {400, 422}:
            raise IncidentIoRequestRejectedError(
                "incident.io rejected the request" + _error_codes(response)
            )
        if status >= 400:
            raise IncidentIoResponseError(f"incident.io returned HTTP {status}")
        try:
            value = response.json()
        except ValueError as exc:
            raise IncidentIoResponseError("incident.io returned an invalid response") from exc
        if not isinstance(value, dict):
            raise IncidentIoResponseError("incident.io returned an invalid response")
        return value

    async def items(self, path: str, key: str, params: dict[str, Any] | None = None) -> list[dict]:
        """GET a non-paginated collection and return the list under ``key``."""
        data = await self.get(path, params)
        values = data.get(key)
        if not isinstance(values, list):
            raise IncidentIoResponseError("incident.io returned a malformed collection")
        return [item for item in values if isinstance(item, dict)]

    async def paginate(
        self,
        path: str,
        key: str,
        limit: int,
        params: dict[str, Any] | None = None,
        *,
        max_page_size: int = 100,
        keep: Callable[[dict], bool] | None = None,
    ) -> list[dict]:
        """Follow ``after`` cursors until ``limit`` kept items or the collection ends.

        ``keep`` filters items locally for filters the API does not support; at most
        ``_MAX_PAGES`` pages are read either way.
        """
        results: list[dict] = []
        after: str | None = None
        for _ in range(_MAX_PAGES):
            query = dict(params or {})
            query["page_size"] = max_page_size if keep else min(max_page_size, limit - len(results))
            if after:
                query["after"] = after
            data = await self.get(path, query)
            values = data.get(key)
            if not isinstance(values, list):
                raise IncidentIoResponseError("incident.io returned a malformed collection")
            results.extend(
                item for item in values if isinstance(item, dict) and (keep is None or keep(item))
            )
            meta = data.get("pagination_meta")
            after = meta.get("after") if isinstance(meta, dict) else None
            if len(results) >= limit or not values or not isinstance(after, str) or not after:
                break
        return results[:limit]


@asynccontextmanager
async def incident_io_client():
    """Yield a client under one overall deadline; a timeout is a transport error."""
    client = IncidentIoClient(_credential())
    try:
        async with asyncio.timeout(_DEADLINE_SECONDS):
            yield client
    except TimeoutError as exc:
        raise IncidentIoTransportError("incident.io request exceeded the time limit") from exc

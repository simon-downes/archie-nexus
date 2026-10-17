"""Async Datadog REST client limited to an allowlist of requests, with sanitized errors.

Datadog application-key scopes cannot forbid deletes, so the tool API enforces it: this client
sends only the ``(method, path)`` pairs in ``_ALLOWED``, and none of them is a DELETE. This guards
the tool interface; it is not process isolation (exec code runs in the same process).
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from typing import Any

import httpx
from archie_shared.credentials.models import DatadogCredential
from archie_shared.credentials.store import get_credential

from archie_agent.exec.tools import ToolError

_TIMEOUT = httpx.Timeout(30.0, connect=10.0, write=10.0, pool=10.0)
_DEADLINE_SECONDS = 60.0
MAX_ERROR = 300

SITES = frozenset(
    {
        "datadoghq.com",
        "us3.datadoghq.com",
        "us5.datadoghq.com",
        "datadoghq.eu",
        "ap1.datadoghq.com",
        "ap2.datadoghq.com",
        "ddog-gov.com",
    }
)

_MONITOR = r"[1-9][0-9]{0,18}"
DASHBOARD_ID = r"[a-z0-9]{3}-[a-z0-9]{3}-[a-z0-9]{3}"
METRIC = r"[A-Za-z][A-Za-z0-9_.]{0,199}"
_ALLOWED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        ("GET", r"/api/v1/query"),
        ("GET", r"/api/v1/search"),
        ("GET", r"/api/v1/metrics"),
        ("GET", rf"/api/v1/metrics/{METRIC}"),
        ("GET", rf"/api/v2/metrics/{METRIC}/all-tags"),
        ("GET", rf"/api/v2/metrics/{METRIC}/assets"),
        ("POST", r"/api/v2/logs/events/search"),
        ("POST", r"/api/v2/spans/events/search"),
        ("GET", r"/api/v2/events"),
        ("GET", r"/api/v1/monitor/search"),
        ("GET", rf"/api/v1/monitor/{_MONITOR}"),
        ("POST", r"/api/v1/monitor/validate"),
        ("POST", r"/api/v1/monitor"),
        ("PUT", rf"/api/v1/monitor/{_MONITOR}"),
        ("GET", r"/api/v1/dashboard"),
        ("GET", rf"/api/v1/dashboard/{DASHBOARD_ID}"),
        ("GET", r"/api/v2/catalog/entity"),
        ("GET", r"/api/v2/team"),
    )
)


class DatadogError(ToolError):
    """Base class for Datadog tool failures."""


class DatadogConfigurationError(DatadogError):
    pass


class DatadogPolicyError(DatadogError):
    pass


class DatadogInputError(DatadogError):
    pass


class DatadogOwnershipError(DatadogError):
    pass


class DatadogAuthenticationError(DatadogError):
    pass


class DatadogAuthorizationError(DatadogError):
    pass


class DatadogNotFoundError(DatadogError):
    pass


class DatadogRateLimitError(DatadogError):
    def __init__(self, retry_after_seconds: int | None):
        self.retry_after_seconds = retry_after_seconds
        suffix = (
            f"; retry after {retry_after_seconds} seconds"
            if retry_after_seconds is not None
            else ""
        )
        super().__init__(f"Datadog rate limit reached{suffix}")


class DatadogRequestRejectedError(DatadogError):
    pass


class DatadogResponseError(DatadogError):
    pass


class DatadogTransportError(DatadogError):
    def __init__(self, message: str, *, transmitted: bool):
        super().__init__(message)
        self.transmitted = transmitted


class DatadogMutationIndeterminateError(DatadogError):
    pass


def allowed(method: str, path: str) -> bool:
    return any(method == m and pattern.fullmatch(path) for m, pattern in _ALLOWED)


def parse_reset(value: Any) -> int | None:
    """Return ``X-RateLimit-Reset`` (seconds until reset) as a non-negative int, or None."""
    if not isinstance(value, str) or not value.strip().isdigit():
        return None
    return int(value.strip())


def api_host(site: Any) -> str:
    """Map a Datadog site such as ``app.datadoghq.eu`` to its API host, or fail closed."""
    if not isinstance(site, str):
        raise DatadogConfigurationError("Datadog site is not configured")
    name = site.strip().lower().removeprefix("app.")
    if name not in SITES:
        raise DatadogConfigurationError("Datadog site is not a known Datadog site")
    return f"https://api.{name}"


def _site() -> str:
    try:
        from archie_shared.credentials.providers import effective_provider
        from archie_shared.schemas import load_nexus_config

        override = load_nexus_config().auth.providers.get("datadog")
        return effective_provider("datadog", override).site
    except Exception as exc:
        raise DatadogConfigurationError("Datadog site configuration could not be read") from exc


def _credential() -> tuple[str, str]:
    try:
        value = get_credential("datadog", DatadogCredential)
    except Exception as exc:
        raise DatadogConfigurationError("Datadog credentials are unavailable or invalid") from exc
    api_key = value.api_key.strip() if value and isinstance(value.api_key, str) else ""
    app_key = value.app_key.strip() if value and isinstance(value.app_key, str) else ""
    if not api_key or not app_key:
        raise DatadogConfigurationError("Datadog credentials are incomplete")
    return api_key, app_key


def _error_detail(response: httpx.Response, secrets: tuple[str, ...]) -> str:
    """Return the first provider error string, truncated, with any key text removed."""
    try:
        body = response.json()
    except ValueError:
        return ""
    errors = body.get("errors") if isinstance(body, dict) else None
    first: Any = errors[0] if isinstance(errors, list) and errors else None
    if isinstance(first, dict):
        first = first.get("detail") or first.get("title")
    if not isinstance(first, str) or not first.strip():
        return ""
    detail = first.strip()
    for secret in secrets:
        detail = detail.replace(secret, "[redacted]")
    return f": {detail[:MAX_ERROR]}"


class DatadogClient:
    """Send allowlisted Datadog requests; anything else raises before a request is made."""

    def __init__(
        self,
        api_key: str,
        app_key: str,
        host: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._secrets = (api_key, app_key)
        self._headers = {
            "DD-API-KEY": api_key,
            "DD-APPLICATION-KEY": app_key,
            "Accept": "application/json",
            "User-Agent": "archie-datadog-tool/1.0",
        }
        self._host = host
        self._transport = transport

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        if not allowed(method, path):
            raise DatadogPolicyError(f"Datadog request {method} {path} is not permitted")
        try:
            async with httpx.AsyncClient(
                base_url=self._host,
                headers=self._headers,
                timeout=_TIMEOUT,
                transport=self._transport,
            ) as client:
                response = await client.request(method, path, params=params, json=json)
        except httpx.ConnectError as exc:
            raise DatadogTransportError(
                "Datadog request could not be completed", transmitted=False
            ) from exc
        except httpx.RequestError as exc:
            raise DatadogTransportError(
                "Datadog request outcome is uncertain", transmitted=True
            ) from exc
        status = response.status_code
        if status == 401:
            raise DatadogAuthenticationError("Datadog authentication failed")
        if status == 403:
            raise DatadogAuthorizationError(
                "Datadog authorization denied" + _error_detail(response, self._secrets)
            )
        if status == 404:
            raise DatadogNotFoundError("Datadog resource was not found")
        if status == 429:
            raise DatadogRateLimitError(parse_reset(response.headers.get("X-RateLimit-Reset")))
        if status in {400, 422}:
            raise DatadogRequestRejectedError(
                "Datadog rejected the request" + _error_detail(response, self._secrets)
            )
        if status >= 400:
            raise DatadogResponseError(f"Datadog returned HTTP {status}")
        try:
            value = response.json()
        except ValueError as exc:
            raise DatadogResponseError("Datadog returned an invalid response") from exc
        if not isinstance(value, (dict, list)):
            raise DatadogResponseError("Datadog returned an invalid response")
        return value


@asynccontextmanager
async def datadog_client():
    """Yield a client under one overall deadline; a timeout is a transport error."""
    api_key, app_key = _credential()
    client = DatadogClient(api_key, app_key, api_host(_site()))
    try:
        async with asyncio.timeout(_DEADLINE_SECONDS):
            yield client
    except TimeoutError as exc:
        raise DatadogTransportError(
            "Datadog request exceeded the time limit", transmitted=True
        ) from exc

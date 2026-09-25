"""Shared offline-testable boundaries for Google Workspace tools."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import httpx
from archie_shared.credentials.models import OAuthCredential
from archie_shared.credentials.store import get_credential
from archie_shared.tool_policy import current_policy_snapshot, resolve_provider_policy

from archie_agent.exec.tools import ToolError


class GoogleError(ToolError):
    """Base class for sanitized Google tool failures."""


class GoogleValidationError(GoogleError):
    pass


class GoogleConfigurationError(GoogleError):
    pass


class GoogleAuthenticationError(GoogleError):
    pass


class GoogleAuthorizationError(GoogleError):
    pass


class GoogleNotFoundError(GoogleError):
    pass


class GoogleRateLimitError(GoogleError):
    pass


class GoogleTransportError(GoogleError):
    pass


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def text(value: Any, name: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str):
        raise GoogleValidationError(f"{name} must be a string")
    value = value.strip()
    if not value or len(value) > maximum or _CONTROL.search(value):
        raise GoogleValidationError(f"{name} must be 1-{maximum} safe characters")
    return value


def bounded_limit(value: Any, *, maximum: int, name: str = "limit") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise GoogleValidationError(f"{name} must be 1-{maximum}")
    return value


def safe_id(value: Any, name: str = "id", maximum: int = 256) -> str:
    return text(value, name, maximum=maximum)


def policy(snapshot: dict[str, Any] | None = None) -> tuple[bool, bool]:
    read, write = resolve_provider_policy(
        snapshot if snapshot is not None else current_policy_snapshot(), "google"
    )
    return bool(read["enabled"]), bool(write["enabled"])


def require_read(snapshot: dict[str, Any] | None = None) -> None:
    if not policy(snapshot)[0]:
        raise GoogleAuthorizationError("Google reads are disabled by policy")


def require_write(snapshot: dict[str, Any] | None = None) -> None:
    if not policy(snapshot)[1]:
        raise GoogleAuthorizationError("Google writes are disabled by policy")


def credential() -> OAuthCredential:
    try:
        value = get_credential("google")
    except Exception as exc:
        raise GoogleConfigurationError("Google credentials are unavailable or invalid") from exc
    if not isinstance(value, OAuthCredential) or not value.access_token:
        raise GoogleConfigurationError("Google credentials are incomplete")
    return value


def clean(value: Any, maximum: int = 500) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or _CONTROL.search(value):
        return None
    return value[:maximum]


def safe_url(value: Any) -> str | None:
    value = clean(value, 2000)
    if value is None:
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.netloc:
        return None
    return value


def safe_text(value: Any, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:maximum]


def _diagnostic(value: Any, maximum: int = 240) -> str | None:
    """Return bounded, single-line diagnostic text without control characters."""
    if not isinstance(value, str):
        return None
    value = _CONTROL.sub(" ", value).strip()
    return value[:maximum] or None


def _provider_error_detail(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    reasons: list[str] = []
    for item in error.get("errors", []) if isinstance(error.get("errors"), list) else []:
        if isinstance(item, dict):
            reason = _diagnostic(item.get("reason"), 80)
            if reason and reason not in reasons:
                reasons.append(reason)
    for item in error.get("details", []) if isinstance(error.get("details"), list) else []:
        if isinstance(item, dict):
            reason = _diagnostic(item.get("reason"), 80)
            if reason and reason not in reasons:
                reasons.append(reason)
    message = _diagnostic(error.get("message"))
    detail = ", ".join(reasons)
    if message:
        detail = f"{detail}: {message}" if detail else message
    return detail or None


async def _google_get(
    url: str,
    token: str,
    *,
    params: dict[str, Any] | None = None,
    method: str = "GET",
    json: dict[str, Any] | None = None,
    timeout: float = 30,
) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.request(
                method, url, params=params, json=json, headers={"Authorization": f"Bearer {token}"}
            )
    except httpx.TimeoutException as exc:
        raise GoogleTransportError(f"Google request timed out ({type(exc).__name__})") from exc
    except httpx.HTTPError as exc:
        detail = _diagnostic(str(exc))
        suffix = f": {detail}" if detail else ""
        raise GoogleTransportError(
            f"Google request failed ({type(exc).__name__}){suffix}"
        ) from exc
    if response.status_code == 401:
        raise GoogleAuthenticationError("Google authentication failed")
    if response.status_code == 403:
        detail = "Google authorization failed"
        try:
            payload = response.json()
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            message = error.get("message") if isinstance(error, dict) else None
            reason = None
            details = error.get("details", []) if isinstance(error, dict) else []
            if isinstance(details, list):
                for item in details:
                    if isinstance(item, dict) and isinstance(item.get("reason"), str):
                        reason = item["reason"]
                        break
            if isinstance(message, str) and message.strip():
                detail = message.strip()
            if reason:
                detail = f"{detail} ({reason})"
        except (ValueError, TypeError):
            pass
        raise GoogleAuthorizationError(detail)
    if response.status_code == 404:
        raise GoogleNotFoundError("Google resource was not found")
    if response.status_code == 429:
        raise GoogleRateLimitError("Google rate limit exceeded")
    if response.status_code >= 400:
        detail = _provider_error_detail(response)
        suffix = f": {detail}" if detail else ""
        raise GoogleTransportError(f"Google request failed (HTTP {response.status_code}){suffix}")
    return response


async def google_json(
    url: str,
    token: str,
    *,
    params: dict[str, Any] | None = None,
    method: str = "GET",
    json: dict[str, Any] | None = None,
    timeout: float = 30,
    allow_empty: bool = False,
) -> Any:
    """Fetch one bounded Google JSON response without exposing credentials."""
    response = await _google_get(
        url, token, params=params, method=method, json=json, timeout=timeout
    )
    if allow_empty and not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise GoogleTransportError("Google returned an invalid response") from exc


async def google_text(url: str, token: str, *, params: dict[str, Any] | None = None) -> str:
    """Fetch bounded textual Google content without exposing credentials."""
    response = await _google_get(url, token, params=params)
    return response.text

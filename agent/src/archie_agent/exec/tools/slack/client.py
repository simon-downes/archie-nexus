"""Single-attempt Slack Web API transport and sanitized error mapping."""

from __future__ import annotations

from typing import Any

import httpx
from archie_shared.credentials.models import OAuthCredential
from archie_shared.credentials.store import get_credential

from .errors import (
    SlackAuthenticationError,
    SlackConfigurationError,
    SlackRateLimitError,
    SlackReauthorizationRequiredError,
    SlackResponseError,
    SlackTransportError,
)

RTS_URL = "https://slack.com/api/assistant.search.context"
_API_URL = "https://slack.com/api/"


def parse_retry_after(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str) or not value.strip().isdigit():
        return None
    result = int(value.strip())
    return result if result >= 0 else None


def _credential() -> OAuthCredential:
    try:
        credential = get_credential("slack", OAuthCredential)
    except Exception as exc:
        raise SlackConfigurationError("Slack credentials are unavailable or invalid") from exc
    if not credential or not credential.access_token or not credential.access_token.strip():
        raise SlackConfigurationError("Slack credentials are incomplete")
    return credential


async def api_call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    credential = _credential()
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            headers = {
                "Authorization": f"Bearer {credential.access_token.strip()}",
                "Accept": "application/json",
                "User-Agent": "archie-slack-tool/1.0",
            }
            if method in {
                "users.conversations",
                "conversations.list",
                "users.list",
                "conversations.history",
                "conversations.replies",
                "reactions.get",
            }:
                query = dict(payload)
                if method in {"users.conversations", "conversations.list"}:
                    query.setdefault("exclude_muted", False)
                query = {
                    key: str(value).lower() if isinstance(value, bool) else value
                    for key, value in query.items()
                }
                response = await client.get(_API_URL + method, params=query, headers=headers)
            else:
                response = await client.post(
                    _API_URL + method,
                    json=payload,
                    headers={**headers, "Content-Type": "application/json"},
                )
    except httpx.HTTPError as exc:
        raise SlackTransportError("Slack request could not be completed") from exc
    retry_after = parse_retry_after(response.headers.get("Retry-After"))
    if response.status_code == 429:
        raise SlackRateLimitError(method, retry_after)
    if response.status_code in (401, 403):
        raise SlackAuthenticationError("Slack authentication failed")
    try:
        body = response.json()
    except ValueError as exc:
        raise SlackResponseError("Slack returned a malformed response") from exc
    if not isinstance(body, dict):
        raise SlackResponseError("Slack returned a malformed response")
    error = body.get("error")
    if error in {"ratelimited", "rate_limited"}:
        raise SlackRateLimitError(method, retry_after or parse_retry_after(body.get("retry_after")))
    if error == "missing_scope":
        needed = body.get("needed")
        scopes = [needed] if isinstance(needed, str) and needed else []
        raise SlackReauthorizationRequiredError(scopes)
    if error in {
        "account_inactive",
        "invalid_auth",
        "token_expired",
        "token_revoked",
        "token_denied",
        "not_authed",
        "two_factor_setup_required",
    }:
        raise SlackAuthenticationError("Slack authentication failed")
    return body


async def search_context(payload: dict[str, Any], *, required_scopes: list[str]) -> dict[str, Any]:
    del required_scopes  # Scope validation is supplied by the shared credential seam.
    return await api_call("assistant.search.context", payload)

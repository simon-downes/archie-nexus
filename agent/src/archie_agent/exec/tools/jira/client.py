"""Async Jira Cloud REST client with sanitized provider errors."""

from __future__ import annotations

import asyncio
import base64
import re
from contextlib import asynccontextmanager
from typing import Any

import httpx
from archie_shared.credentials.models import JiraCredential
from archie_shared.credentials.store import get_credential

from archie_agent.exec.tools import ToolError


class JiraConfigurationError(ToolError):
    pass


class JiraPolicyError(ToolError):
    pass


class JiraAuthenticationError(ToolError):
    pass


class JiraAuthorizationError(ToolError):
    pass


class JiraNotFoundError(ToolError):
    pass


class JiraRateLimitError(ToolError):
    pass


class JiraTransportError(ToolError):
    def __init__(self, message: str, *, transmitted: bool):
        super().__init__(message)
        self.transmitted = transmitted


class JiraValidationError(ToolError):
    pass


class JiraResponseError(ToolError):
    pass


_CLOUD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TIMEOUT = httpx.Timeout(30.0, connect=10.0, write=10.0, pool=10.0)


def _credential() -> JiraCredential:
    try:
        value = get_credential("jira")
    except Exception as exc:
        raise JiraConfigurationError("Jira credentials are unavailable or invalid") from exc
    if not value or not value.email or not value.token or not value.cloud_id:
        raise JiraConfigurationError("Jira credentials are incomplete")
    email, token, cloud_id = value.email.strip(), value.token.strip(), value.cloud_id.strip()
    if not email or not token or not _CLOUD_ID.fullmatch(cloud_id):
        raise JiraConfigurationError("Jira credentials are invalid")
    return JiraCredential(email=email, token=token, cloud_id=cloud_id)


class JiraClient:
    def __init__(self, credential: JiraCredential):
        self.base_url = f"https://api.atlassian.com/ex/jira/{credential.cloud_id}/rest/api/3"
        raw = f"{credential.email}:{credential.token}".encode()
        self.headers = {
            "Authorization": f"Basic {base64.b64encode(raw).decode()}",
            "Accept": "application/json",
        }

    @asynccontextmanager
    async def session(self):
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=self.headers) as client:
            yield client

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with self.session() as client:
                response = await client.request(method, self.base_url + path, **kwargs)
        except httpx.ConnectError as exc:
            raise JiraTransportError(
                "Jira request could not be completed", transmitted=False
            ) from exc
        except httpx.RequestError as exc:
            raise JiraTransportError("Jira request outcome is uncertain", transmitted=True) from exc
        if response.status_code == 401:
            raise JiraAuthenticationError("Jira authentication failed")
        if response.status_code == 403:
            raise JiraAuthorizationError("Jira authorization denied")
        if response.status_code == 404:
            raise JiraNotFoundError("Jira resource was not found")
        if response.status_code == 429:
            raise JiraRateLimitError("Jira rate limit exceeded")
        if response.status_code in {400, 422}:
            raise JiraValidationError("Jira rejected the request fields")
        if response.status_code >= 400:
            raise JiraResponseError("Jira rejected the request")
        if response.status_code == 204 or not response.content:
            return None
        try:
            value = response.json()
        except (ValueError, TypeError) as exc:
            raise JiraResponseError("Jira returned an invalid response") from exc
        if not isinstance(value, (dict, list)):
            raise JiraResponseError("Jira returned an invalid response")
        return value


@asynccontextmanager
async def jira_client():
    async with asyncio.timeout(60):
        yield JiraClient(_credential())

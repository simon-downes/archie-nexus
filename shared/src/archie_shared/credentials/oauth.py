"""OAuth2 + PKCE flow primitives for browser-based authentication.

This module requires httpx and is CLI-only (not imported by the agent container).
It is NOT re-exported from credentials/__init__.py — import directly:
    from archie_shared.credentials.oauth import ...
"""

import secrets
import webbrowser
from base64 import urlsafe_b64encode
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

CALLBACK_PORT = 8585
CALLBACK_TIMEOUT = 120
HTTP_TIMEOUT = 30


def generate_pkce() -> tuple[str, str]:
    """Generate PKCE code verifier and challenge (S256).

    Returns:
        Tuple of (verifier, challenge).
    """
    verifier = secrets.token_urlsafe(32)
    challenge = urlsafe_b64encode(sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def discover_endpoints(server_url: str) -> dict[str, Any]:
    """Discover OAuth endpoints via RFC 9470 + RFC 8414.

    First fetches the protected resource metadata to find the authorization
    server URL, then fetches the authorization server metadata.

    Args:
        server_url: The OAuth resource server base URL.

    Returns:
        Authorization server metadata dict (contains authorization_endpoint,
        token_endpoint, registration_endpoint, etc.).
    """
    resp = httpx.get(f"{server_url}/.well-known/oauth-protected-resource", timeout=HTTP_TIMEOUT)
    resp.raise_for_status()

    auth_server_url = resp.json()["authorization_servers"][0]

    resp = httpx.get(
        f"{auth_server_url}/.well-known/oauth-authorization-server", timeout=HTTP_TIMEOUT
    )
    resp.raise_for_status()

    return resp.json()


def register_client(registration_endpoint: str, redirect_uri: str) -> dict[str, Any]:
    """Register OAuth client dynamically (RFC 7591).

    Args:
        registration_endpoint: The OAuth client registration endpoint.
        redirect_uri: Callback URI for the client.

    Returns:
        Registration response (contains client_id, etc.).
    """
    resp = httpx.post(
        registration_endpoint,
        json={
            "client_name": "Archie",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        headers={"Content-Type": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def exchange_code(
    token_endpoint: str,
    client_id: str,
    code: str,
    verifier: str,
    redirect_uri: str,
    *,
    client_secret: str | None = None,
) -> dict[str, Any]:
    """Exchange authorization code for tokens.

    Args:
        token_endpoint: OAuth token endpoint.
        client_id: Registered client ID.
        code: Authorization code from callback.
        verifier: PKCE code verifier.
        redirect_uri: Must match the one used in the auth request.
        client_secret: Optional client secret.

    Returns:
        Token response dict (access_token, refresh_token, expires_in, etc.).
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret
    resp = httpx.post(
        token_endpoint,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def refresh_token(
    token_endpoint: str,
    client_id: str,
    refresh: str,
    *,
    client_secret: str | None = None,
) -> dict[str, Any]:
    """Refresh access token using a refresh token.

    Args:
        token_endpoint: OAuth token endpoint.
        client_id: Registered client ID.
        refresh: The refresh token.
        client_secret: Optional client secret.

    Returns:
        Token response dict.
    """
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret
    resp = httpx.post(
        token_endpoint,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def build_auth_url(
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    challenge: str,
    *,
    scopes: list[str] | None = None,
    extra_params: dict[str, str] | None = None,
) -> str:
    """Build OAuth authorization URL with PKCE.

    Args:
        authorization_endpoint: OAuth authorization endpoint.
        client_id: Registered client ID.
        redirect_uri: Callback URI.
        state: CSRF state parameter.
        challenge: PKCE code challenge.
        scopes: OAuth scopes to request.
        extra_params: Additional query parameters.

    Returns:
        Full authorization URL string.
    """
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if scopes:
        params["scope"] = " ".join(scopes)
    if extra_params:
        params.update(extra_params)
    return f"{authorization_endpoint}?{urlencode(params)}"


def open_browser(url: str) -> bool:
    """Open URL in the default browser. Returns False on failure."""
    try:
        return webbrowser.open(url)
    except Exception:
        return False


class _CallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler for the OAuth callback server."""

    auth_code: str | None = None
    auth_state: str | None = None
    auth_error: str | None = None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)
        _CallbackHandler.auth_code = params.get("code", [None])[0]
        _CallbackHandler.auth_state = params.get("state", [None])[0]
        _CallbackHandler.auth_error = params.get("error", [None])[0]

        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()

        if _CallbackHandler.auth_error:
            self.wfile.write(b"<h1>Authentication failed</h1><p>You can close this window.</p>")
        else:
            self.wfile.write(b"<h1>Authentication successful</h1><p>You can close this window.</p>")

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress HTTP server log output."""


def wait_for_callback() -> tuple[str | None, str | None, str | None]:
    """Run localhost callback server and wait for the OAuth redirect.

    Listens on localhost:CALLBACK_PORT for a single request, timing out
    after CALLBACK_TIMEOUT seconds.

    Returns:
        Tuple of (code, state, error). Code is None on timeout.
    """
    _CallbackHandler.auth_code = None
    _CallbackHandler.auth_state = None
    _CallbackHandler.auth_error = None

    server = HTTPServer(("localhost", CALLBACK_PORT), _CallbackHandler)
    server.timeout = CALLBACK_TIMEOUT
    server.handle_request()

    return _CallbackHandler.auth_code, _CallbackHandler.auth_state, _CallbackHandler.auth_error

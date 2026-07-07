"""Tests for OAuth primitives (oauth.py) — pure functions only."""

from base64 import urlsafe_b64encode
from hashlib import sha256
from urllib.parse import parse_qs, urlparse

from archie_shared.credentials.oauth import build_auth_url, generate_pkce
from archie_shared.credentials.refresh import extract_nested

# --- Tests: generate_pkce ---


def test_generate_pkce_format():
    """PKCE verifier and challenge have correct format."""
    verifier, challenge = generate_pkce()
    # Verifier is URL-safe base64
    assert len(verifier) >= 32
    assert all(
        c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in verifier
    )
    # Challenge is base64url-encoded SHA256 of verifier (no padding)
    assert "=" not in challenge


def test_generate_pkce_challenge_matches():
    """Challenge is S256 of verifier."""
    verifier, challenge = generate_pkce()
    expected = urlsafe_b64encode(sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected


def test_generate_pkce_unique():
    """Each call produces unique values."""
    v1, _ = generate_pkce()
    v2, _ = generate_pkce()
    assert v1 != v2


# --- Tests: build_auth_url ---


def test_build_auth_url_basic():
    """Build basic auth URL with required params."""
    url = build_auth_url(
        "https://auth.example.com/authorize",
        "client-123",
        "http://localhost:8585/callback",
        "state-abc",
        "challenge-xyz",
    )
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.example.com"
    assert params["response_type"] == ["code"]
    assert params["client_id"] == ["client-123"]
    assert params["state"] == ["state-abc"]
    assert params["code_challenge"] == ["challenge-xyz"]
    assert params["code_challenge_method"] == ["S256"]


def test_build_auth_url_with_scopes():
    """Scopes are joined with spaces."""
    url = build_auth_url(
        "https://auth.example.com/authorize",
        "client",
        "http://localhost/callback",
        "state",
        "challenge",
        scopes=["read", "write", "admin"],
    )
    params = parse_qs(urlparse(url).query)
    assert params["scope"] == ["read write admin"]


def test_build_auth_url_with_extra_params():
    """Extra params are included in URL."""
    url = build_auth_url(
        "https://auth.example.com/authorize",
        "client",
        "http://localhost/callback",
        "state",
        "challenge",
        extra_params={"access_type": "offline", "prompt": "consent"},
    )
    params = parse_qs(urlparse(url).query)
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]


# --- Tests: _extract ---


def test_extract_simple():
    """Extract top-level key."""
    assert extract_nested({"access_token": "abc"}, "access_token") == "abc"


def test_extract_nested():
    """Extract nested key via dot path."""
    data = {"authed_user": {"access_token": "nested-token"}}
    assert extract_nested(data, "authed_user.access_token") == "nested-token"


def test_extract_missing():
    """Missing key returns None."""
    assert extract_nested({"other": "val"}, "access_token") is None


def test_extract_deeply_nested():
    """Deep nesting works."""
    data = {"a": {"b": {"c": "deep"}}}
    assert extract_nested(data, "a.b.c") == "deep"


def test_extract_non_dict_intermediate():
    """Non-dict intermediate → None."""
    data = {"a": "string"}
    assert extract_nested(data, "a.b") is None

"""Tests for the orchestrator web console (GET / and static files)."""

from unittest.mock import patch

import pytest
from archie_orchestrator.app import app
from archie_shared.schemas import NexusConfig
from archie_shared.session import SessionDescriptor
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _app_state():
    app.state.config = NexusConfig()
    yield


def _make_session(
    session_id: str = "myproject-01abc12345",
    container_name: str = "archie-myproject-01abc12345",
    port: int = 32771,
    status: str = "Up 5 minutes",
) -> SessionDescriptor:
    return SessionDescriptor(
        session_id=session_id,
        container_name=container_name,
        port=port,
        raw_docker_status=status,
    )


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_css_served():
    """GET /static/style.css returns 200 with CSS content."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/static/style.css")

    assert resp.status_code == 200
    assert "text/css" in resp.headers.get("content-type", "")
    body = resp.text
    assert "--color-primary" in body
    assert "--color-bg" in body
    assert "--font-family" in body


@pytest.mark.asyncio
async def test_static_css_contains_design_tokens():
    """style.css defines all required design token variables."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/static/style.css")

    css = resp.text
    required_tokens = [
        "--color-primary",
        "--color-secondary",
        "--color-muted",
        "--color-text",
        "--color-bg",
        "--color-border",
        "--space-xs",
        "--space-sm",
        "--space-md",
        "--font-family",
        "--font-mono",
        "--radius",
        "--max-width",
        "--font-size-lg",
    ]
    for token in required_tokens:
        assert token in css, f"Missing CSS token: {token}"


# ---------------------------------------------------------------------------
# Sessions page — with sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_root_returns_html():
    """GET / returns 200 with text/html content type."""
    with patch("archie_orchestrator.web.list_sessions", return_value=[]):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/")

    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_sessions_page_shows_session_ids():
    """GET / with sessions → HTML contains session IDs and workspace names."""
    sessions = [
        _make_session("myproject-01abc12345"),
        _make_session("otherapp-02def67890", "archie-otherapp-02def67890", port=32772),
    ]

    with patch("archie_orchestrator.web.list_sessions", return_value=sessions):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/")

    html = resp.text
    assert "myproject-01abc12345" in html
    assert "otherapp-02def67890" in html
    # Workspace names extracted from session IDs
    assert "myproject" in html
    assert "otherapp" in html
    # Ports
    assert "32771" in html
    assert "32772" in html


@pytest.mark.asyncio
async def test_sessions_page_shows_status():
    """GET / includes docker status in session rows."""
    sessions = [_make_session(status="Up 10 minutes")]

    with patch("archie_orchestrator.web.list_sessions", return_value=sessions):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/")

    assert "Up 10 minutes" in resp.text


# ---------------------------------------------------------------------------
# Sessions page — empty state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_page_no_sessions_shows_empty_message():
    """GET / with no sessions → 'No running sessions' message."""
    with patch("archie_orchestrator.web.list_sessions", return_value=[]):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/")

    assert resp.status_code == 200
    assert "No running sessions" in resp.text


# ---------------------------------------------------------------------------
# Auto-refresh meta tag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_contains_auto_refresh_meta():
    """GET / HTML includes <meta http-equiv='refresh'> for auto-refresh."""
    with patch("archie_orchestrator.web.list_sessions", return_value=[]):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/")

    html = resp.text
    assert 'http-equiv="refresh"' in html
    assert 'content="5"' in html


# ---------------------------------------------------------------------------
# Uptime and version
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_contains_uptime():
    """GET / HTML includes the uptime string in the meta element."""
    with patch("archie_orchestrator.web.list_sessions", return_value=[]):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/")

    html = resp.text
    # Uptime appears inside the <p class="meta"> element as e.g. "0m" or "2h 15m"
    import re

    meta_match = re.search(r'class="meta"[^>]*>Up\s+(\d+[hm])', html)
    assert meta_match is not None, f"No uptime pattern found in meta element. HTML: {html[:500]}"


@pytest.mark.asyncio
async def test_page_contains_version():
    """GET / HTML includes the package version string."""
    with (
        patch("archie_orchestrator.web.list_sessions", return_value=[]),
        patch("archie_orchestrator.web._get_version", return_value="1.2.3"),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/")

    assert "1.2.3" in resp.text


@pytest.mark.asyncio
async def test_get_version_uses_shared_product_version():
    """_get_version() returns the shared Archie product version."""
    from archie_orchestrator.web import _get_version
    from archie_shared import __version__

    assert _get_version() == __version__


# ---------------------------------------------------------------------------
# Session with no port
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_with_no_port_shows_dash():
    """Session with port=None renders '—' in the port column."""
    sessions = [
        SessionDescriptor(
            session_id="myproject-01abc12345",
            container_name="archie-myproject-01abc12345",
            port=None,
            raw_docker_status="Starting",
        )
    ]

    with patch("archie_orchestrator.web.list_sessions", return_value=sessions):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/")

    # Jinja2 renders "—" (the em-dash fallback) for None ports
    assert "—" in resp.text


# ---------------------------------------------------------------------------
# Template inheritance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_includes_base_template_structure():
    """GET / HTML includes base template elements (header, title)."""
    with patch("archie_orchestrator.web.list_sessions", return_value=[]):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/")

    html = resp.text
    assert "<header>" in html
    assert "Archie Orchestrator" in html
    assert "Sessions" in html
    assert '/static/style.css"' in html

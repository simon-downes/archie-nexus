"""Tests for the orchestrator web console (GET / and static files)."""

import re
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from archie_orchestrator.app import app
from archie_shared.schemas import NexusConfig
from archie_shared.session import SessionDescriptor
from httpx import ASGITransport, AsyncClient

from tests.session_fakes import record, serve

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _app_state():
    app.state.config = NexusConfig()
    app.state.start_time = datetime.now(UTC)
    yield


def _make_session(
    session_id: str = "myproject-01abc12345", status: str = "idle"
) -> SessionDescriptor:
    return record(session_id, status=status)


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_css_served():
    """GET /static/style.css returns 200 with CSS content."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
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
    with serve(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/")

    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert 'href="/sessions"' in resp.text and 'href="/insights"' in resp.text
    assert "/ui/" not in resp.text


@pytest.mark.asyncio
async def test_sessions_page_shows_session_ids():
    """GET / with sessions → HTML contains session IDs and workspace names."""
    sessions = [
        _make_session("myproject-01abc12345"),
        _make_session("otherapp-02def67890"),
    ]

    with serve(app, records=sessions):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/")

    html = resp.text
    assert "myproject-01abc12345" in html
    assert "otherapp-02def67890" in html
    # Workspace names from the records
    assert "<td>myproject</td>" in html
    assert "<td>otherapp</td>" in html


@pytest.mark.asyncio
async def test_sessions_page_shows_status():
    """GET / shows the record's status in session rows."""
    sessions = [_make_session(status="blocked")]

    with serve(app, records=sessions):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/")

    assert "<td>blocked</td>" in resp.text


@pytest.mark.asyncio
async def test_sessions_page_columns_name_and_last_activity():
    """GET / shows the name only when user-set, and the last activity in UTC (082)."""
    sessions = [
        record(
            "named-01abc12345",
            name="Review PR",
            name_fixed=True,
            last_activity_at="2026-07-01T12:34:56+02:00",
        ),
        record("plain-01abc12345"),
    ]

    with serve(app, records=sessions):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/")

    html = resp.text
    headers = re.findall(r"<th>([^<]*)</th>", html)
    assert headers == ["Session ID", "Name", "Workspace", "Status", "Last activity"]
    rows = {
        cells[0]: cells[1:]
        for cells in (
            re.findall(r"<td>([^<]*)</td>", row)
            for row in re.findall(r"<tr>(.*?)</tr>", html, re.S)
        )
        if cells
    }
    assert rows["named-01abc12345"] == ["Review PR", "named", "idle", "2026-07-01 10:34:56 UTC"]
    # An unfixed name is the session ID; it isn't repeated, and no activity shows a dash.
    assert rows["plain-01abc12345"] == ["", "plain", "idle", "—"]


# ---------------------------------------------------------------------------
# Sessions page — empty state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_page_no_sessions_shows_empty_message():
    """GET / with no sessions → 'No running sessions' message."""
    with serve(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/")

    assert resp.status_code == 200
    assert "No running sessions" in resp.text


# ---------------------------------------------------------------------------
# Auto-refresh meta tag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_contains_auto_refresh_meta():
    """GET / HTML includes <meta http-equiv='refresh'> for auto-refresh."""
    with serve(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
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
    with serve(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/")

    html = resp.text
    # Uptime appears inside the <p class="meta"> element as e.g. "0m" or "2h 15m"
    meta_match = re.search(r'class="meta"[^>]*>Up\s+(\d+[hm])', html)
    assert meta_match is not None, f"No uptime pattern found in meta element. HTML: {html[:500]}"


@pytest.mark.asyncio
async def test_page_contains_version():
    """GET / HTML includes the package version string."""
    with (
        serve(app),
        patch("archie_orchestrator.web._get_version", return_value="1.2.3"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/")

    assert "1.2.3" in resp.text


@pytest.mark.asyncio
async def test_get_version_uses_shared_product_version():
    """_get_version() returns the shared Archie product version."""
    from archie_orchestrator.web import _get_version
    from archie_shared import __version__

    assert _get_version() == __version__


# ---------------------------------------------------------------------------
# Starting and ended sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_starting_session_shows_status_and_no_port_column():
    """A session still starting is listed with its status; the page has no Port column."""
    sessions = [_make_session(status="starting")]

    with serve(app, records=sessions):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/")

    assert "<td>starting</td>" in resp.text
    assert "<th>Port</th>" not in resp.text


@pytest.mark.asyncio
async def test_ended_sessions_are_not_listed():
    sessions = [
        _make_session("live-01abc12345"),
        record("gone-01abc12345", status="exited", ended_at="2026-07-01T00:00:00+00:00"),
    ]

    with serve(app, records=sessions):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/")

    assert "live-01abc12345" in resp.text
    assert "gone-01abc12345" not in resp.text


# ---------------------------------------------------------------------------
# Template inheritance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_includes_base_template_structure():
    """GET / HTML includes base template elements (header, title)."""
    with serve(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/")

    html = resp.text
    assert "<header>" in html
    assert "Archie Orchestrator" in html
    assert "Sessions" in html
    assert '/static/style.css"' in html

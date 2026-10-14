"""Tests for GET /ui/sessions (session log list)."""

import re
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from archie_orchestrator.app import app
from archie_orchestrator.web import VIEWER_CSP
from archie_shared.schemas import NexusConfig
from archie_shared.session import SessionDescriptor
from httpx import ASGITransport, AsyncClient


@pytest.fixture(autouse=True)
def _app_state():
    app.state.config = NexusConfig()
    app.state.start_time = datetime.now(UTC)
    app.state.session_index = None
    yield


def _running(session_id: str) -> SessionDescriptor:
    return SessionDescriptor(
        session_id=session_id,
        container_name=f"archie-{session_id}",
        port=32771,
        raw_docker_status="Up",
    )


async def _get(path: str, *, running=(), headers=None, side_effect=None):
    kwargs = {"side_effect": side_effect} if side_effect else {"return_value": list(running)}
    with patch("archie_orchestrator.web.list_sessions", **kwargs):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(path, headers=headers or {})


def _row_ids(html: str) -> list[str]:
    return re.findall(r'<tr[^>]*data-session-id="([^"]+)"', html)


@pytest.mark.asyncio
async def test_lists_logs_newest_first(session_logs):
    w = session_logs
    for sid in ("a-01kx5yc7g0", "c-01m4qq54e0", "b-01kx77jkz8"):
        w.write(sid, [w.user(f"hi from {sid}")])

    resp = await _get("/ui/sessions")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert _row_ids(resp.text) == ["c-01m4qq54e0", "b-01kx77jkz8", "a-01kx5yc7g0"]


@pytest.mark.asyncio
async def test_row_shows_link_workspace_models_and_usage(session_logs):
    w = session_logs
    events = [w.user("hello")]
    for turn in (1, 2, 3):
        events.append(w.llm(model_key="model-one", turn=turn, input_tokens=1000, cost_usd=0.5))
    events.append(
        w.llm(
            model_key="model-two",
            turn=4,
            input_tokens=234,
            output_tokens=56,
            cache_read_tokens=7890,
            cache_write_tokens=12,
            cost_usd=0.25,
        )
    )
    w.write("my-proj-01kx77jkz8", events)

    resp = await _get("/ui/sessions")
    html = resp.text

    assert 'href="/ui/sessions/my-proj-01kx77jkz8"' in html
    assert ">my-proj<" in html
    assert "2026-07-11 00:00" in html
    one, two = html.index("model-one"), html.index("model-two")
    assert one < two
    assert re.search(r"model-one\s*<span[^>]*>×3</span>", html)
    assert re.search(r"model-two\s*<span[^>]*>×1</span>", html)
    assert "3,234" in html  # input
    assert "7,890" in html  # cache read
    assert ">12<" in html  # cache write
    assert ">71<" in html  # output: 3 x 5 (default) + 56
    assert "$1.7500" in html


@pytest.mark.asyncio
async def test_active_marker(session_logs):
    w = session_logs
    for sid in ("a-01kx5yc7g0", "b-01kx77jkz8"):
        w.write(sid, [w.user("x")])

    resp = await _get("/ui/sessions", running=[_running("b-01kx77jkz8")])

    html = resp.text
    row_b = re.search(r'<tr[^>]*data-session-id="b-01kx77jkz8".*?</tr>', html, re.S).group(0)
    row_a = re.search(r'<tr[^>]*data-session-id="a-01kx5yc7g0".*?</tr>', html, re.S).group(0)
    assert 'class="active"' in row_b
    assert 'class="active"' not in row_a


@pytest.mark.asyncio
async def test_docker_missing_fails_open(session_logs):
    w = session_logs
    w.write("a-01kx5yc7g0", [w.user("x")])

    resp = await _get("/ui/sessions", side_effect=FileNotFoundError("docker"))

    assert resp.status_code == 200
    assert "Active status unavailable" in resp.text
    assert _row_ids(resp.text) == ["a-01kx5yc7g0"]
    assert 'class="active"' not in resp.text


@pytest.mark.asyncio
async def test_row_cap(session_logs):
    w = session_logs
    for n in range(201):
        (w.dir / f"ws-01kx5yc{n:03d}.jsonl").write_text("")

    resp = await _get("/ui/sessions")

    assert len(_row_ids(resp.text)) == 200
    assert "Showing 200 of 201" in resp.text


@pytest.mark.asyncio
async def test_unreadable_and_partial_logs_still_render(session_logs):
    w = session_logs
    path = w.write("good-01kx5yc7g0", [w.llm(input_tokens=4321)])
    with path.open("a") as f:
        f.write("garbage\n")
    (w.dir / "bad-01kx77jkz8.jsonl").write_text(
        '{"type":"user_message","id":"x","turn":1,"scope":null,"content":"a"}\n'
        '{"type":"user_message","id":"x","turn":1,"scope":null,"content":"b"}\n'
    )

    resp = await _get("/ui/sessions")

    assert resp.status_code == 200
    assert "4,321" in resp.text
    row_bad = re.search(r'<tr[^>]*data-session-id="bad-01kx77jkz8".*?</tr>', resp.text, re.S)
    assert row_bad and "unreadable" in row_bad.group(0)


@pytest.mark.asyncio
async def test_empty_directory(session_logs):
    resp = await _get("/ui/sessions")
    assert resp.status_code == 200
    assert "No session logs" in resp.text


@pytest.mark.asyncio
async def test_csp_header_and_assets(session_logs):
    resp = await _get("/ui/sessions")
    assert resp.headers["content-security-policy"] == VIEWER_CSP
    assert VIEWER_CSP == (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    assert "/static/vendor/nimble.min.css" in resp.text
    assert "/static/vendor/htmx.min.js" in resp.text


@pytest.mark.asyncio
async def test_vendored_assets_served():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        css = await client.get("/static/vendor/nimble.min.css")
        js = await client.get("/static/vendor/htmx.min.js")
    assert css.status_code == 200 and "nimble" in css.text.lower()
    assert js.status_code == 200 and "htmx" in js.text


@pytest.mark.asyncio
async def test_console_links_to_session_logs():
    resp = await _get("/")
    assert 'href="/ui/sessions"' in resp.text


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _search_fixture(w):
    w.write("archie-01kx5yc7g0", [w.user("Fix the bug in the parser")])  # 2026-07-10
    w.write(
        "archie-01kx77jkz8",  # 2026-07-11 00:00:01
        [
            w.user("please fix a bug"),
            w.tool_call("shell", {"command": "echo zebra"}, tool_use_id="t1"),
            w.tool_result("zebra", tool_use_id="t1"),
        ],
    )
    w.write("myarch-01m4mzkm00", [w.assistant("Fix the bug done")])  # 2026-10-11


@pytest.mark.asyncio
async def test_search_phrase(session_logs):
    _search_fixture(session_logs)
    resp = await _get('/ui/sessions?q="fix the bug"')
    assert _row_ids(resp.text) == ["myarch-01m4mzkm00", "archie-01kx5yc7g0"]


@pytest.mark.asyncio
async def test_search_ignores_tool_output(session_logs):
    _search_fixture(session_logs)
    resp = await _get("/ui/sessions?q=zebra")
    assert _row_ids(resp.text) == []
    assert "No sessions match" in resp.text


@pytest.mark.asyncio
async def test_search_matches_session_id(session_logs):
    _search_fixture(session_logs)
    resp = await _get("/ui/sessions?q=01kx77")
    assert _row_ids(resp.text) == ["archie-01kx77jkz8"]


@pytest.mark.asyncio
async def test_search_dates_and_workspace(session_logs):
    _search_fixture(session_logs)
    after = await _get("/ui/sessions?q=after:2026-07-11")
    before = await _get("/ui/sessions?q=before:2026-07-11")
    ws = await _get("/ui/sessions?q=workspace:arch")
    assert _row_ids(after.text) == ["myarch-01m4mzkm00", "archie-01kx77jkz8"]
    assert _row_ids(before.text) == ["archie-01kx5yc7g0"]
    assert _row_ids(ws.text) == ["archie-01kx77jkz8", "archie-01kx5yc7g0"]


@pytest.mark.asyncio
async def test_search_invalid_date_notice(session_logs):
    _search_fixture(session_logs)
    resp = await _get("/ui/sessions?q=before:2026-13-01")
    assert len(_row_ids(resp.text)) == 3
    assert "before:2026-13-01" in resp.text
    assert 'value="before:2026-13-01"' in resp.text


@pytest.mark.asyncio
async def test_search_count_reflects_matches(session_logs):
    _search_fixture(session_logs)
    resp = await _get("/ui/sessions?q=workspace:archie")
    assert "2 sessions" in resp.text


@pytest.mark.asyncio
async def test_htmx_request_returns_results_fragment(session_logs):
    _search_fixture(session_logs)
    resp = await _get("/ui/sessions?q=before:2026-13-01", headers={"HX-Request": "true"})
    body = resp.text.strip()
    assert body.startswith('<section id="results"')
    assert "<html" not in body
    assert "before:2026-13-01" in body  # notice inside the swapped fragment
    assert "3 sessions" in body
    assert resp.headers["vary"] == "HX-Request, HX-History-Restore-Request"
    assert resp.headers["content-security-policy"] == VIEWER_CSP


@pytest.mark.asyncio
async def test_history_restore_gets_full_page(session_logs):
    resp = await _get(
        "/ui/sessions?q=x",
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
    )
    assert "<html" in resp.text
    assert resp.headers["vary"] == "HX-Request, HX-History-Restore-Request"


@pytest.mark.asyncio
async def test_full_page_vary_and_search_attributes(session_logs):
    resp = await _get("/ui/sessions")
    assert resp.headers["vary"] == "HX-Request, HX-History-Restore-Request"
    form_input = re.search(r'<input type="search"[^>]*>', resp.text, re.S).group(0)
    assert 'hx-get="/ui/sessions"' in form_input
    assert 'hx-target="#results"' in form_input
    assert 'hx-swap="outerHTML"' in form_input
    assert 'hx-push-url="true"' in form_input
    assert "delay:" in form_input


@pytest.mark.asyncio
async def test_non_utf8_log_does_not_break_the_list(session_logs):
    w = session_logs
    w.write("good-01kx5yc7g0", [w.user("fine")])
    (w.dir / "bin-01kx77jkz8.jsonl").write_bytes(b"\xff\xfe{bad\n")
    resp = await _get("/ui/sessions")
    assert resp.status_code == 200
    row = re.search(r'<tr[^>]*data-session-id="bin-01kx77jkz8".*?</tr>', resp.text, re.S)
    assert row and "unreadable" in row.group(0)

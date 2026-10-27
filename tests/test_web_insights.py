"""Tests for the /ui/insights pages."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from archie_orchestrator import insights
from archie_orchestrator.app import app
from archie_orchestrator.metrics import MetricsIndex, connect
from archie_orchestrator.web import VIEWER_CSP
from archie_shared.schemas import NexusConfig
from httpx import ASGITransport, AsyncClient

PAGES = [
    "/ui/insights",
    "/ui/insights/sessions",
    "/ui/insights/models",
    "/ui/insights/tools",
    "/ui/insights/subagents",
    "/ui/insights/errors",
]
TEMPLATES = Path(insights.__file__).parent / "templates" / "insights"
SID = "proj-01k0000000"


@pytest.fixture(autouse=True)
def _app_state():
    app.state.config = NexusConfig()
    app.state.start_time = datetime.now(UTC)
    yield


@pytest.fixture
def home(session_logs, tmp_path):
    w = session_logs
    w.write(
        SID,
        [
            w.session_started(sent_at="2026-07-01T09:00:00+00:00"),
            w.user("Fix <b>the</b> parser", turn=1),
            w.llm(
                event_id="r1", model_key="m1", cost_usd=1.25, sent_at="2026-07-01T10:00:00+00:00"
            ),
            w.tool_call("shell", {"command": "ls"}, tool_use_id="tu1", request_id="r1"),
            w.tool_result("[exit: 2]\nls: nope", tool_use_id="tu1", is_error=True, duration_ms=30),
            w.turn_complete(turn=1),
            w.user("again", turn=2),
            w.llm(
                event_id="r2",
                turn=2,
                model_key="m2",
                cost_usd=0.5,
                status="error",
                error="Throttled",
                sent_at="2026-07-02T10:00:00+00:00",
            ),
            w.turn_error("provider down", turn=2),
        ],
    )
    w.write("other-01k0000001", [w.llm(model_key="m1", cost_usd=0.1)])
    return tmp_path


async def _get(path: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [*PAGES, f"/ui/insights/sessions/{SID}"])
async def test_pages_render_with_csp_and_nav(home, path):
    resp = await _get(f"{path}?since=all")
    assert resp.status_code == 200, resp.text[:500]
    assert resp.headers["content-security-policy"] == VIEWER_CSP
    for other in PAGES:
        assert f'href="{other}' in resp.text


@pytest.mark.asyncio
async def test_page_syncs_index_first(home):
    assert not (home / "metrics.db").exists()
    resp = await _get("/ui/insights?since=all")
    assert "$1.85" in resp.text
    assert (home / "metrics.db").exists()


@pytest.mark.asyncio
async def test_filters_round_trip(home):
    resp = await _get("/ui/insights?since=all&workspace=proj&model=m1&scope=root&until=2026-08-01")
    html = resp.text
    assert re.search(r'<option value="all" selected>', html)
    assert re.search(r'<option value="proj" selected>proj</option>', html)
    assert re.search(r'<option value="m1" selected>m1</option>', html)
    assert re.search(r'<option value="root" selected>', html)
    assert 'name="until" value="2026-08-01"' in html
    assert "$1.25" in html  # only m1 in proj


@pytest.mark.asyncio
async def test_from_date_overrides_since(home):
    resp = await _get("/ui/insights?since=all&from=2026-07-02")
    assert "$0.50" in resp.text
    assert 'name="from" value="2026-07-02"' in resp.text


@pytest.mark.asyncio
async def test_invalid_filter_shows_notice_and_is_ignored(home):
    resp = await _get("/ui/insights?since=2026-13-01&scope=nope")
    assert resp.status_code == 200
    assert "Ignored invalid since: 2026-13-01." in resp.text
    assert "Ignored invalid scope: nope." in resp.text


@pytest.mark.asyncio
async def test_invalid_sort_shows_notice(home):
    resp = await _get("/ui/insights/sessions?since=all&sort=bogus")
    assert resp.status_code == 200
    assert "Ignored invalid sort: bogus." in resp.text


@pytest.mark.asyncio
async def test_sessions_rows_link_to_detail_and_transcript(home):
    resp = await _get("/ui/insights/sessions?since=all")
    row = re.search(rf'<tr data-session-id="{SID}">.*?</tr>', resp.text, re.S).group(0)
    assert f'href="/ui/insights/sessions/{SID}"' in row
    assert f'href="/ui/sessions/{SID}"' in row
    assert "Fix &lt;b&gt;the&lt;/b&gt; parser" in row  # escaped


@pytest.mark.asyncio
async def test_sessions_sort_links_keep_filters(home):
    resp = await _get("/ui/insights/sessions?since=all&workspace=proj")
    assert 'href="/ui/insights/sessions?since=all&amp;workspace=proj&amp;sort=cost"' in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", [".hidden", "nope-01k0000000", "bad%20id"])
async def test_unknown_or_malformed_session_is_404(home, sid):
    resp = await _get(f"/ui/insights/sessions/{sid}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_tools_and_errors_content(home):
    tools = await _get("/ui/insights/tools?since=all")
    assert "[exit: 2] ls: nope" in tools.text
    errors = await _get("/ui/insights/errors?since=all")
    assert "provider down" in errors.text
    assert "Throttled" in errors.text


@pytest.mark.asyncio
async def test_page_while_index_locked_shows_updating(home):
    index = MetricsIndex(home / "metrics.db", home / "sessions")
    with index._locked(wait=True):
        resp = await _get("/ui/insights?since=all")
    assert resp.status_code == 200
    assert "Index is updating; figures may be incomplete." in resp.text
    assert "No data yet." in resp.text


# ---------------------------------------------------------------------------
# Charts (R23)
# ---------------------------------------------------------------------------


def _chart(html: str, chart_id: str):
    match = re.search(
        rf'<script type="application/json" id="chart-{chart_id}">(.*?)</script>', html, re.S
    )
    assert match, f"no chart-{chart_id}"
    return json.loads(match.group(1))


@pytest.mark.asyncio
async def test_chart_overview_daily_cost_matches_query(home):
    resp = await _get("/ui/insights?since=all")
    payload = _chart(resp.text, "daily-cost")
    conn = connect(home / "metrics.db")
    try:
        expected = insights.overview(conn, insights.parse_filters(since="all"))["daily_cost"]
    finally:
        conn.close()
    assert payload == expected
    assert 'data-chart="daily-cost"' in resp.text


@pytest.mark.asyncio
async def test_chart_session_detail(home):
    resp = await _get(f"/ui/insights/sessions/{SID}")
    assert _chart(resp.text, "turn-cost") == {"labels": [1, 2], "values": [1.25, 0.5]}
    assert _chart(resp.text, "context")["values"] == [100, 100]


@pytest.mark.asyncio
async def test_chart_errors_and_models(home):
    errors = await _get("/ui/insights/errors?since=all")
    daily = _chart(errors.text, "daily-errors")
    assert daily["days"] == ["2026-07-01", "2026-07-02"]
    assert daily["request_errors"] == [0, 1]
    models = await _get("/ui/insights/models?since=all")
    assert _chart(models.text, "model-cost")["labels"][0] == "m1"


@pytest.mark.asyncio
async def test_chart_json_cannot_break_out_of_script(session_logs, tmp_path):
    w = session_logs
    w.write("x-01k0000000", [w.llm(model_key="</script><b>x", cost_usd=1.0)])
    resp = await _get("/ui/insights?since=all")
    payload = _chart(resp.text, "daily-cost")
    assert payload["series"][0]["model"] == "</script><b>x"
    assert "</script><b>" not in resp.text


def test_templates_have_no_inline_script_or_style():
    for path in TEMPLATES.glob("*.html"):
        text = path.read_text()
        assert "style=" not in text, path.name
        for tag in re.findall(r"<script[^>]*>", text):
            assert 'type="application/json"' in tag or "src=" in tag, (path.name, tag)


@pytest.mark.asyncio
async def test_sync_failure_shows_notice_and_renders(home, monkeypatch):
    MetricsIndex(home / "metrics.db", home / "sessions").sync()

    def boom(self, *args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(MetricsIndex, "sync", boom)
    resp = await _get("/ui/insights?since=all")
    assert resp.status_code == 200
    assert "Index sync failed: disk on fire" in resp.text
    assert "$1.85" in resp.text


@pytest.mark.asyncio
async def test_unindexed_session_while_updating_is_200_with_notice(home):
    index = MetricsIndex(home / "metrics.db", home / "sessions")
    with index._locked(wait=True):
        resp = await _get(f"/ui/insights/sessions/{SID}")
    assert resp.status_code == 200
    assert "Index is updating" in resp.text

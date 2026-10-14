"""Tests for GET /ui/sessions/{id} (transcript view) and transcript rendering."""

import re
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from archie_orchestrator.app import app
from archie_orchestrator.transcript import markup_to_html, pygments_css, render_markdown
from archie_orchestrator.web import VIEWER_CSP
from archie_shared.events import (
    CompactionComplete,
    ContextCompacted,
    SessionStarted,
    TurnComplete,
    TurnError,
    TurnInterrupted,
)
from archie_shared.schemas import NexusConfig
from archie_shared.session import SessionDescriptor
from httpx import ASGITransport, AsyncClient

SID = "proj-01kx77jkz8"
TEMPLATES = Path(__file__).parents[1] / "orchestrator/src/archie_orchestrator/templates/viewer"
STATIC = Path(__file__).parents[1] / "orchestrator/src/archie_orchestrator/static/viewer"


@pytest.fixture(autouse=True)
def _app_state():
    app.state.config = NexusConfig()
    app.state.start_time = datetime.now(UTC)
    app.state.session_index = None
    yield


async def _get(path: str, *, running=()):
    with patch("archie_orchestrator.web.list_sessions", return_value=list(running)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(path)


def _block(html: str, element_id: str) -> str:
    """Return the HTML from the element with ``element_id`` to the end of the document."""
    start = html.index(f'id="{element_id}"')
    return html[start:]


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def test_markup_to_html_maps_styles_and_escapes():
    html = str(
        markup_to_html(
            r"Shell [bold]ls \[a\] <b>[/] — [#ff6d67]\[exit: 1][/] [dim italic](2 lines)[/]"
        )
    )
    assert '<span class="b">ls [a] &lt;b&gt;</span>' in html
    assert '<span class="err">[exit: 1]</span>' in html
    assert '<span class="dim i">(2 lines)</span>' in html
    assert "<b>" not in html


def test_markup_to_html_default_and_unknown_styles():
    html = str(markup_to_html("[#676767 italic](all lines)[/] [blink]x[/] [#67c26d]+1[/]"))
    assert '<span class="default i">(all lines)</span>' in html
    assert "<span>x</span>" in html
    assert '<span class="ok">+1</span>' in html


def test_markup_to_html_unclosed_tags_are_closed():
    assert str(markup_to_html("[bold]open")) == '<span class="b">open</span>'


def test_render_markdown_escapes_html_and_bad_links():
    html = str(render_markdown("<script>alert(1)</script> [x](javascript:alert(1))"))
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    assert 'href="javascript:' not in html


def test_render_markdown_highlights_code():
    html = str(render_markdown("```python\ndef f():\n    return 1\n```"))
    assert html.startswith('<pre class="highlight"><code>')
    assert '<span class="k">def</span>' in html
    assert "<div" not in html


def test_render_markdown_unknown_language_falls_back():
    html = str(render_markdown("```nosuchlang\n<x>\n```"))
    assert '<pre class="highlight"><code>' in html
    assert "&lt;x&gt;" in html


def test_render_markdown_tables_use_classes_not_styles():
    html = str(render_markdown("| a | b |\n|--:|:-:|\n| 1 | 2 |"))
    assert 'class="align-right"' in html
    assert 'class="align-center"' in html
    assert "style=" not in html


def test_render_markdown_images_become_links():
    html = str(render_markdown("![diagram](https://e.test/a.png)"))
    assert "<img" not in html
    assert 'href="https://e.test/a.png"' in html
    assert "image: diagram" in html


def test_pygments_css_file_is_current():
    assert (STATIC / "pygments.css").read_text() == pygments_css()


def test_templates_have_no_inline_script_or_style():
    for path in TEMPLATES.glob("*.html"):
        text = path.read_text()
        assert "style=" not in text, path.name
        assert not re.search(r"<script(?![^>]*\bsrc=)", text), path.name


# ---------------------------------------------------------------------------
# Detail page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detail_header_and_turns(session_logs):
    w = session_logs
    w.write(
        SID,
        [
            SessionStarted(
                id="s0", schema_version=1, sent_at="2026-07-11T00:00:01Z", model_key="m1"
            ),
            w.user("first question", turn=1),
            w.llm(turn=1, input_tokens=1200, cost_usd=0.5),
            w.assistant("first answer", turn=1),
            TurnComplete(id="tc1", turn=1, scope=None, stop_reason="end_turn"),
            w.user("second question", turn=2),
            w.llm(turn=2, model_key="m2", input_tokens=30, cost_usd=0.25),
            w.assistant("second answer", turn=2),
        ],
    )

    resp = await _get(f"/ui/sessions/{SID}")
    html = resp.text

    assert resp.status_code == 200
    assert resp.headers["content-security-policy"] == VIEWER_CSP
    assert SID in html and ">proj<" in html
    assert "2026-07-11 00:00" in html
    assert "1,230" in html and "$0.7500" in html
    assert html.count('class="turn"') == 2
    turns = re.findall(r'<section class="turn".*?</section>\s*<!-- /turn -->', html, re.S)
    assert len(turns) == 2
    assert re.search(r'class="msg msg-user".*first question', turns[0], re.S)
    assert "first answer" in turns[0] and "second answer" not in turns[0]
    assert "$0.5000" in turns[0]  # per-turn footer cost
    assert "live.js" not in html
    assert 'id="live-state"' not in html


@pytest.mark.asyncio
async def test_detail_404s(session_logs):
    w = session_logs
    (w.dir / ".hidden.jsonl").write_text("")
    for path in ("/ui/sessions/nope-01k0000000", "/ui/sessions/.hidden", "/ui/sessions/bad%20id"):
        resp = await _get(path)
        assert resp.status_code == 404, path


@pytest.mark.asyncio
async def test_detail_markdown_is_distinct_and_safe(session_logs):
    w = session_logs
    w.write(
        SID,
        [
            w.user("<script>alert(1)</script>"),
            w.assistant("Here:\n\n```python\nprint('hi')\n```\n\n[x](javascript:alert(1))"),
        ],
    )
    html = (await _get(f"/ui/sessions/{SID}")).text
    assert '<div class="md">' in html
    assert 'class="highlight"' in html
    assert "<script>alert" not in html
    assert 'href="javascript:' not in html


@pytest.mark.asyncio
async def test_detail_tool_rows(session_logs):
    w = session_logs
    w.write(
        SID,
        [
            w.user("run it"),
            w.tool_call("shell", {"command": "ls /nope"}, tool_use_id="t-err"),
            w.tool_result(
                "[exit: 2]\nls: cannot access", tool_use_id="t-err", is_error=True, duration_ms=1500
            ),
            w.tool_call("read", {"path": "big.txt"}, tool_use_id="t-big"),
            w.tool_result("x" * 10_000, tool_use_id="t-big"),
            w.tool_call("glob", {"pattern": "*.py"}, tool_use_id="t-pending"),
        ],
    )
    html = (await _get(f"/ui/sessions/{SID}")).text

    err = re.search(r'<details[^>]*id="tool-t-err".*?</details>', html, re.S).group(0)
    assert 'class="tool error"' in err
    assert '<span class="b">ls /nope</span>' in err
    assert "1.5s" in err
    assert "ls: cannot access" in err

    big = re.search(r'<details[^>]*id="tool-t-big".*?</details>', html, re.S).group(0)
    assert "x" * 8000 in big and "x" * 8001 not in big
    assert "… 2000 more characters" in big

    pending = re.search(r'<details[^>]*id="tool-t-pending".*?</details>', html, re.S).group(0)
    assert 'class="tool pending"' in pending


@pytest.mark.asyncio
async def test_detail_orphan_tool_result(session_logs):
    w = session_logs
    w.write(SID, [w.user("x"), w.tool_result("orphan output", tool_use_id="t-x", is_error=True)])
    html = (await _get(f"/ui/sessions/{SID}")).text
    row = re.search(r'<details[^>]*id="tool-t-x".*?</details>', html, re.S).group(0)
    assert 'class="tool error"' in row and "orphan output" in row


@pytest.mark.asyncio
async def test_detail_notices(session_logs):
    w = session_logs
    w.write(
        SID,
        [
            w.user("x", turn=1),
            ContextCompacted(
                id="cc",
                turn=1,
                iteration=1,
                scope=None,
                request_id="r1",
                reason="threshold",
                summary="Summary of **work**",
                model_key="m1",
                tokens_before=9000,
                tokens_after=900,
                duration_ms=10,
                debug="",
            ),
            CompactionComplete(
                id="cmp",
                turn=1,
                iteration=1,
                scope=None,
                request_id="r2",
                reason="manual",
                status="failure",
                tokens_before=100,
                tokens_after=None,
                cost_saved_usd=0.0,
                duration_ms=5,
                error="boom",
                summary=None,
            ),
            TurnError(id="te", turn=1, scope=None, message="provider exploded"),
            w.user("y", turn=2),
            TurnInterrupted(id="ti", turn=2, scope=None),
        ],
    )
    html = (await _get(f"/ui/sessions/{SID}")).text
    assert html.count('class="notice-block compaction"') == 2
    assert "9,000" in html and "900" in html and "<strong>work</strong>" in html
    assert "boom" in html
    assert 'class="notice-block turn-error"' in html and "provider exploded" in html
    assert 'class="notice-block turn-interrupted"' in html


@pytest.mark.asyncio
async def test_detail_subagents_nest_under_task(session_logs):
    w = session_logs
    w.write(
        SID,
        [
            w.user("delegate"),
            w.tool_call(
                "task", {"tasks": [{"prompt": "a"}, {"prompt": "b"}]}, tool_use_id="tu-task"
            ),
            w.user("child zero prompt", scope="tu-task", index=0),
            w.assistant("child zero says hi", scope="tu-task", index=0),
            w.user("child one prompt", scope="tu-task", index=1),
            w.tool_call(
                "read", {"path": "c.txt"}, tool_use_id="tu-child-read", scope="tu-task", index=1
            ),
            w.tool_result("data", tool_use_id="tu-child-read", scope="tu-task", index=1),
            w.assistant("child one says hi", scope="tu-task", index=1),
            w.tool_result("both done", tool_use_id="tu-task"),
            w.assistant("parent wraps up"),
        ],
    )
    html = (await _get(f"/ui/sessions/{SID}")).text

    task = re.search(r'<details[^>]*id="tool-tu-task".*?<!-- /tool-tu-task -->', html, re.S).group(
        0
    )
    assert task.count('class="subagent"') == 2
    assert 'id="sub-tu-task-0"' in task and 'id="sub-tu-task-1"' in task
    assert "child zero says hi" in task and "child one says hi" in task
    assert 'id="tool-tu-child-read"' in task
    outside = html.replace(task, "")
    assert "child zero says hi" not in outside
    assert "parent wraps up" in outside


@pytest.mark.asyncio
async def test_detail_child_without_task_row(session_logs):
    w = session_logs
    w.write(SID, [w.user("x"), w.assistant("lonely child", scope="tu-missing", index=0)])
    html = (await _get(f"/ui/sessions/{SID}")).text
    assert 'id="sub-tu-missing-0"' in html and "lonely child" in html


@pytest.mark.asyncio
async def test_detail_events_before_first_user_message(session_logs):
    w = session_logs
    w.write(SID, [SessionStarted(id="s0", schema_version=1, sent_at="x", model_key="m1")])
    html = (await _get(f"/ui/sessions/{SID}")).text
    assert 'class="turn"' not in html
    assert "No messages" in html


@pytest.mark.asyncio
async def test_detail_unreadable_log(session_logs):
    (session_logs.dir / f"{SID}.jsonl").write_text(
        '{"type":"user_message","id":"x","turn":1,"scope":null,"content":"a"}\n'
        '{"type":"user_message","id":"x","turn":1,"scope":null,"content":"b"}\n'
    )
    resp = await _get(f"/ui/sessions/{SID}")
    assert resp.status_code == 200
    assert "conflicting duplicate event id" in resp.text


@pytest.mark.asyncio
async def test_detail_active_session_loads_live_client(session_logs):
    w = session_logs
    w.write(SID, [w.user("x"), w.assistant("y")])
    running = SessionDescriptor(
        session_id=SID, container_name=f"archie-{SID}", port=1, raw_docker_status="Up"
    )
    html = (await _get(f"/ui/sessions/{SID}", running=[running])).text
    assert '<span id="live-state">connecting</span>' in html
    assert '<script src="/static/viewer/live.js" defer></script>' in html
    assert f'data-live-url="/ui/sessions/{SID}/live"' in html
    assert re.search(r'data-last-event-id="am\d+"', html)


@pytest.mark.asyncio
async def test_detail_non_utf8_log(session_logs):
    (session_logs.dir / f"{SID}.jsonl").write_bytes(b"\xff\xfe{bad\n")
    resp = await _get(f"/ui/sessions/{SID}")
    assert resp.status_code == 200
    assert "could not be read" in resp.text


@pytest.mark.asyncio
async def test_exec_source_is_capped(session_logs):
    w = session_logs
    w.write(
        SID, [w.user("x"), w.tool_call("exec", {"source": "a = 1\n" * 3000}, tool_use_id="t-exec")]
    )
    html = (await _get(f"/ui/sessions/{SID}")).text
    row = re.search(r'<details[^>]*id="tool-t-exec".*?</details>', html, re.S).group(0)
    assert "more characters" in row
    assert row.count("a</span>") <= 8000 // 6 + 1


@pytest.mark.asyncio
async def test_detail_render_runs_off_the_event_loop(session_logs, monkeypatch):
    import threading

    from archie_orchestrator import transcript

    w = session_logs
    w.write(SID, [w.user("x"), w.assistant("hello")])
    main = threading.main_thread()
    threads = []
    real = transcript.render_markdown

    def spy(text):
        threads.append(threading.current_thread())
        return real(text)

    monkeypatch.setattr(transcript, "render_markdown", spy)
    resp = await _get(f"/ui/sessions/{SID}")
    assert resp.status_code == 200
    assert threads and all(t is not main for t in threads)


def test_csp_blocks_framing_and_base():
    assert "frame-ancestors 'none'" in VIEWER_CSP
    assert "base-uri 'none'" in VIEWER_CSP
    assert "form-action 'self'" in VIEWER_CSP

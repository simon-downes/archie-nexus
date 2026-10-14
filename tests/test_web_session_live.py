"""Tests for GET /ui/sessions/{id}/live (SSE relay of the agent WebSocket)."""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from archie_orchestrator import web
from archie_orchestrator.app import app
from archie_shared.events import encode_event
from archie_shared.schemas import NexusConfig
from archie_shared.session import SessionDescriptor
from httpx import ASGITransport, AsyncClient

SID = "proj-01kx77jkz8"
LIVE_JS = Path(__file__).parents[1] / "orchestrator/src/archie_orchestrator/static/viewer/live.js"


@pytest.fixture(autouse=True)
def _app_state():
    app.state.config = NexusConfig()
    app.state.start_time = datetime.now(UTC)
    yield


def _running(port: int = 32771) -> SessionDescriptor:
    return SessionDescriptor(
        session_id=SID, container_name=f"archie-{SID}", port=port, raw_docker_status="Up"
    )


class FakeAgent:
    """Fake agent /stream socket: yields frames (str) or awaits pauses (float seconds)."""

    def __init__(self, frames):
        self.frames = frames
        self.sent: list[str] = []
        self.urls: list[str] = []

    async def send(self, msg):  # pragma: no cover - must never be called
        self.sent.append(msg)

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for frame in self.frames:
            if isinstance(frame, float):
                await asyncio.sleep(frame)
            else:
                yield frame

    def connect(self):
        agent = self

        @asynccontextmanager
        async def _connect(url, **kwargs):
            agent.urls.append(url)
            yield agent

        return _connect


def _frames(*events) -> list:
    out = [
        json.dumps({"type": "handshake", "id": "h", "protocol_version": 2, "session_id": SID}),
        json.dumps({"type": "session_status", "id": "ss", "model_key": "m1", "git_branch": "main"}),
    ]
    for e in events:
        out.append(e if isinstance(e, (str, float)) else encode_event(e))
    return out


def _parse_sse(body: str) -> list[dict]:
    """Split an SSE body into messages: {event, id, data} or {comment}."""
    messages = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        msg: dict = {"data": []}
        for line in block.split("\n"):
            if line.startswith(":"):
                msg.setdefault("comment", line[1:].strip())
            elif line.startswith("event: "):
                msg["event"] = line[7:]
            elif line.startswith("id: "):
                msg["id"] = line[4:]
            elif line.startswith("data: "):
                msg["data"].append(line[6:])
            elif line == "data:":
                msg["data"].append("")
        msg["data"] = "\n".join(msg["data"])
        messages.append(msg)
    return messages


async def _live(path, *, agent=None, running=True, headers=None, list_side_effect=None):
    kwargs = (
        {"side_effect": list_side_effect}
        if list_side_effect
        else {"return_value": [_running()] if running else []}
    )
    patches = [patch("archie_orchestrator.proxy.list_sessions", **kwargs)]
    if agent is not None:
        patches.append(patch("archie_orchestrator.web.websockets.connect", agent.connect()))
    for p in patches:
        p.start()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await asyncio.wait_for(client.get(path, headers=headers or {}), timeout=5)
    finally:
        for p in patches:
            p.stop()
    return resp, _parse_sse(resp.text)


def _fragments(messages):
    return [m for m in messages if m.get("event") == "fragment"]


@pytest.mark.asyncio
async def test_relays_events_after_cursor_once(session_logs):
    w = session_logs
    e1, e2, e3 = w.user("hello"), w.assistant("first reply"), w.user("second prompt", turn=2)
    w.write(SID, [e1, e2, e3])
    e4 = w.assistant("second reply", turn=2)
    agent = FakeAgent(_frames(e3, e4))

    resp, messages = await _live(f"/ui/sessions/{SID}/live?after={e2.id}", agent=agent)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache"
    frags = _fragments(messages)
    ids = [m.get("id") for m in frags]
    assert ids.count(e3.id) == 1 and ids.count(e4.id) == 1
    text = "\n".join(m["data"] for m in frags)
    assert "second prompt" in text and "second reply" in text
    assert "hello" not in text and "first reply" not in text
    assert agent.urls == ["ws://127.0.0.1:32771/stream"]
    assert agent.sent == []  # read-only
    assert messages[-1]["event"] == "end"


@pytest.mark.asyncio
async def test_last_event_id_header_wins(session_logs):
    w = session_logs
    e1, e2, e3 = w.user("one"), w.assistant("two"), w.assistant("three", request_id="r2")
    w.write(SID, [e1, e2, e3])
    agent = FakeAgent(_frames())

    _, messages = await _live(
        f"/ui/sessions/{SID}/live?after={e1.id}", agent=agent, headers={"Last-Event-ID": e2.id}
    )
    text = "\n".join(m["data"] for m in _fragments(messages))
    assert "three" in text and "two" not in text


@pytest.mark.asyncio
async def test_no_cursor_sends_everything(session_logs):
    w = session_logs
    w.write(SID, [w.user("from the log")])
    _, messages = await _live(f"/ui/sessions/{SID}/live", agent=FakeAgent(_frames()))
    assert "from the log" in "\n".join(m["data"] for m in _fragments(messages))


@pytest.mark.asyncio
async def test_unknown_cursor_asks_for_reload(session_logs):
    w = session_logs
    w.write(SID, [w.user("x")])
    agent = FakeAgent(_frames())
    _, messages = await _live(f"/ui/sessions/{SID}/live?after=nope", agent=agent)
    assert [m.get("event") for m in messages] == ["reload"]
    assert agent.sent == []


@pytest.mark.asyncio
async def test_streaming_deltas_then_final_message(session_logs):
    w = session_logs
    user = w.user("hi")
    w.write(SID, [user])

    def delta(text, n):
        return json.dumps(
            {
                "type": "text_delta",
                "id": f"d{n}",
                "turn": 1,
                "iteration": 1,
                "scope": None,
                "request_id": "r1",
                "text": text,
            }
        )

    final = w.assistant("Hello **there**")
    agent = FakeAgent(_frames(delta("Hel", 1), delta("lo <b>", 2), final))

    _, messages = await _live(f"/ui/sessions/{SID}/live?after={user.id}", agent=agent)
    frags = [m["data"] for m in _fragments(messages)]

    stream_id = "stream-1-1-r1"
    create = next(f for f in frags if f'id="{stream_id}"' in f and "streaming" in f)
    assert f'hx-swap-oob="beforeend:#turn-{user.id}-items"' in create
    assert "Hel" in create
    append = next(f for f in frags if f"beforeend:#{stream_id}-text" in f)
    assert "lo &lt;b&gt;" in append
    replace = next(f for f in frags if f'hx-swap-oob="outerHTML:#{stream_id}"' in f)
    assert "<strong>there</strong>" in replace
    assert frags.index(create) < frags.index(append) < frags.index(replace)
    # live-only deltas carry no SSE id (they are not resumable)
    assert all(
        m.get("id") is None for m in _fragments(messages) if "beforeend:#stream" in m["data"]
    )


@pytest.mark.asyncio
async def test_final_message_without_local_stream_clears_stale_block(session_logs):
    w = session_logs
    user = w.user("hi")
    w.write(SID, [user])
    final = w.assistant("complete text")
    _, messages = await _live(
        f"/ui/sessions/{SID}/live?after={user.id}", agent=FakeAgent(_frames(final))
    )
    text = "\n".join(m["data"] for m in _fragments(messages))
    assert "complete text" in text
    assert '<template data-remove-id="stream-1-1-r1"></template>' in text


@pytest.mark.asyncio
async def test_usage_header_updates(session_logs):
    w = session_logs
    user = w.user("hi")
    w.write(SID, [user, w.llm(input_tokens=1000, cost_usd=0.5)])
    req = w.llm(input_tokens=234, cost_usd=0.25)
    _, messages = await _live(
        f"/ui/sessions/{SID}/live?after={user.id}", agent=FakeAgent(_frames(req))
    )
    usage = [m["data"] for m in _fragments(messages) if 'id="session-usage"' in m["data"]]
    assert usage, "no usage fragment"
    assert 'hx-swap-oob="outerHTML:#session-usage"' in usage[-1]
    assert "1,234" in usage[-1] and "$0.7500" in usage[-1]


@pytest.mark.asyncio
async def test_tool_result_replaces_row_and_children_nest(session_logs):
    w = session_logs
    user = w.user("go")
    call = w.tool_call("task", {"tasks": [{}]}, tool_use_id="tu-task")
    w.write(SID, [user, call])
    child = w.assistant("child says", scope="tu-task", index=0)
    result = w.tool_result("done", tool_use_id="tu-task")
    _, messages = await _live(
        f"/ui/sessions/{SID}/live?after={call.id}", agent=FakeAgent(_frames(child, result))
    )
    frags = [m["data"] for m in _fragments(messages)]
    sub = next(f for f in frags if 'id="sub-tu-task-0"' in f and 'class="subagent"' in f)
    assert 'hx-swap-oob="beforeend:#tool-tu-task-children"' in sub
    row = next(f for f in frags if 'hx-swap-oob="outerHTML:#tool-tu-task"' in f)
    assert 'class="tool ok"' in row
    assert "child says" in row  # replacing the row keeps its nested children


@pytest.mark.asyncio
async def test_top_level_turn_is_plain_fragment(session_logs):
    w = session_logs
    first = w.user("a")
    w.write(SID, [first])
    second = w.user("b", turn=2)
    _, messages = await _live(
        f"/ui/sessions/{SID}/live?after={first.id}", agent=FakeAgent(_frames(second))
    )
    frag = next(m["data"] for m in _fragments(messages) if f'id="turn-{second.id}"' in m["data"])
    assert frag.lstrip().startswith('<section class="turn"')
    assert "hx-swap-oob" not in frag.split(">", 1)[0]


@pytest.mark.asyncio
async def test_undecodable_frames_are_skipped(session_logs):
    w = session_logs
    user = w.user("x")
    w.write(SID, [user])
    later = w.assistant("still relayed")
    _, messages = await _live(
        f"/ui/sessions/{SID}/live?after={user.id}",
        agent=FakeAgent(_frames("not json", '{"type":"mystery"}', later)),
    )
    assert "still relayed" in "\n".join(m["data"] for m in _fragments(messages))


@pytest.mark.asyncio
async def test_socket_close_ends_stream(session_logs):
    w = session_logs
    w.write(SID, [w.user("x")])
    _, messages = await _live(f"/ui/sessions/{SID}/live", agent=FakeAgent(_frames()))
    assert messages[-1]["event"] == "end"
    assert "Session ended" in messages[-1]["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["not-running", "no-port", "docker-missing", "refused"],
)
async def test_not_running_cases(session_logs, case):
    w = session_logs
    w.write(SID, [w.user("x")])
    kwargs = {}
    if case == "not-running":
        kwargs["running"] = False
    elif case == "docker-missing":
        kwargs["list_side_effect"] = FileNotFoundError("docker")
    elif case == "no-port":
        kwargs["list_side_effect"] = lambda: [
            SessionDescriptor(session_id=SID, container_name="c", port=None, raw_docker_status="Up")
        ]

    @asynccontextmanager
    async def refusing(url, **kw):
        raise ConnectionRefusedError("refused")
        yield  # pragma: no cover

    with patch("archie_orchestrator.web.websockets.connect", refusing):
        resp, messages = await _live(f"/ui/sessions/{SID}/live", **kwargs)

    assert resp.status_code == 200
    assert messages[-1]["event"] == "end"
    assert "Session is not running" in messages[-1]["data"]


@pytest.mark.asyncio
async def test_unknown_session_404(session_logs):
    resp, _ = await _live("/ui/sessions/nope-01k0000000/live", running=False)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_idle_ping_keeps_relay_open(session_logs, monkeypatch):
    monkeypatch.setattr(web, "PING_INTERVAL", 0.01)
    w = session_logs
    user = w.user("x")
    w.write(SID, [user])
    later = w.assistant("after the pause")
    _, messages = await _live(
        f"/ui/sessions/{SID}/live?after={user.id}", agent=FakeAgent(_frames(0.08, later))
    )
    kinds = [m.get("comment") or m.get("event") for m in messages]
    assert "ping" in kinds
    assert "after the pause" in "\n".join(m["data"] for m in _fragments(messages))
    assert kinds.index("ping") < kinds.index("fragment")
    assert kinds[-1] == "end"


def test_live_js_uses_event_source_and_htmx_swap():
    js = LIVE_JS.read_text()
    assert "new EventSource" in js
    assert "htmx.swap" in js
    assert "data-remove-id" in js
    for name in ("fragment", "end", "reload"):
        assert f'"{name}"' in js
    for state in ("connecting", "live", "ended", "reconnecting"):
        assert state in js


def _ids_in(html: str) -> list[str]:
    return re.findall(r'\bid="([^"]+)"', html)


@pytest.mark.asyncio
async def test_first_child_event_is_sent_once(session_logs):
    w = session_logs
    user = w.user("go")
    call = w.tool_call("task", {"tasks": [{}]}, tool_use_id="tu-task")
    w.write(SID, [user, call])
    child = w.user("child prompt", scope="tu-task", index=0)
    _, messages = await _live(
        f"/ui/sessions/{SID}/live?after={call.id}", agent=FakeAgent(_frames(child))
    )
    text = "\n".join(m["data"] for m in _fragments(messages))
    assert text.count("child prompt") == 1
    assert _ids_in(text).count(f"msg-{child.id}") == 1


@pytest.mark.asyncio
async def test_each_event_is_one_sse_message(session_logs):
    """All fragments for one event share one SSE message, so a cursor covers all of them."""
    w = session_logs
    user = w.user("go")
    call = w.tool_call("task", {"tasks": [{}]}, tool_use_id="tu-task")
    w.write(SID, [user, call])
    child = w.assistant("child says", scope="tu-task", index=0)
    _, messages = await _live(
        f"/ui/sessions/{SID}/live?after={call.id}", agent=FakeAgent(_frames(child))
    )
    frags = _fragments(messages)
    carrying = [m for m in frags if "child says" in m["data"] or "sub-tu-task-0" in m["data"]]
    assert len(carrying) == 1
    assert carrying[0]["id"] == child.id


@pytest.mark.asyncio
async def test_carriage_returns_cannot_inject_sse_fields(session_logs):
    w = session_logs
    user = w.user("go")
    call = w.tool_call("shell", {"command": "x"}, tool_use_id="t1")
    w.write(SID, [user, call])
    result = w.tool_result("10%\rid: bogus\revent: reload\r\r\rdone\r\n", tool_use_id="t1")
    resp, _ = await _live(
        f"/ui/sessions/{SID}/live?after={call.id}", agent=FakeAgent(_frames(result))
    )
    # Parse as an EventSource does: CR, LF and CRLF all end a line.
    fields = [line for line in re.split(r"\r\n|\r|\n", resp.text) if line]
    assert "id: bogus" not in fields
    assert "event: reload" not in fields
    assert not any(f.startswith(("id: bogus", "event: reload")) for f in fields)
    data = "\n".join(f[6:] for f in fields if f.startswith("data: "))
    assert "done" in data and "</details>" in data


@pytest.mark.asyncio
async def test_resumed_stream_replaces_stale_block(session_logs):
    """A reconnect mid-stream must not create a second element with the same stream id."""
    w = session_logs
    user = w.user("hi")
    w.write(SID, [user])
    delta = json.dumps(
        {
            "type": "text_delta",
            "id": "d9",
            "turn": 1,
            "iteration": 1,
            "scope": None,
            "request_id": "r1",
            "text": "more",
        }
    )
    _, messages = await _live(
        f"/ui/sessions/{SID}/live?after={user.id}", agent=FakeAgent(_frames(delta))
    )
    frag = next(m["data"] for m in _fragments(messages) if "more" in m["data"])
    # The first delta a builder sees deletes any block left by an earlier
    # connection, then appends a fresh one with the same id.
    delete = '<template data-remove-id="stream-1-1-r1"></template>'
    assert delete in frag
    assert frag.index(delete) < frag.index('streaming" id="stream-1-1-r1"')


@pytest.mark.asyncio
async def test_unreadable_log_ends_stream(session_logs):
    (session_logs.dir / f"{SID}.jsonl").write_text(
        '{"type":"user_message","id":"x","turn":1,"scope":null,"content":"a"}\n'
        '{"type":"user_message","id":"x","turn":1,"scope":null,"content":"b"}\n'
    )
    _, messages = await _live(f"/ui/sessions/{SID}/live", agent=FakeAgent(_frames()))
    assert messages[-1]["event"] == "end"
    assert "could not be read" in messages[-1]["data"]


@pytest.mark.asyncio
async def test_first_child_assistant_message_is_sent_once(session_logs):
    w = session_logs
    user = w.user("go")
    call = w.tool_call("task", {"tasks": [{}]}, tool_use_id="tu-task")
    w.write(SID, [user, call])
    child = w.assistant("CHILDTEXT", scope="tu-task", index=0)
    _, messages = await _live(
        f"/ui/sessions/{SID}/live?after={call.id}", agent=FakeAgent(_frames(child))
    )
    text = "\n".join(m["data"] for m in _fragments(messages))
    assert text.count("CHILDTEXT") == 1
    assert _ids_in(text).count(f"msg-{child.id}") == 1


@pytest.mark.asyncio
async def test_resumed_child_stream_replaces_stale_group(session_logs):
    w = session_logs
    user = w.user("go")
    call = w.tool_call("task", {"tasks": [{}]}, tool_use_id="tu-task")
    w.write(SID, [user, call])
    delta = json.dumps(
        {
            "type": "text_delta",
            "id": "d1",
            "turn": 1,
            "iteration": 1,
            "scope": "tu-task",
            "subagent_index": 0,
            "request_id": "c1",
            "text": "child streaming",
        }
    )
    _, messages = await _live(
        f"/ui/sessions/{SID}/live?after={call.id}", agent=FakeAgent(_frames(delta))
    )
    frag = next(m["data"] for m in _fragments(messages) if "child streaming" in m["data"])
    marker = '<template data-remove-id="sub-tu-task-0"></template>'
    assert marker in frag
    assert frag.index(marker) < frag.index('class="subagent" id="sub-tu-task-0"')
    assert frag.count("child streaming") == 1

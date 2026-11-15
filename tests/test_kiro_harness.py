"""KiroHarness end to end against tests/fake_kiro.py (077)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

import msgspec
import pytest
import yaml
from archie_agent.kiro.harness import KiroHarness, KiroStartupError
from archie_agent.tool_policy_live import LiveToolPolicy
from archie_shared.commands import AnswerCommand
from archie_shared.events import (
    AssistantMessage,
    CompactionComplete,
    CompactionStart,
    ErrorNotice,
    LLMRequest,
    SessionStatus,
    ToolCall,
    ToolResult,
    TurnComplete,
    TurnError,
    TurnInterrupted,
    UserMessage,
    decode_event,
    encode_event,
)
from archie_shared.schemas import NexusConfig

FAKE = Path(__file__).parent / "fake_kiro.py"
SECRET = "kiro-test-secret-0123456789"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "nexus"
    (home / "sessions").mkdir(parents=True)
    (home / "credentials.yaml").write_text(yaml.safe_dump({"kiro": {"api_key": SECRET}}))
    (home / "credentials.yaml").chmod(0o600)
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(home))
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    monkeypatch.setenv("FAKE_KIRO_RECORD", str(tmp_path / "record.jsonl"))
    monkeypatch.setenv("FAKE_KIRO_HOME", str(tmp_path / "kiro"))
    return home


def _harness(home: Path, tmp_path: Path, **kwargs) -> KiroHarness:
    return KiroHarness(
        NexusConfig(),
        session_id="kirotest-01",
        log_dir=home / "sessions",
        tool_policy=LiveToolPolicy.static(None),
        kiro_home=tmp_path / "kiro",
        command=[sys.executable, str(FAKE)],
        workspace=str(tmp_path),
        **kwargs,
    )


def _record(tmp_path: Path) -> list[dict]:
    path = tmp_path / "record.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _log(home: Path) -> list:
    path = home / "sessions" / "kirotest-01.jsonl"
    return [decode_event(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
async def harness(home, tmp_path):
    h = _harness(home, tmp_path)
    await h.start()
    yield h
    await h.close()


async def test_startup_sets_config_and_passes_key_only_to_kiro(harness, home, tmp_path, caplog):
    record = _record(tmp_path)
    assert record[0]["env"]["KIRO_API_KEY"] == SECRET
    assert "KIRO_API_KEY" not in os.environ
    sets = [
        (r["in"]["params"]["configId"], r["in"]["params"]["value"])
        for r in record
        if r.get("in", {}).get("method") == "session/set_config_option"
    ]
    assert sets == [
        ("contentCollection", "disabled"),
        ("autopilot", "on"),
        ("model", "gpt-5.6-luna"),
    ]
    new = next(r["in"] for r in record if r.get("in", {}).get("method") == "session/new")
    assert new["params"]["_meta"] == {"kiro": {"modeId": "archie"}}
    assert new["params"]["mcpServers"] == []
    agent = json.loads((tmp_path / "kiro" / "agents" / "archie.json").read_text())
    assert agent["mcpServers"]["archie"]["url"] == harness.mcp_url
    settings = json.loads((tmp_path / "kiro" / "settings" / "cli.json").read_text())
    assert settings["telemetry.enabled"] is False
    frames = (home / "logs" / "kiro" / "kirotest-01.acp.jsonl").read_text()
    assert SECRET not in frames
    parsed = [json.loads(line) for line in frames.splitlines()]
    assert any(f["dir"] == "out" and f["msg"].get("method") == "initialize" for f in parsed)
    assert any(
        f["dir"] == "in" and (f["msg"].get("result") or {}).get("sessionId") == "sess_fake"
        for f in parsed
    )


async def test_stderr_filter(home, tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="archie_agent.kiro.acp")
    h = _harness(home, tmp_path)
    await h.start()
    deadline = time.monotonic() + 5
    while "fake kiro: started" not in caplog.text and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    await h.close()
    assert "fake kiro: started" in caplog.text
    assert "ExperimentalWarning" not in caplog.text
    assert "routine progress" not in caplog.text
    assert SECRET not in caplog.text


async def test_turn_with_archie_tool(harness, home):
    await harness.handle_message("run it")
    events = _log(home)
    assert isinstance(events[0], UserMessage)
    call = next(e for e in events if isinstance(e, ToolCall))
    assert (call.name, call.input) == (
        "shell",
        {"command": "echo KIRO-OK", "description": "Say OK"},
    )
    result = next(e for e in events if isinstance(e, ToolResult))
    assert "KIRO-OK" in result.content and not result.is_error
    request = next(e for e in events if isinstance(e, LLMRequest))
    assert request.credits == 0.5
    assert request.cost_usd == 0.01
    assert request.model_key == "kiro/gpt-5.6-luna"
    assert (
        request.context_pct,
        request.context_pct_max,
        request.context_samples,
        request.kiro_requests,
    ) == (3.1, 3.1, 2, 3)
    messages = [e.content for e in events if isinstance(e, AssistantMessage)]
    assert messages == ["Running the command.", "Done: KIRO-OK."]
    assert isinstance(events[-1], TurnComplete)
    assert not harness.turn_active


def _capture(harness: KiroHarness) -> list:
    seen: list = []
    original = harness.event_bus.emit

    async def capture(event, target=None):
        seen.append(event)
        return await original(event, target)

    harness.event_bus.emit = capture
    return seen


async def test_context_usage_reaches_session_status(harness):
    """Each new root context percentage sends a status; repeats don't (089 D7)."""
    assert harness.status_event().context_pct is msgspec.UNSET
    assert "context_pct" not in encode_event(harness.status_event())
    seen = _capture(harness)
    await harness.handle_message("run it")
    statuses = [e for e in seen if isinstance(e, SessionStatus)]
    assert [s.context_pct for s in statuses] == [2.5, 3.1, 3.1]
    assert [s.state for s in statuses] == ["working", "working", "idle"]
    assert harness.status_event().context_pct == 3.1


async def test_subagent_context_frame_is_ignored(harness):
    seen = _capture(harness)
    frame = {
        "method": "session/update",
        "params": {
            "update": {
                "sessionUpdate": "session_info_update",
                "_meta": {
                    "kiro": {
                        "kind": "context_usage",
                        "usagePercentage": 9.0,
                        "agentSubtaskId": "sub-1",
                    }
                },
            }
        },
    }
    await harness._on_notification(frame, time.time())
    assert not any(isinstance(e, SessionStatus) for e in seen)
    assert harness.status_event().context_pct is msgspec.UNSET


async def test_context_frame_while_turn_closes_waits_for_the_closing_status(harness):
    """After the translator is gone but before the closing status, no ``working`` status."""
    seen = _capture(harness)
    harness._turn_active = True  # finish() has run; handle_message's finally hasn't
    frame = {
        "method": "session/update",
        "params": {
            "update": {
                "sessionUpdate": "session_info_update",
                "_meta": {"kiro": {"kind": "context_usage", "usagePercentage": 48.9}},
            }
        },
    }
    await harness._on_notification(frame, time.time())
    assert not any(isinstance(e, SessionStatus) for e in seen)
    harness._turn_active = False
    assert harness.status_event().context_pct == 48.9  # carried by the closing status
    await harness._on_notification(frame, time.time())
    assert not any(isinstance(e, SessionStatus) for e in seen)  # a repeat sends nothing
    frame["params"]["update"]["_meta"]["kiro"]["usagePercentage"] = 48.7
    await harness._on_notification(frame, time.time())  # between turns: sent at once
    statuses = [e for e in seen if isinstance(e, SessionStatus)]
    assert [(s.state, s.context_pct) for s in statuses] == [("idle", 48.7)]


def test_native_session_status_omits_context_pct():
    status = SessionStatus(id="s", model_key="m", git_branch="main")
    assert "context_pct" not in encode_event(status)
    kiro = SessionStatus(id="k", model_key="kiro/m", git_branch="main", context_pct=4.5)
    assert decode_event(encode_event(kiro)).context_pct == 4.5


async def test_prompt_error_is_turn_error(harness, home):
    await harness.handle_message("error")
    events = _log(home)
    assert isinstance(events[-1], TurnError)
    assert "model exploded" in events[-1].message
    request = next(e for e in events if isinstance(e, LLMRequest))
    assert request.status == "no_usage"


async def test_interrupt_cancels_turn_and_kills_tool(harness, home):
    task = asyncio.create_task(harness.handle_message("sleep"))
    deadline = time.monotonic() + 10
    while not any(isinstance(e, ToolCall) for e in _safe_log(home)):
        assert time.monotonic() < deadline
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    started = time.monotonic()
    harness.interrupt()
    await asyncio.wait_for(task, 5)
    assert time.monotonic() - started < 3
    events = _log(home)
    assert isinstance(events[-1], TurnInterrupted)
    request = next(e for e in events if isinstance(e, LLMRequest))
    assert request.status == "interrupted" and request.cost_usd > 0


def _safe_log(home: Path) -> list:
    try:
        return _log(home)
    except FileNotFoundError:
        return []


async def test_permission_request_is_cancelled(harness, tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    await harness.handle_message("permission")
    responses = [r["permission_response"] for r in _record(tmp_path) if "permission_response" in r]
    assert responses == [{"outcome": {"outcome": "cancelled"}}]
    assert "Run builtin shell" in caplog.text


async def test_kiro_exit_mid_turn_then_dead_notice(harness, home):
    await harness.handle_message("exit")
    events = _log(home)
    assert isinstance(events[-1], TurnError)
    assert "Kiro backend stopped" in events[-1].message
    count = len(events)
    seen: list = []
    original = harness.event_bus.emit

    async def capture(event, target=None):
        seen.append(event)
        return await original(event, target)

    harness.event_bus.emit = capture
    assert harness.try_begin_turn()
    await harness.handle_message("again")
    assert len(_log(home)) == count
    notice = next(e for e in seen if isinstance(e, ErrorNotice))
    assert notice.kind == "turn_error" and "start a new session" in notice.message
    assert not harness.turn_active


async def _compact(harness: KiroHarness) -> list:
    """Run ``/compact`` as ``app.py`` does and return every emitted event."""
    seen = _capture(harness)
    assert harness.try_begin_turn()
    await harness.run_manual_compaction()
    assert not harness.turn_active
    return seen


def _compact_requests(tmp_path: Path) -> list[dict]:
    return [
        r["in"]["params"]
        for r in _record(tmp_path)
        if r.get("in", {}).get("method") == "_kiro/session/compact"
    ]


async def test_compact_calls_kiro_and_records_a_manual_compaction(harness, home, tmp_path):
    """091 D4: /compact runs _kiro/session/compact and logs a manual compaction."""
    await harness.handle_message("run it")
    seen = await _compact(harness)
    assert _compact_requests(tmp_path) == [{"sessionId": "sess_fake"}]
    start, complete = [e for e in _log(home) if isinstance(e, CompactionStart | CompactionComplete)]
    assert (start.reason, start.tokens_before, start.model_key, start.turn) == (
        "manual",
        0,
        "kiro/gpt-5.6-luna",
        1,
    )
    assert (complete.status, complete.request_id, complete.error) == (
        "success",
        start.request_id,
        None,
    )
    assert (complete.tokens_after, complete.cost_saved_usd) == (None, 0.0)
    assert complete.summary == "## Overview\nFake Kiro summary."
    states = [e.state for e in seen if isinstance(e, SessionStatus)]
    assert states[0] == "working" and states[-1] == "idle"


@pytest.mark.parametrize(
    ("variant", "error"),
    [
        ("busy", "Kiro did not compact: a turn or compaction is in progress"),
        ("error", "Kiro compaction failed: no summary"),
    ],
)
async def test_compact_failures_end_as_failed_compactions(
    home, tmp_path, monkeypatch, variant, error
):
    monkeypatch.setenv("FAKE_KIRO_COMPACT", variant)
    h = _harness(home, tmp_path)
    await h.start()
    try:
        await h.handle_message("run it")
        await _compact(h)
    finally:
        await h.close()
    (complete,) = [e for e in _log(home) if isinstance(e, CompactionComplete)]
    assert (complete.status, complete.error, complete.summary) == ("failure", error, None)


async def test_compact_before_any_turn_sends_nothing(harness, home, tmp_path):
    seen = await _compact(harness)
    assert [(e.kind, e.message) for e in seen if isinstance(e, ErrorNotice)] == [
        ("manual_compaction_failed", "Nothing to compact")
    ]
    assert _compact_requests(tmp_path) == []
    assert not any(isinstance(e, CompactionStart) for e in _safe_log(home))


async def test_compact_on_a_dead_backend_is_refused(harness, tmp_path):
    await harness.handle_message("exit")
    seen = await _compact(harness)
    notice = next(e for e in seen if isinstance(e, ErrorNotice))
    assert notice.kind == "manual_compaction_failed" and "start a new session" in notice.message
    assert _compact_requests(tmp_path) == []


async def test_interrupt_during_compaction_sends_no_cancel(home, tmp_path, monkeypatch):
    """091 D5: Kiro can't cancel a compaction, so interrupt only says so."""
    monkeypatch.setenv("FAKE_KIRO_COMPACT", "slow")
    h = _harness(home, tmp_path)
    await h.start()
    try:
        await h.handle_message("run it")
        seen = _capture(h)
        assert h.try_begin_turn()
        task = asyncio.create_task(h.run_manual_compaction())
        deadline = time.monotonic() + 5
        while not _compact_requests(tmp_path) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        h.interrupt()
        await task
    finally:
        await h.close()
    methods = [r.get("in", {}).get("method") for r in _record(tmp_path)]
    assert "session/cancel" not in methods
    assert [e.kind for e in seen if isinstance(e, ErrorNotice)] == ["compaction_not_cancellable"]
    (complete,) = [e for e in _log(home) if isinstance(e, CompactionComplete)]
    assert complete.status == "success"


async def test_automatic_compaction_is_logged_and_hides_the_placeholder(harness, home):
    """091 D7/D8: a mid-turn Kiro compaction is a threshold compaction; 20 % never shows."""
    seen = _capture(harness)
    await harness.handle_message("autocompact")
    events = _log(home)
    (start,) = [e for e in events if isinstance(e, CompactionStart)]
    (complete,) = [e for e in events if isinstance(e, CompactionComplete)]
    assert (start.reason, complete.status, complete.summary) == (
        "threshold",
        "success",
        "## Overview\nAuto summary.",
    )
    assert [e.content for e in events if isinstance(e, AssistantMessage)] == ["Answer.", "Ready."]
    pcts = [e.context_pct for e in seen if isinstance(e, SessionStatus)]
    assert 20.0 not in pcts and 4.2 in pcts
    assert isinstance(events[-1], TurnComplete)


async def test_failed_automatic_compaction_is_logged(harness, home):
    await harness.handle_message("autocompact_fail")
    (complete,) = [e for e in _log(home) if isinstance(e, CompactionComplete)]
    assert (complete.status, complete.error) == ("failure", "Kiro compaction failed: error")


async def _until(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def _recorded(tmp_path: Path, key: str):
    return next((r[key] for r in _record(tmp_path) if key in r), None)


async def test_question_tool_is_root_only(harness, tmp_path):
    """091 D11: the root agent gets every Archie tool; the worker lists all but question."""
    assert "question" in harness._mcp.tool_names()
    root = json.loads((tmp_path / "kiro" / "agents" / "archie.json").read_text())
    worker = json.loads((tmp_path / "kiro" / "agents" / "archie-worker.json").read_text())
    assert "@archie" in root["tools"]
    assert "@archie/question" not in worker["tools"]
    expected = {f"@archie/{n}" for n in harness._mcp.tool_names()} - {"@archie/question"}
    assert set(worker["tools"]) == expected and "@archie/shell" in expected


async def test_question_blocks_until_answered(harness, home):
    """091 D9: a Kiro question waits for the answer and shows as live state."""
    seen = _capture(harness)
    task = asyncio.create_task(harness.handle_message("question"))
    pending = await _until(lambda: harness.pending_question)
    assert [o.label for o in pending.options] == ["Blue", "Red"]  # recommended first
    status = harness.status_event()
    assert (status.state, status.question) == ("blocked", pending)
    wrong = harness.answer_question(AnswerCommand(question_id="nope", option=0))
    assert wrong.kind == "question_not_pending"
    assert harness.answer_question(AnswerCommand(question_id=pending.id, option=0)) is None
    await task
    events = _log(home)
    call = next(e for e in events if isinstance(e, ToolCall))
    result = next(e for e in events if isinstance(e, ToolResult))
    assert call.name == "question" and result.content == "User selected option 1: Blue"
    assert isinstance(events[-1], TurnComplete)
    statuses = [e for e in seen if isinstance(e, SessionStatus)]
    assert any(s.state == "blocked" and s.question == pending for s in statuses)
    assert (statuses[-1].state, statuses[-1].question) == ("idle", None)


async def test_interrupt_clears_a_pending_question(harness, home, tmp_path):
    """091 D10: Kiro doesn't cancel the MCP call, so the harness ends the wait."""
    seen = _capture(harness)
    task = asyncio.create_task(harness.handle_message("question"))
    await _until(lambda: harness.pending_question)
    harness.interrupt()
    await task
    assert isinstance(_log(home)[-1], TurnInterrupted)
    assert harness.pending_question is None
    last = [e for e in seen if isinstance(e, SessionStatus)][-1]
    assert (last.state, last.question) == ("idle", None)
    late = await _until(lambda: _recorded(tmp_path, "question_after_cancel"))
    assert late == "Error: the turn ended before an answer"
    await harness.handle_message("run it")
    assert isinstance(_log(home)[-1], TurnComplete)


async def test_question_call_failed_by_kiro_clears_it(harness, home, tmp_path):
    """091 D10: a question tool call Kiro ends (a timeout) doesn't leave a stale question."""
    await harness.handle_message("question_fail")
    assert harness.pending_question is None
    late = _recorded(tmp_path, "question_after_fail")
    assert late == "Error: Kiro ended the question tool call before an answer"
    result = next(e for e in _log(home) if isinstance(e, ToolResult))
    assert result.is_error


async def test_switch_model(harness, tmp_path):
    assert await harness.switch_model("bedrock-anthropic.claude-sonnet-4-6") is not None
    assert await harness.switch_model("kiro/claude-sonnet-5.5") is None
    assert harness.session.model_id == "kiro/claude-sonnet-5.5"
    sets = [
        r["in"]["params"]
        for r in _record(tmp_path)
        if r.get("in", {}).get("method") == "session/set_config_option"
    ]
    assert sets[-1]["configId"] == "model" and sets[-1]["value"] == "claude-sonnet-5.5"


async def test_close_kills_kiro_and_mcp(home, tmp_path):
    h = _harness(home, tmp_path)
    await h.start()
    pid = h.pid
    url = h.mcp_url
    await h.close()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    port = int(url.rsplit(":", 1)[1].split("/")[0])
    with pytest.raises(OSError):
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()


async def test_missing_key_fails_startup(home, tmp_path):
    (home / "credentials.yaml").write_text("{}\n")
    h = _harness(home, tmp_path)
    with pytest.raises(KiroStartupError, match="No Kiro API key"):
        await h.start()
    await h.close()


async def test_app_lifespan_on_kiro(home, tmp_path, monkeypatch):
    """D19: invalid global.model doesn't block Kiro; hooks run; model_key is kiro/<default>."""
    from unittest.mock import patch

    from archie_agent import app as app_module

    (home / "config.yaml").write_text(yaml.safe_dump({"global": {"model": "no-such-model"}}))
    monkeypatch.setenv("ARCHIE_BACKEND", "kiro")
    monkeypatch.setenv("ARCHIE_SESSION_ID", "kirotest-01")
    hooks: list = []
    real = app_module.KiroHarness

    def make(config, **kwargs):
        kwargs.update(kiro_home=tmp_path / "kiro", command=[sys.executable, str(FAKE)])
        kwargs["workspace"] = str(tmp_path)
        return real(config, **kwargs)

    with (
        patch.object(app_module, "run_startup_hooks", lambda *a, **k: hooks.append(k)),
        patch.object(app_module, "KiroHarness", make),
    ):
        async with app_module.lifespan(app_module.app):
            agent = app_module._agent
            assert isinstance(agent, real)
            pid = agent.pid
    assert len(hooks) == 1
    first = _log(home)[0]
    assert first.model_key == "kiro/gpt-5.6-luna"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    app_module._agent = None


async def test_app_lifespan_unknown_backend_exits(monkeypatch):
    from archie_agent import app as app_module

    monkeypatch.setenv("ARCHIE_BACKEND", "bogus")
    with pytest.raises(SystemExit) as exc:
        async with app_module.lifespan(app_module.app):
            pass
    assert exc.value.code == 1


async def test_app_lifespan_startup_failure_writes_error_file(home, tmp_path, monkeypatch):
    from archie_agent import app as app_module

    (home / "credentials.yaml").write_text("{}\n")
    monkeypatch.setenv("ARCHIE_BACKEND", "kiro")
    monkeypatch.setenv("ARCHIE_SESSION_ID", "kirotest-01")
    from unittest.mock import patch

    real = app_module.KiroHarness

    def make(config, **kwargs):
        kwargs.update(kiro_home=tmp_path / "kiro", command=[sys.executable, str(FAKE)])
        return real(config, **kwargs)

    with (
        patch.object(app_module, "run_startup_hooks", lambda *a, **k: None),
        patch.object(app_module, "KiroHarness", make),
        pytest.raises(SystemExit) as exc,
    ):
        async with app_module.lifespan(app_module.app):
            pass
    assert exc.value.code == 1
    reason = (home / "logs" / "kiro" / "kirotest-01.startup-error").read_text()
    assert "No Kiro API key" in reason


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("config_error", "agent config error"),
        ("wrong_mode", "Kiro mode is 'vibe'"),
        ("builtin_tag", "built-in tools shell"),
        ("sticky_collection", "contentCollection is 'enabled'"),
        ("autopilot_off", "autopilot is 'off'"),
        ("no_model", "does not offer kiro.default_model 'gpt-5.6-luna'"),
        ("mcp_failed", "archie MCP server is connecting"),
        ("exit_on_start", "Kiro backend stopped (exit code 7)"),
        ("hang", "timed out"),
    ],
)
async def test_startup_check_fails_closed(home, tmp_path, monkeypatch, scenario, reason):
    monkeypatch.setenv("FAKE_KIRO_SCENARIO", scenario)
    h = _harness(home, tmp_path, startup_timeout=2.0)
    started = time.monotonic()
    with pytest.raises(KiroStartupError) as exc:
        await h.start()
    assert time.monotonic() - started < 4
    assert reason in str(exc.value)
    await h.close()


async def test_builtin_tag_after_startup_kills_backend(home, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KIRO_SCENARIO", "late_builtin")
    h = _harness(home, tmp_path)
    await h.start()
    try:
        await asyncio.wait_for(h.handle_message("run it"), 10)
        assert h.dead
        events = _log(home)
        assert isinstance(events[-1], TurnError)
        assert "Kiro backend stopped" in events[-1].message
    finally:
        await h.close()


def _skill(root: Path, name: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nname: {name}\ndescription: {name} skill\n---\nBody\n")
    return path


async def test_agent_file_lists_skills_of_enabled_plugins_only(home, tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    for plugin in ("enabledp", "disabledp"):
        (plugins / plugin).mkdir(parents=True)
        (plugins / plugin / "plugin.yaml").write_text(f"name: {plugin}\n")
        _skill(plugins / plugin / "skills", f"{plugin}-skill")
    persona = tmp_path / "persona"
    persona_skill = _skill(persona / "skills", "persona-skill")
    (persona / "prompts").mkdir()
    from archie_shared.config import persona_dir

    for prompt in (persona_dir() / "prompts").glob("*.md"):
        (persona / "prompts" / prompt.name).write_text(prompt.read_text())
    monkeypatch.setenv("ARCHIE_PLUGINS_DIR", str(plugins))
    monkeypatch.setenv("ARCHIE_PERSONA_DIR", str(persona))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    # Without plugins/kiro the credential type is unknown; the env fallback supplies the key.
    monkeypatch.setenv("KIRO_API_KEY", SECRET)
    h = KiroHarness(
        NexusConfig(),
        session_id="kirotest-01",
        log_dir=home / "sessions",
        tool_policy=LiveToolPolicy({}, loader=None, enabled=["enabledp"]),
        kiro_home=tmp_path / "kiro",
        command=[sys.executable, str(FAKE)],
        workspace=str(tmp_path),
    )
    await h.start()
    await h.close()
    agent = json.loads((tmp_path / "kiro" / "agents" / "archie.json").read_text())
    assert sorted(agent["resources"]) == sorted(
        [
            f"skill://{persona_skill}",
            f"skill://{plugins / 'enabledp' / 'skills' / 'enabledp-skill' / 'SKILL.md'}",
        ]
    )


def _config_sets(tmp_path: Path) -> list[tuple[str, str]]:
    return [
        (r["in"]["params"]["configId"], r["in"]["params"]["value"])
        for r in _record(tmp_path)
        if r.get("in", {}).get("method") == "session/set_config_option"
    ]


def _kiro_config(**kiro) -> NexusConfig:
    import msgspec

    return msgspec.convert({"kiro": kiro}, NexusConfig)


async def test_effort_applied_when_model_offers_it(home, tmp_path):
    h = _harness(home, tmp_path)
    h._kiro = _kiro_config(default_model="claude-sonnet-5.5", effort="low").kiro
    h.session.model_id = "kiro/claude-sonnet-5.5"
    await h.start()
    await h.close()
    assert _config_sets(tmp_path)[-2:] == [("model", "claude-sonnet-5.5"), ("effortLevel", "low")]


async def test_effort_unsupported_level_warns_and_keeps_default(home, tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    h = _harness(home, tmp_path)
    h._kiro = _kiro_config(effort="max").kiro  # gpt-5.6-luna offers no effort in the fake
    await h.start()
    await h.close()
    assert "effortLevel" not in [c for c, _ in _config_sets(tmp_path)]
    assert "does not offer effort 'max'" in caplog.text


async def test_effort_unset_sends_nothing(harness, tmp_path):
    assert "effortLevel" not in [c for c, _ in _config_sets(tmp_path)]


async def test_switch_model_reapplies_effort(home, tmp_path):
    h = _harness(home, tmp_path)
    h._kiro = _kiro_config(effort="high").kiro
    await h.start()
    try:
        assert await h.switch_model("kiro/claude-opus-5.5") is None
    finally:
        await h.close()
    assert _config_sets(tmp_path)[-2:] == [("model", "claude-opus-5.5"), ("effortLevel", "high")]


async def test_switch_model_rejects_model_outside_allowlist(home, tmp_path):
    h = _harness(home, tmp_path)
    h._kiro = _kiro_config(models=["gpt-5.6-luna"]).kiro
    await h.start()
    try:
        assert await h.switch_model("kiro/claude-opus-5.5") == "Unknown model: kiro/claude-opus-5.5"
        assert h.session.model_id == "kiro/gpt-5.6-luna"
    finally:
        await h.close()


async def test_app_model_switch_routes_to_kiro(harness, monkeypatch):
    from archie_agent import app as app_module
    from archie_shared.commands import SwitchModelCommand
    from archie_shared.events import SessionStatus

    seen: list = []
    original = harness.event_bus.emit

    async def capture(event, target=None):
        seen.append(event)
        return await original(event, target)

    harness.event_bus.emit = capture
    monkeypatch.setattr(app_module, "_agent", harness)
    monkeypatch.setattr(app_module, "_catalog", {})
    monkeypatch.setattr(app_module, "_config", NexusConfig())
    websocket = object()
    await app_module._handle_model_switch(SwitchModelCommand(model_key="bedrock-x"), websocket)
    assert [(e.kind, e.message) for e in seen if isinstance(e, ErrorNotice)] == [
        ("unknown_model", "Unknown model: bedrock-x")
    ]
    await app_module._handle_model_switch(
        SwitchModelCommand(model_key="kiro/claude-sonnet-5.5"), websocket
    )
    assert seen[-1].__class__ is SessionStatus
    assert seen[-1].model_key == "kiro/claude-sonnet-5.5"


async def test_content_collection_reenabled_after_startup_kills_backend(
    home, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_KIRO_SCENARIO", "late_collection")
    h = _harness(home, tmp_path)
    await h.start()
    try:
        await asyncio.wait_for(h.handle_message("run it"), 10)
        assert h.dead
        assert isinstance(_log(home)[-1], TurnError)
    finally:
        await h.close()


async def test_close_during_exit_still_fails_pending_request(home, tmp_path):
    """close() racing the reader's EOF handling must still fail outstanding requests."""
    h = _harness(home, tmp_path)
    await h.start()
    pending = asyncio.ensure_future(
        h._acp.request(
            "session/prompt",
            {"sessionId": "sess_fake", "prompt": [{"type": "text", "text": "hang"}]},
        )
    )
    await asyncio.sleep(0.05)
    await h._acp.close()
    with pytest.raises(Exception, match="Kiro backend stopped"):
        await asyncio.wait_for(pending, 2)
    assert h.dead
    await h.close()


async def test_lifespan_reports_unexpected_startup_error(home, tmp_path, monkeypatch):
    from unittest.mock import patch

    from archie_agent import app as app_module

    monkeypatch.setenv("ARCHIE_BACKEND", "kiro")
    monkeypatch.setenv("ARCHIE_SESSION_ID", "kirotest-01")
    real = app_module.KiroHarness

    def make(config, **kwargs):
        kwargs.update(kiro_home=tmp_path / "kiro", command=[sys.executable, str(FAKE)])
        harness = real(config, **kwargs)

        async def boom():
            harness._api_key = SECRET
            raise OSError(f"disk full near {SECRET}")

        harness.start = boom
        return harness

    with (
        patch.object(app_module, "run_startup_hooks", lambda *a, **k: None),
        patch.object(app_module, "KiroHarness", make),
        pytest.raises(SystemExit),
    ):
        async with app_module.lifespan(app_module.app):
            pass
    reason = (home / "logs" / "kiro" / "kirotest-01.startup-error").read_text()
    assert reason.startswith("OSError: disk full near [redacted]")
    assert SECRET not in reason

"""Tests for `archie stats` (CLI over the local metrics index)."""

from __future__ import annotations

import json

import pytest
from archie_cli.cli import main
from click.testing import CliRunner


@pytest.fixture
def home(session_logs, tmp_path):
    w = session_logs
    w.write(
        "proj-01k0000000",
        [
            w.session_started(),
            w.user("Fix the parser", turn=1),
            w.llm(
                event_id="r1", model_key="m1", cost_usd=1.25, sent_at="2026-07-01T10:00:00+00:00"
            ),
            w.tool_call("shell", {"command": "ls"}, tool_use_id="tu1", request_id="r1"),
            w.tool_result("[exit: 2]\nls: nope", tool_use_id="tu1", is_error=True, duration_ms=30),
            w.turn_complete(turn=1),
        ],
    )
    return tmp_path


def _invoke(*args):
    return CliRunner().invoke(main, ["stats", *args])


def test_stats_json_prints_overview(home):
    result = _invoke("--since", "all", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["totals"]["cost"] == 1.25
    assert data["totals"]["requests"] == 1
    assert (home / "metrics.db").exists()


def test_stats_default_table(home):
    result = _invoke("--since", "all")
    assert result.exit_code == 0, result.output
    assert "Overview" in result.output
    assert "$1.25" in result.output
    assert "m1" in result.output


def test_stats_invalid_since_exits_2(home):
    result = _invoke("--since", "bad")
    assert result.exit_code == 2
    assert "invalid since: bad" in result.output


def test_stats_session_not_found(home):
    result = _invoke("session", "nope-01k0000000")
    assert result.exit_code == 1
    assert "Session not found" in result.output


def test_stats_works_without_orchestrator(home, monkeypatch):
    import httpx

    def refuse(*args, **kwargs):
        raise AssertionError("stats must not call the orchestrator")

    monkeypatch.setattr(httpx, "get", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)
    assert _invoke("--since", "all", "--json").exit_code == 0

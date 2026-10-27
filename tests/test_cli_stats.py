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
    # A wide terminal so table cells aren't truncated in assertions.
    return CliRunner(env={"COLUMNS": "200"}).invoke(main, ["stats", *args])


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


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["sessions", "--since", "all"], "Fix the parser"),
        (["sessions", "--since", "all", "--sort", "cost", "--limit", "1"], "proj-01k0000000"),
        (["session", "proj-01k0000000"], "Turns"),
        (["models", "--since", "all"], "m1"),
        (["tools", "--since", "all"], "shell"),
        (["subagents", "--since", "all"], "Skills"),
        (["errors", "--since", "all"], "Request errors"),
    ],
)
def test_stats_subcommands_render(home, args, expected):
    result = _invoke(*args)
    assert result.exit_code == 0, result.output
    assert expected in result.output


def test_stats_tools_shows_exit_header_literally(home):
    result = _invoke("tools", "--since", "all")
    assert "[exit: 2] ls: nope" in result.output


@pytest.mark.parametrize("view", ["sessions", "models", "tools", "subagents", "errors"])
def test_stats_subcommands_json(home, view):
    result = _invoke(view, "--since", "all", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["filters"]["since"] is None


def test_stats_session_json(home):
    result = _invoke("session", "proj-01k0000000", "--json")
    assert json.loads(result.output)["session"]["title"] == "Fix the parser"


def test_stats_bad_sort_rejected(home):
    result = _invoke("sessions", "--sort", "nope")
    assert result.exit_code == 2


def test_group_options_apply_to_subcommand(home):
    result = _invoke("--since", "all", "--json", "tools")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["filters"]["since"] is None


def test_subcommand_option_wins_over_group(home):
    result = _invoke(
        "--workspace", "nope", "models", "--workspace", "proj", "--json", "--since", "all"
    )
    assert json.loads(result.output)["filters"]["workspace"] == "proj"


def test_session_title_with_markup_prints_literally(session_logs, tmp_path):
    w = session_logs
    w.write("mk-01k0000000", [w.user("fix [/] and [exit: 2] now"), w.llm()])
    result = _invoke("session", "mk-01k0000000")
    assert result.exit_code == 0, result.output
    assert "fix [/] and [exit: 2] now" in result.output


def test_filter_values_with_markup_print_literally(home):
    result = _invoke("--since", "all", "--workspace", "[/]")
    assert result.exit_code == 0, result.output
    assert "workspace [/]" in result.output


def test_waits_for_lock_with_notice(home, monkeypatch):
    import threading

    from archie_cli import stats as stats_module
    from archie_orchestrator.metrics import MetricsIndex

    monkeypatch.setattr(stats_module, "_WAIT_NOTICE_AFTER", 0.05)
    index = MetricsIndex(home / "metrics.db", home / "sessions")
    release = threading.Event()

    def hold():
        with index._locked(wait=True):
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    threading.Timer(0.3, release.set).start()
    try:
        result = _invoke("--since", "all", "--json")
    finally:
        release.set()
        holder.join()
    assert result.exit_code == 0
    assert "Waiting for index sync" in result.stderr

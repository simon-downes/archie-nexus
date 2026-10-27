"""Tests for archie_orchestrator.insights: filters and the seven views."""

from __future__ import annotations

from datetime import date

import pytest
from archie_orchestrator import insights
from archie_orchestrator.insights import InsightsError, parse_filters, percentile
from archie_orchestrator.metrics import MetricsIndex, connect

ALL = parse_filters(since="all")


@pytest.fixture
def db(session_logs, tmp_path):
    """Two sessions in two workspaces; returns an open connection after a sync."""
    w = session_logs
    w.write(
        "alpha-01k0000000",
        [
            w.session_started(sent_at="2026-07-01T09:00:00+00:00"),
            w.user("Alpha session title", turn=1),
            w.llm(
                event_id="a1",
                turn=1,
                model_key="m1",
                cost_usd=1.0,
                sent_at="2026-07-01T10:00:00+00:00",
                duration_ms=1000,
                input_tokens=10,
                cache_read_tokens=80,
                cache_write_tokens=10,
                context_tokens=500,
            ),
            w.tool_call(
                "task",
                {
                    "tasks": [
                        {"agent": "default", "prompt": "p"},
                        {"agent": "research", "prompt": "q"},
                    ]
                },
                tool_use_id="tu-task",
                request_id="a1",
            ),
            w.tool_call("skill", {"name": "build-it"}, tool_use_id="tu-skill", request_id="a1"),
            w.tool_call("shell", {"command": "false"}, tool_use_id="tu-sh", request_id="a1"),
            w.llm(
                event_id="ac0",
                scope="tu-task",
                index=0,
                model_key="m2",
                cost_usd=0.2,
                sent_at="2026-07-01T10:00:05+00:00",
                duration_ms=3000,
            ),
            w.turn_complete(scope="tu-task", index=0),
            w.llm(
                event_id="ac1",
                scope="tu-task",
                index=1,
                model_key="m2",
                cost_usd=0.3,
                status="error",
                error="Throttled",
                sent_at="2026-07-01T10:00:06+00:00",
            ),
            w.turn_error("child failed", scope="tu-task", index=1),
            w.tool_result("ok", tool_use_id="tu-task", duration_ms=9000),
            w.tool_result("body", tool_use_id="tu-skill", duration_ms=5),
            w.tool_result("[exit: 1]\nboom", tool_use_id="tu-sh", is_error=True, duration_ms=20),
            w.turn_complete(turn=1),
            w.user("second", turn=2),
            w.llm(
                event_id="a2",
                turn=2,
                model_key="m1",
                cost_usd=0.5,
                sent_at="2026-07-02T10:00:00+00:00",
                duration_ms=2000,
                context_tokens=900,
            ),
            w.turn_error("provider down", turn=2),
        ],
    )
    w.write(
        "beta-01k0000001",
        [
            w.session_started(sent_at="2026-07-03T09:00:00+00:00"),
            w.user("Beta", turn=1),
            w.llm(
                event_id="b1",
                turn=1,
                model_key="m1",
                cost_usd=2.5,
                sent_at="2026-07-03T10:00:00+00:00",
                duration_ms=4000,
                status="no_usage",
            ),
            w.tool_call("read", {"path": "x"}, tool_use_id="tu-read", request_id="b1"),
            w.tool_result("data", tool_use_id="tu-read", duration_ms=3),
            w.turn_interrupted(turn=1),
        ],
    )
    MetricsIndex(tmp_path / "metrics.db", tmp_path / "sessions").sync()
    conn = connect(tmp_path / "metrics.db")
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_since_days_counts_today():
    f = parse_filters(since="7d", today=date(2026, 10, 26))
    assert f.since == date(2026, 10, 20)


def test_since_default_is_30_days():
    assert parse_filters(today=date(2026, 10, 30)).since == date(2026, 10, 1)


def test_since_all_and_date():
    assert parse_filters(since="all").since is None
    assert parse_filters(since="2026-07-02").since == date(2026, 7, 2)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"since": "2026-13-01"}, "invalid since: 2026-13-01"),
        ({"since": "0d"}, "invalid since: 0d"),
        ({"until": "soon"}, "invalid until: soon"),
        ({"scope": "children"}, "invalid scope: children"),
    ],
)
def test_invalid_filters_raise_naming_value(kwargs, message):
    with pytest.raises(InsightsError, match=message):
        parse_filters(**kwargs)


def test_percentile_nearest_rank():
    values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert percentile(values, 50) == 50
    assert percentile(values, 95) == 100
    assert percentile([7], 95) == 7
    assert percentile([], 50) is None


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def test_overview_totals(db):
    data = insights.overview(db, ALL)
    t = data["totals"]
    assert t["cost"] == pytest.approx(4.5)
    assert t["requests"] == 5
    assert t["sessions"] == 2
    assert t["turns"] == 3
    assert t["errors"] == 1  # only status='error'; no_usage is separate (D26)
    assert t["no_usage_requests"] == 1
    assert t["turn_errors"] == 1
    assert t["interruptions"] == 1
    assert t["cache_hit_pct"] is not None
    assert [w["workspace"] for w in data["top_workspaces"]] == ["beta", "alpha"]
    assert data["top_models"][0]["model"] == "m1"


def test_overview_daily_cost_one_entry_per_day(db):
    daily = insights.overview(db, ALL)["daily_cost"]
    assert daily["days"] == ["2026-07-01", "2026-07-02", "2026-07-03"]
    m1 = next(s for s in daily["series"] if s["model"] == "m1")
    assert m1["values"] == [1.0, 0.5, 2.5]


def test_overview_scope_root_excludes_children(db):
    root = insights.overview(db, parse_filters(since="all", scope="root"))["totals"]
    child = insights.overview(db, parse_filters(since="all", scope="subagent"))["totals"]
    assert root["cost"] == pytest.approx(4.0)
    assert child["cost"] == pytest.approx(0.5)


def test_overview_date_and_workspace_filters(db):
    f = parse_filters(since="2026-07-02", until="2026-07-03")
    t = insights.overview(db, f)["totals"]
    assert t["cost"] == pytest.approx(0.5)
    assert t["turn_errors"] == 1
    assert t["sessions"] == 0  # no session started on 2026-07-02
    w = insights.overview(db, parse_filters(since="all", workspace="beta"))["totals"]
    assert w["cost"] == pytest.approx(2.5) and w["sessions"] == 1


def test_overview_model_filter(db):
    t = insights.overview(db, parse_filters(since="all", model="m2"))["totals"]
    assert t["requests"] == 2

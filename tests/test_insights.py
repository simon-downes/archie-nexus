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


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def test_sessions_default_sort_newest_first(db):
    data = insights.sessions(db, ALL)
    assert data["total"] == 2
    assert [s["session_id"] for s in data["sessions"]] == ["beta-01k0000001", "alpha-01k0000000"]
    alpha = data["sessions"][1]
    assert alpha["title"] == "Alpha session title"
    assert alpha["turns"] == 2
    assert alpha["requests"] == 4
    assert alpha["cost"] == pytest.approx(2.0)
    assert alpha["errors"] == 2  # one error request + one root turn error
    assert alpha["duration_s"] == 90002  # 09:00 day 1 -> 10:00:02 day 2


def test_sessions_sort_by_cost(db):
    data = insights.sessions(db, ALL, sort="cost")
    assert [s["session_id"] for s in data["sessions"]] == ["beta-01k0000001", "alpha-01k0000000"]
    data = insights.sessions(db, ALL, sort="turns")
    assert data["sessions"][0]["session_id"] == "alpha-01k0000000"


def test_sessions_unknown_sort_raises(db):
    with pytest.raises(InsightsError, match="invalid sort: nope"):
        insights.sessions(db, ALL, sort="nope")


def test_sessions_limit_and_total(db):
    data = insights.sessions(db, ALL, limit=1)
    assert data["total"] == 2 and len(data["sessions"]) == 1


def test_sessions_model_filter_drops_sessions_without_matches(db):
    data = insights.sessions(db, parse_filters(since="all", model="m2"))
    assert [s["session_id"] for s in data["sessions"]] == ["alpha-01k0000000"]
    assert data["sessions"][0]["cost"] == pytest.approx(0.5)


def test_sessions_date_filter_uses_session_start(db):
    data = insights.sessions(db, parse_filters(since="2026-07-03"))
    assert [s["session_id"] for s in data["sessions"]] == ["beta-01k0000001"]


def test_sessions_cap_is_200(session_logs, tmp_path):
    w = session_logs
    for n in range(201):
        w.write(f"bulk-01k{n:07d}", [w.llm(cost_usd=0.01)])
    MetricsIndex(tmp_path / "metrics.db", tmp_path / "sessions").sync()
    conn = connect(tmp_path / "metrics.db")
    try:
        data = insights.sessions(conn, ALL, limit=500)
    finally:
        conn.close()
    assert data["total"] == 201 and len(data["sessions"]) == 200


# ---------------------------------------------------------------------------
# Session detail
# ---------------------------------------------------------------------------


def test_session_detail_turn_cost_includes_children(db):
    data = insights.session_detail(db, "alpha-01k0000000")
    turn1, turn2 = data["turns"]
    assert turn1["cost"] == pytest.approx(1.5)  # root 1.0 + children 0.2 + 0.3 (D13)
    assert turn1["requests"] == 3
    assert turn1["tool_calls"] == 3
    assert turn1["context_tokens"] == 500
    assert turn1["errors"] == 2  # child error request + shell error
    assert turn2["status"] == "error" and turn2["error"] == "provider down"
    assert [r["context_tokens"] for r in data["requests"] if r["scope"] is None] == [500, 900]
    assert {t["name"] for t in data["tools"]} == {"task", "skill", "shell"}
    agents = {(a["agent"], a["status"]): a["cost"] for a in data["subagents"]}
    assert agents == {("default", "completed"): 0.2, ("research", "error"): 0.3}


def test_session_detail_unknown_raises_key_error(db):
    with pytest.raises(KeyError):
        insights.session_detail(db, "nope-01k0000000")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def test_models_usage_latency_and_errors(db):
    data = {m["model"]: m for m in insights.models(db, ALL)["models"]}
    m1, m2 = data["m1"], data["m2"]
    assert m1["requests"] == 3 and m1["cost"] == pytest.approx(4.0)
    assert m1["p50_ms"] == 1000 and m1["p95_ms"] == 2000  # completed only; no_usage excluded
    assert m1["no_usage"] == 1 and m1["errors"] == 0
    assert m2["errors"] == 1 and m2["error_pct"] == 50.0
    assert m2["p50_ms"] == 3000


def test_models_percentiles_known_set(session_logs, tmp_path):
    w = session_logs
    w.write("p-01k0000000", [w.llm(duration_ms=d) for d in range(100, 1100, 100)])
    MetricsIndex(tmp_path / "metrics.db", tmp_path / "sessions").sync()
    conn = connect(tmp_path / "metrics.db")
    try:
        (row,) = insights.models(conn, ALL)["models"]
    finally:
        conn.close()
    assert (row["p50_ms"], row["p95_ms"]) == (500, 1000)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def test_tools_stats_and_top_errors(db):
    data = insights.tools(db, ALL)
    rows = {t["name"]: t for t in data["tools"]}
    assert rows["shell"]["errors"] == 1 and rows["shell"]["error_pct"] == 100.0
    assert rows["task"]["p50_ms"] == 9000
    assert rows["read"]["calls"] == 1
    assert data["top_errors"] == [{"name": "shell", "error": "[exit: 1] boom", "count": 1}]


def test_tools_filters(db):
    beta = insights.tools(db, parse_filters(since="all", workspace="beta"))
    assert [t["name"] for t in beta["tools"]] == ["read"]
    sub = insights.tools(db, parse_filters(since="all", scope="subagent"))
    assert sub["tools"] == []


# ---------------------------------------------------------------------------
# Subagents and skills
# ---------------------------------------------------------------------------


def test_subagents_and_skills(db):
    data = insights.subagents(db, ALL)
    agents = {a["agent"]: a for a in data["agents"]}
    assert agents["default"]["launches"] == 1
    assert agents["default"]["cost"] == pytest.approx(0.2)
    assert agents["research"]["errors"] == 1
    assert agents["research"]["cost_per_launch"] == pytest.approx(0.3)
    assert data["skills"] == [{"skill": "build-it", "loads": 1, "sessions": 1}]


def test_subagents_date_filter(db):
    data = insights.subagents(db, parse_filters(since="2026-07-02"))
    assert data["agents"] == [] and data["skills"] == []


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_errors_view(db):
    data = insights.errors(db, ALL)
    assert data["totals"] == {
        "request_errors": 1,
        "interrupted_requests": 0,
        "no_usage_requests": 1,
        "turn_errors": 1,
        "subagent_errors": 1,
    }
    assert data["by_model"] == [{"model": "m2", "errors": 1}]
    assert data["top_messages"][0]["error"] == "Throttled"
    assert data["turn_errors"][0]["error"] == "provider down"
    assert data["subagent_errors"][0]["agent"] == "research"
    daily = data["daily"]
    assert daily["days"] == ["2026-07-01", "2026-07-02", "2026-07-03"]
    assert daily["request_errors"] == [1, 0, 0]
    assert daily["turn_errors"] == [0, 1, 0]
    assert daily["interruptions"] == [0, 0, 1]


def test_turn_error_without_requests_uses_session_start(session_logs, tmp_path):
    w = session_logs
    w.write(
        "e-01k0000000",
        [w.session_started(sent_at="2026-07-05T08:00:00+00:00"), w.user("x"), w.turn_error("auth")],
    )
    MetricsIndex(tmp_path / "metrics.db", tmp_path / "sessions").sync()
    conn = connect(tmp_path / "metrics.db")
    try:
        data = insights.errors(conn, parse_filters(since="2026-07-05"))
    finally:
        conn.close()
    assert [e["error"] for e in data["turn_errors"]] == ["auth"]


def test_filter_options(db):
    opts = insights.filter_options(db)
    assert opts["workspaces"] == ["alpha", "beta"]
    assert opts["models"] == ["m1", "m2"]

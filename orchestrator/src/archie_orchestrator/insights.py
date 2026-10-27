"""Read-only queries over the metrics index for ``archie stats`` and ``/ui/insights``.

Each view is a function ``(conn, filters, ...) -> dict`` returning plain,
JSON-serialisable data, so the CLI and the web pages share one implementation.
Filters (plan 075 D15/D16):

- ``since`` / ``until``: UTC dates; ``until`` is exclusive. Requests and tool
  calls filter on their timestamp; turns, subagent runs and compactions on the
  turn's first request time, falling back to the session start; session lists
  and session counts on the session start.
- ``workspace``: exact workspace name, applied everywhere through ``session_id``.
- ``model``: exact ``model_key``; applies to requests and tool calls.
- ``scope``: ``all``, ``root`` (``scope IS NULL``) or ``subagent``; applies to
  requests and tool calls.

All filter values are bound parameters; sort keys come from a fixed whitelist.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

SCOPES = ("all", "root", "subagent")
DEFAULT_SINCE = "30d"
MAX_SESSIONS = 200
TOP_ERRORS = 20
MAX_ERROR_ROWS = 200
TOP_DAILY_MODELS = 5
TOP_N = 10

_DAYS = re.compile(r"^(\d{1,4})d$")


class InsightsError(ValueError):
    """An invalid filter or option value; the message names the value."""


@dataclass(frozen=True)
class Filters:
    since: date | None = None
    until: date | None = None
    workspace: str | None = None
    model: str | None = None
    scope: str = "all"

    def as_dict(self) -> dict:
        return {
            "since": self.since.isoformat() if self.since else None,
            "until": self.until.isoformat() if self.until else None,
            "workspace": self.workspace,
            "model": self.model,
            "scope": self.scope,
        }


def _parse_date(name: str, value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise InsightsError(f"invalid {name}: {value}") from None


def parse_since(value: str | None, today: date | None = None) -> date | None:
    """``Nd`` (today and the N-1 previous UTC days), ``all``, or ``YYYY-MM-DD``."""
    value = (value or "").strip() or DEFAULT_SINCE
    if value == "all":
        return None
    match = _DAYS.match(value)
    if match:
        days = int(match.group(1))
        if days < 1:
            raise InsightsError(f"invalid since: {value}")
        today = today or datetime.now(UTC).date()
        return today - timedelta(days=days - 1)
    return _parse_date("since", value)


def parse_filters(
    since: str | None = None,
    until: str | None = None,
    workspace: str | None = None,
    model: str | None = None,
    scope: str | None = None,
    *,
    today: date | None = None,
) -> Filters:
    """Validate raw filter values; raise :class:`InsightsError` naming a bad one."""
    scope = (scope or "all").strip() or "all"
    if scope not in SCOPES:
        raise InsightsError(f"invalid scope: {scope}")
    until_value = (until or "").strip()
    return Filters(
        since=parse_since(since, today),
        until=_parse_date("until", until_value) if until_value else None,
        workspace=(workspace or "").strip() or None,
        model=(model or "").strip() or None,
        scope=scope,
    )


# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------


def _range(column: str, f: Filters, clauses: list[str], params: list) -> None:
    if f.since is not None:
        clauses.append(f"{column} >= ?")
        params.append(f.since.isoformat())
    if f.until is not None:
        clauses.append(f"{column} < ?")
        params.append(f.until.isoformat())


def _workspace(alias: str, f: Filters, clauses: list[str], params: list) -> None:
    if f.workspace is not None:
        clauses.append(
            f"{alias}.session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)"
        )
        params.append(f.workspace)


def _model_scope(alias: str, f: Filters, clauses: list[str], params: list) -> None:
    if f.model is not None:
        clauses.append(f"{alias}.model_key = ?")
        params.append(f.model)
    if f.scope == "root":
        clauses.append(f"{alias}.scope IS NULL")
    elif f.scope == "subagent":
        clauses.append(f"{alias}.scope IS NOT NULL")


def _where(clauses: list[str]) -> str:
    return ("WHERE " + " AND ".join(clauses)) if clauses else ""


def _request_where(f: Filters, alias: str = "r", *, dates: bool = True) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    if dates:
        _range(f"{alias}.timestamp", f, clauses, params)
    _workspace(alias, f, clauses, params)
    _model_scope(alias, f, clauses, params)
    return _where(clauses), params


def _tool_where(f: Filters, alias: str = "c") -> tuple[str, list]:
    return _request_where(f, alias)


_TURN_TIME = "COALESCE(t.started_at, s.started_at)"
_TURN_FROM = "turns t JOIN sessions s ON s.session_id = t.session_id"


def _turn_where(f: Filters, alias: str = "t") -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    _range(_TURN_TIME, f, clauses, params)
    _workspace(alias, f, clauses, params)
    return _where(clauses), params


def _session_where(f: Filters) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    _range("s.started_at", f, clauses, params)
    if f.workspace is not None:
        clauses.append("s.workspace = ?")
        params.append(f.workspace)
    return _where(clauses), params


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def percentile(values: list, p: float):
    """Nearest-rank percentile of ``values`` (None when empty)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def cache_hit(input_tokens: int, cache_read: int, cache_write: int) -> float | None:
    """Share of prompt tokens served from cache, as a percentage (plan D18)."""
    total = (input_tokens or 0) + (cache_read or 0) + (cache_write or 0)
    if not total:
        return None
    return round(100.0 * (cache_read or 0) / total, 1)


def day_span(days) -> list[str]:
    """Every ``YYYY-MM-DD`` from the earliest to the latest of ``days``, inclusive."""
    parsed = sorted({date.fromisoformat(d) for d in days if d})
    if not parsed:
        return []
    count = (parsed[-1] - parsed[0]).days + 1
    return [(parsed[0] + timedelta(days=i)).isoformat() for i in range(count)]


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def _seconds_between(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    except ValueError:
        return None
    return max(0, int(delta.total_seconds()))


_USAGE = (
    "COUNT(r.id) AS requests, COALESCE(SUM(r.cost_usd), 0.0) AS cost, "
    "COALESCE(SUM(r.input_tokens), 0) AS input_tokens, "
    "COALESCE(SUM(r.output_tokens), 0) AS output_tokens, "
    "COALESCE(SUM(r.cache_read_tokens), 0) AS cache_read_tokens, "
    "COALESCE(SUM(r.cache_write_tokens), 0) AS cache_write_tokens, "
    "COALESCE(SUM(r.status = 'error'), 0) AS errors"
)


def _usage(row) -> dict:
    return {
        "requests": row["requests"],
        "cost": round(row["cost"], 6),
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "cache_read_tokens": row["cache_read_tokens"],
        "cache_write_tokens": row["cache_write_tokens"],
        "cache_hit_pct": cache_hit(
            row["input_tokens"], row["cache_read_tokens"], row["cache_write_tokens"]
        ),
        "errors": row["errors"],
    }


def filter_options(conn: sqlite3.Connection) -> dict:
    """Workspaces and models present in the index, for filter dropdowns."""
    workspaces = [r[0] for r in conn.execute("SELECT DISTINCT workspace FROM sessions ORDER BY 1")]
    models = [r[0] for r in conn.execute("SELECT DISTINCT model_key FROM requests ORDER BY 1")]
    return {"workspaces": workspaces, "models": models, "scopes": list(SCOPES)}


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def overview(conn: sqlite3.Connection, f: Filters) -> dict:
    """Headline totals, daily cost by model, and top workspaces and models."""
    where, params = _request_where(f)
    row = conn.execute(
        f"SELECT {_USAGE}, COALESCE(SUM(r.status = 'interrupted'), 0) AS interrupted, "
        f"COALESCE(SUM(r.status = 'no_usage'), 0) AS no_usage FROM requests r {where}",
        params,
    ).fetchone()
    totals = _usage(row)
    totals["interrupted_requests"] = row["interrupted"]
    totals["no_usage_requests"] = row["no_usage"]

    swhere, sparams = _session_where(f)
    totals["sessions"] = conn.execute(
        f"SELECT COUNT(*) FROM sessions s {swhere}", sparams
    ).fetchone()[0]
    twhere, tparams = _turn_where(f)
    turn_row = conn.execute(
        f"SELECT COUNT(*) AS turns, COALESCE(SUM(t.status = 'error'), 0) AS errors, "
        f"COALESCE(SUM(t.status = 'interrupted'), 0) AS interrupted FROM {_TURN_FROM} {twhere}",
        tparams,
    ).fetchone()
    totals["turns"] = turn_row["turns"]
    totals["turn_errors"] = turn_row["errors"]
    totals["interruptions"] = turn_row["interrupted"]

    by_model = [
        {"model": r["model"], **_usage(r)}
        for r in conn.execute(
            f"SELECT r.model_key AS model, {_USAGE} FROM requests r {where} "
            "GROUP BY r.model_key ORDER BY cost DESC, model",
            params,
        )
    ]
    by_workspace = [
        {"workspace": r["workspace"], "sessions": r["sessions"], **_usage(r)}
        for r in conn.execute(
            f"SELECT s.workspace AS workspace, COUNT(DISTINCT r.session_id) AS sessions, {_USAGE} "
            f"FROM requests r JOIN sessions s ON s.session_id = r.session_id {where} "
            "GROUP BY s.workspace ORDER BY cost DESC, workspace LIMIT ?",
            [*params, TOP_N],
        )
    ]
    return {
        "filters": f.as_dict(),
        "totals": totals,
        "daily_cost": _daily_cost(conn, where, params, [m["model"] for m in by_model]),
        "top_workspaces": by_workspace,
        "top_models": by_model[:TOP_N],
    }


def _daily_cost(conn, where: str, params: list, models_by_cost: list[str]) -> dict:
    top = models_by_cost[:TOP_DAILY_MODELS]
    rows = conn.execute(
        f"SELECT substr(r.timestamp, 1, 10) AS day, r.model_key AS model, SUM(r.cost_usd) AS cost "
        f"FROM requests r {where} GROUP BY day, model ORDER BY day",
        params,
    ).fetchall()
    days = day_span(r["day"] for r in rows)
    index = {day: i for i, day in enumerate(days)}
    names = [*top, *(["other"] if len(models_by_cost) > len(top) else [])]
    series = {name: [0.0] * len(days) for name in names}
    for r in rows:
        name = r["model"] if r["model"] in top else "other"
        series[name][index[r["day"]]] += r["cost"]
    return {
        "days": days,
        "series": [
            {"model": name, "values": [round(v, 6) for v in series[name]]} for name in names
        ],
    }


_SESSION_SORTS = {
    "started": "s.started_at DESC, s.session_id DESC",
    "cost": "cost DESC, s.started_at DESC",
    "turns": "turns DESC, s.started_at DESC",
    "requests": "requests DESC, s.started_at DESC",
    "errors": "errors DESC, s.started_at DESC",
    "duration": "duration DESC, s.started_at DESC",
}
SESSION_SORTS = tuple(_SESSION_SORTS)


def _session_select(request_where: str) -> str:
    return (
        "SELECT s.session_id, s.workspace, s.started_at, s.title, s.last_activity_at, "
        "s.initial_model, "
        "COALESCE(u.requests, 0) AS requests, COALESCE(u.cost, 0.0) AS cost, "
        "COALESCE(u.input_tokens, 0) AS input_tokens, "
        "COALESCE(u.output_tokens, 0) AS output_tokens, "
        "COALESCE(u.cache_read_tokens, 0) AS cache_read_tokens, "
        "COALESCE(u.cache_write_tokens, 0) AS cache_write_tokens, "
        "COALESCE(u.errors, 0) + (SELECT COUNT(*) FROM turns t WHERE t.session_id = s.session_id "
        "AND t.status = 'error') AS errors, "
        "(SELECT COUNT(*) FROM turns t WHERE t.session_id = s.session_id) AS turns, "
        "COALESCE((julianday(s.last_activity_at) - julianday(s.started_at)) * 86400, 0) "
        "AS duration "
        "FROM sessions s LEFT JOIN ("
        f"SELECT r.session_id, {_USAGE} FROM requests r {request_where} GROUP BY r.session_id"
        ") u ON u.session_id = s.session_id"
    )


def _session_row(row) -> dict:
    return {
        "session_id": row["session_id"],
        "workspace": row["workspace"],
        "started_at": row["started_at"],
        "title": row["title"],
        "initial_model": row["initial_model"],
        "turns": row["turns"],
        "requests": row["requests"],
        "cost": round(row["cost"], 6),
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "cache_read_tokens": row["cache_read_tokens"],
        "cache_write_tokens": row["cache_write_tokens"],
        "cache_hit_pct": cache_hit(
            row["input_tokens"], row["cache_read_tokens"], row["cache_write_tokens"]
        ),
        "errors": row["errors"],
        "duration_s": _seconds_between(row["started_at"], row["last_activity_at"]),
    }


def sessions(
    conn: sqlite3.Connection, f: Filters, *, sort: str = "started", limit: int = MAX_SESSIONS
) -> dict:
    """Sessions started in range, with usage from requests matching model/scope."""
    if sort not in _SESSION_SORTS:
        raise InsightsError(f"invalid sort: {sort}")
    if limit < 1:
        raise InsightsError(f"invalid limit: {limit}")
    rwhere, rparams = _request_where(f, dates=False)
    swhere, sparams = _session_where(f)
    clauses = [swhere[len("WHERE ") :]] if swhere else []
    if f.model is not None or f.scope != "all":
        clauses.append("u.requests > 0")
    where = _where(clauses)
    base = f"{_session_select(rwhere)} {where}"
    params = [*rparams, *sparams]
    total = conn.execute(f"SELECT COUNT(*) FROM ({base})", params).fetchone()[0]
    rows = conn.execute(
        f"{base} ORDER BY {_SESSION_SORTS[sort]} LIMIT ?", [*params, min(limit, MAX_SESSIONS)]
    ).fetchall()
    return {
        "filters": f.as_dict(),
        "sort": sort,
        "total": total,
        "sessions": [_session_row(r) for r in rows],
    }


def session_detail(conn: sqlite3.Connection, session_id: str) -> dict:
    """Per-turn, per-request, tool and subagent breakdown for one session.

    Raises ``KeyError`` when the session is not indexed. Filters don't apply.
    """
    row = conn.execute(f"{_session_select('')} WHERE s.session_id = ?", (session_id,)).fetchone()
    if row is None:
        raise KeyError(session_id)
    turns = [
        {
            "turn": r["turn"],
            "status": r["status"],
            "error": r["error"],
            "started_at": r["started_at"],
            "requests": r["requests"],
            "cost": round(r["cost"] or 0.0, 6),
            "context_tokens": r["context_tokens"],
            "tool_calls": r["tool_calls"],
            "errors": (r["request_errors"] or 0) + (r["tool_errors"] or 0),
        }
        for r in conn.execute(
            "SELECT t.turn, t.status, t.error, t.started_at, "
            "(SELECT COUNT(*) FROM requests r WHERE r.session_id = t.session_id "
            " AND r.turn = t.turn) AS requests, "
            "(SELECT SUM(r.cost_usd) FROM requests r WHERE r.session_id = t.session_id "
            " AND r.turn = t.turn) AS cost, "
            "(SELECT MAX(r.context_tokens) FROM requests r WHERE r.session_id = t.session_id "
            " AND r.turn = t.turn AND r.scope IS NULL) AS context_tokens, "
            "(SELECT COUNT(*) FROM tool_calls c WHERE c.session_id = t.session_id "
            " AND c.turn = t.turn) AS tool_calls, "
            "(SELECT SUM(r.status = 'error') FROM requests r WHERE r.session_id = t.session_id "
            " AND r.turn = t.turn) AS request_errors, "
            "(SELECT SUM(c.is_error) FROM tool_calls c WHERE c.session_id = t.session_id "
            " AND c.turn = t.turn) AS tool_errors "
            "FROM turns t WHERE t.session_id = ? ORDER BY t.turn",
            (session_id,),
        )
    ]
    requests = [
        {
            "timestamp": r["timestamp"],
            "turn": r["turn"],
            "iteration": r["iteration"],
            "scope": r["scope"],
            "model": r["model_key"],
            "status": r["status"],
            "purpose": r["purpose"],
            "context_tokens": r["context_tokens"],
            "cost": round(r["cost_usd"], 6),
            "duration_ms": r["duration_ms"],
        }
        for r in conn.execute(
            "SELECT * FROM requests WHERE session_id = ? ORDER BY id", (session_id,)
        )
    ]
    tools = _tool_rows(
        conn.execute(
            "SELECT name, is_error, duration_ms, result_bytes FROM tool_calls c "
            "WHERE c.session_id = ?",
            (session_id,),
        ).fetchall()
    )
    subagents = [
        {
            "scope": r["scope"],
            "subagent_index": r["subagent_index"],
            "agent": r["agent"],
            "turn": r["turn"],
            "status": r["status"],
            "error": r["error"],
            "requests": r["requests"],
            "cost": round(r["cost"] or 0.0, 6),
        }
        for r in conn.execute(
            "SELECT a.*, (SELECT COUNT(*) FROM requests r WHERE r.session_id = a.session_id "
            " AND r.scope = a.scope AND r.subagent_index = a.subagent_index) AS requests, "
            "(SELECT SUM(r.cost_usd) FROM requests r WHERE r.session_id = a.session_id "
            " AND r.scope = a.scope AND r.subagent_index = a.subagent_index) AS cost "
            "FROM subagents a WHERE a.session_id = ? ORDER BY a.turn, a.scope, a.subagent_index",
            (session_id,),
        )
    ]
    return {
        "session": _session_row(row),
        "turns": turns,
        "requests": requests,
        "tools": tools,
        "subagents": subagents,
    }


def models(conn: sqlite3.Connection, f: Filters) -> dict:
    """Usage, latency percentiles and error rate per model."""
    where, params = _request_where(f)
    rows = conn.execute(
        f"SELECT r.model_key AS model, {_USAGE}, "
        "COALESCE(SUM(r.status = 'interrupted'), 0) AS interrupted, "
        "COALESCE(SUM(r.status = 'no_usage'), 0) AS no_usage "
        f"FROM requests r {where} GROUP BY r.model_key ORDER BY cost DESC, model",
        params,
    ).fetchall()
    durations: dict[str, list[int]] = {}
    for r in conn.execute(
        f"SELECT r.model_key, r.duration_ms FROM requests r {where}"
        + (" AND " if where else " WHERE ")
        + "r.status = 'completed'",
        params,
    ):
        durations.setdefault(r[0], []).append(r[1])
    result = []
    for r in rows:
        values = durations.get(r["model"], [])
        result.append(
            {
                "model": r["model"],
                **_usage(r),
                "interrupted": r["interrupted"],
                "no_usage": r["no_usage"],
                "error_pct": _pct(r["errors"], r["requests"]),
                "p50_ms": percentile(values, 50),
                "p95_ms": percentile(values, 95),
            }
        )
    return {"filters": f.as_dict(), "models": result}


def _tool_rows(rows) -> list[dict]:
    grouped: dict[str, dict] = {}
    for r in rows:
        g = grouped.setdefault(r["name"], {"calls": 0, "errors": 0, "durations": [], "bytes": []})
        g["calls"] += 1
        g["errors"] += 1 if r["is_error"] else 0
        if r["duration_ms"] is not None:
            g["durations"].append(r["duration_ms"])
        if r["result_bytes"] is not None:
            g["bytes"].append(r["result_bytes"])
    result = [
        {
            "name": name,
            "calls": g["calls"],
            "errors": g["errors"],
            "error_pct": _pct(g["errors"], g["calls"]),
            "p50_ms": percentile(g["durations"], 50),
            "p95_ms": percentile(g["durations"], 95),
            "mean_bytes": round(sum(g["bytes"]) / len(g["bytes"])) if g["bytes"] else None,
            "p95_bytes": percentile(g["bytes"], 95),
        }
        for name, g in grouped.items()
    ]
    result.sort(key=lambda t: (-t["calls"], t["name"]))
    return result


def tools(conn: sqlite3.Connection, f: Filters) -> dict:
    """Calls, errors, duration and result size per tool, plus the commonest errors."""
    where, params = _tool_where(f)
    rows = conn.execute(
        f"SELECT c.name, c.is_error, c.duration_ms, c.result_bytes FROM tool_calls c {where}",
        params,
    ).fetchall()
    err_clause = (" AND " if where else " WHERE ") + "c.is_error = 1"
    top_errors = [
        {"name": r["name"], "error": r["error_summary"], "count": r["n"]}
        for r in conn.execute(
            f"SELECT c.name, c.error_summary, COUNT(*) AS n FROM tool_calls c {where}{err_clause} "
            "GROUP BY c.name, c.error_summary ORDER BY n DESC, c.name LIMIT ?",
            [*params, TOP_ERRORS],
        )
    ]
    return {"filters": f.as_dict(), "tools": _tool_rows(rows), "top_errors": top_errors}


def subagents(conn: sqlite3.Connection, f: Filters) -> dict:
    """Launches and cost per agent type, and loads per skill."""
    twhere, tparams = _turn_where(f, alias="a")
    launches = {
        r["agent"]: {"launches": r["n"], "errors": r["errors"]}
        for r in conn.execute(
            "SELECT a.agent, COUNT(*) AS n, COALESCE(SUM(a.status = 'error'), 0) AS errors "
            "FROM subagents a JOIN sessions s ON s.session_id = a.session_id "
            "LEFT JOIN turns t ON t.session_id = a.session_id AND t.turn = a.turn "
            f"{twhere} GROUP BY a.agent",
            tparams,
        )
    }
    rwhere, rparams = _request_where(f)
    usage = {
        r["agent"]: r
        for r in conn.execute(
            f"SELECT a.agent AS agent, {_USAGE} FROM requests r JOIN subagents a "
            "ON a.session_id = r.session_id AND a.scope = r.scope "
            f"AND a.subagent_index = r.subagent_index {rwhere} GROUP BY a.agent",
            rparams,
        )
    }
    agents = []
    for name in sorted(set(launches) | set(usage)):
        counts = launches.get(name, {"launches": 0, "errors": 0})
        u = usage.get(name)
        cost = round(u["cost"], 6) if u else 0.0
        agents.append(
            {
                "agent": name,
                "launches": counts["launches"],
                "errors": counts["errors"],
                "requests": u["requests"] if u else 0,
                "cost": cost,
                "cost_per_launch": round(cost / counts["launches"], 6)
                if counts["launches"]
                else None,
            }
        )
    agents.sort(key=lambda a: (-a["cost"], a["agent"]))
    cwhere, cparams = _tool_where(f)
    skill_clause = (" AND " if cwhere else " WHERE ") + "c.skill IS NOT NULL"
    skills = [
        {"skill": r["skill"], "loads": r["loads"], "sessions": r["sessions"]}
        for r in conn.execute(
            f"SELECT c.skill, COUNT(*) AS loads, COUNT(DISTINCT c.session_id) AS sessions "
            f"FROM tool_calls c {cwhere}{skill_clause} GROUP BY c.skill "
            "ORDER BY loads DESC, c.skill",
            cparams,
        )
    ]
    return {"filters": f.as_dict(), "agents": agents, "skills": skills}


def errors(conn: sqlite3.Connection, f: Filters) -> dict:
    """Request errors by model, root and subagent turn errors, and daily counts."""
    rwhere, rparams = _request_where(f)
    and_ = " AND " if rwhere else " WHERE "
    by_model = [
        {"model": r["model_key"], "errors": r["n"]}
        for r in conn.execute(
            f"SELECT r.model_key, COUNT(*) AS n FROM requests r {rwhere}{and_}r.status = 'error' "
            "GROUP BY r.model_key ORDER BY n DESC, r.model_key",
            rparams,
        )
    ]
    top_messages = [
        {"model": r["model_key"], "error": r["error"], "count": r["n"]}
        for r in conn.execute(
            f"SELECT r.model_key, r.error, COUNT(*) AS n FROM requests r {rwhere}{and_}"
            "r.status = 'error' GROUP BY r.model_key, r.error ORDER BY n DESC LIMIT ?",
            [*rparams, TOP_ERRORS],
        )
    ]
    counts = conn.execute(
        "SELECT COALESCE(SUM(r.status = 'error'), 0), COALESCE(SUM(r.status = 'interrupted'), 0), "
        f"COALESCE(SUM(r.status = 'no_usage'), 0) FROM requests r {rwhere}",
        rparams,
    ).fetchone()
    twhere, tparams = _turn_where(f)
    tand = " AND " if twhere else " WHERE "
    turn_error_count = conn.execute(
        f"SELECT COUNT(*) FROM {_TURN_FROM} {twhere}{tand}t.status = 'error'", tparams
    ).fetchone()[0]
    turn_errors = [
        {"session_id": r["session_id"], "turn": r["turn"], "error": r["error"], "at": r["at"]}
        for r in conn.execute(
            f"SELECT t.session_id, t.turn, t.error, {_TURN_TIME} AS at FROM {_TURN_FROM} "
            f"{twhere}{tand}t.status = 'error' ORDER BY at DESC, t.session_id, t.turn DESC LIMIT ?",
            [*tparams, MAX_ERROR_ROWS],
        )
    ]
    awhere, aparams = _turn_where(f, alias="a")
    aand = " AND " if awhere else " WHERE "
    subagent_from = (
        "subagents a JOIN sessions s ON s.session_id = a.session_id "
        "LEFT JOIN turns t ON t.session_id = a.session_id AND t.turn = a.turn"
    )
    subagent_error_count = conn.execute(
        f"SELECT COUNT(*) FROM {subagent_from} {awhere}{aand}a.status = 'error'", aparams
    ).fetchone()[0]
    subagent_errors = [
        {"session_id": r["session_id"], "agent": r["agent"], "error": r["error"], "at": r["at"]}
        for r in conn.execute(
            f"SELECT a.session_id, a.agent, a.error, {_TURN_TIME} AS at FROM {subagent_from} "
            f"{awhere}{aand}a.status = 'error' ORDER BY at DESC LIMIT ?",
            [*aparams, MAX_ERROR_ROWS],
        )
    ]
    daily: dict[str, dict[str, int]] = {}
    request_days = [
        r[0]
        for r in conn.execute(
            f"SELECT DISTINCT substr(r.timestamp, 1, 10) FROM requests r {rwhere}", rparams
        )
    ]
    for r in conn.execute(
        f"SELECT substr(r.timestamp, 1, 10) AS day, COUNT(*) FROM requests r {rwhere}{and_}"
        "r.status = 'error' GROUP BY day",
        rparams,
    ):
        daily.setdefault(r[0], {})["request_errors"] = r[1]
    for r in conn.execute(
        f"SELECT substr({_TURN_TIME}, 1, 10) AS day, SUM(t.status = 'error'), "
        f"SUM(t.status = 'interrupted') FROM {_TURN_FROM} {twhere}{tand}"
        "t.status IN ('error', 'interrupted') GROUP BY day",
        tparams,
    ):
        if r[0] is None:
            continue
        daily.setdefault(r[0], {})["turn_errors"] = r[1]
        daily[r[0]]["interruptions"] = r[2]
    days = day_span([*request_days, *daily])
    return {
        "filters": f.as_dict(),
        "totals": {
            "request_errors": counts[0],
            "interrupted_requests": counts[1],
            "no_usage_requests": counts[2],
            "turn_errors": turn_error_count,
            "subagent_errors": subagent_error_count,
        },
        "by_model": by_model,
        "top_messages": top_messages,
        "turn_errors": turn_errors,
        "subagent_errors": subagent_errors,
        "daily": {
            "days": days,
            "request_errors": [daily.get(d, {}).get("request_errors", 0) for d in days],
            "turn_errors": [daily.get(d, {}).get("turn_errors", 0) for d in days],
            "interruptions": [daily.get(d, {}).get("interruptions", 0) for d in days],
        },
    }

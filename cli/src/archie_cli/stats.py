"""``archie stats``: usage insights from the local metrics index.

Each subcommand syncs ``<ARCHIE_HOME_DIR>/metrics.db`` from the session logs,
runs the matching :mod:`archie_orchestrator.insights` query, and prints it as
``rich`` tables, or as JSON with ``--json``. The orchestrator need not be
running.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from functools import wraps

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

_WAIT_NOTICE_AFTER = 1.0


def _console() -> Console:
    return Console(highlight=False, soft_wrap=False)


def _money(value) -> str:
    return "—" if value is None else f"${value:,.2f}"


def _num(value) -> str:
    return "—" if value is None else f"{value:,}"


def _pct(value) -> str:
    return "—" if value is None else f"{value:.1f}%"


def _ms(value) -> str:
    if value is None:
        return "—"
    return f"{value / 1000:.1f}s" if value >= 1000 else f"{value}ms"


def _bytes(value) -> str:
    if value is None:
        return "—"
    for unit in ("B", "KB", "MB"):
        if value < 1024 or unit == "MB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return str(value)


def _duration(seconds) -> str:
    if seconds is None:
        return "—"
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


class _Table(Table):
    """A table whose cells are data, never Rich markup (``[exit: 2]`` stays literal)."""

    def add_row(self, *cells, **kwargs) -> None:
        super().add_row(*(escape(c) if isinstance(c, str) else c for c in cells), **kwargs)


def _one_line(text: str | None, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _table(title: str, columns: list[tuple[str, str]]) -> Table:
    """Columns are ``(name, justify)``; ``"wrap"`` is a left-aligned column that wraps.

    Every other column stays on one line (text truncates with an ellipsis), so a
    row is one terminal line except where an error message needs the space.
    """
    table = _Table(title=title, title_justify="left", title_style="bold")
    for name, justify in columns:
        if justify == "wrap":
            table.add_column(name, justify="left", ratio=1)

        elif justify == "right":
            table.add_column(name, justify="right", no_wrap=True)
        else:
            table.add_column(name, justify="left", no_wrap=True, overflow="ellipsis")
    return table


def _sync_index():
    """Sync the index, telling the user on stderr if another sync holds the lock."""
    from archie_orchestrator.metrics import default_index

    index = default_index()
    timer = threading.Timer(
        _WAIT_NOTICE_AFTER, lambda: click.echo("Waiting for index sync…", err=True)
    )
    timer.daemon = True
    timer.start()
    try:
        index.sync()
    finally:
        timer.cancel()
    return index


def _open_index():
    """Sync the index and open it for reading."""
    import sqlite3

    from archie_orchestrator.metrics import connect

    index = _sync_index()
    try:
        return connect(index.db_path)
    except sqlite3.OperationalError as exc:
        raise click.ClickException(f"Metrics index unavailable: {exc}") from None


def _group_options(keys: tuple[str, ...]) -> dict:
    """Options given explicitly on ``archie stats`` itself, for a subcommand left at defaults.

    ``archie stats --since 7d tools`` applies ``--since 7d`` to ``tools``; an option
    given on the subcommand wins.
    """
    ctx = click.get_current_context()
    parent = ctx.parent
    if parent is None or parent.command is not stats:
        return {}
    result = {}
    for key in keys:
        parent_source = parent.get_parameter_source(key)
        own_source = ctx.get_parameter_source(key)
        explicit_parent = parent_source not in (None, click.core.ParameterSource.DEFAULT)
        own_default = own_source in (None, click.core.ParameterSource.DEFAULT)
        if explicit_parent and own_default:
            result[key] = parent.params[key]
    return result


def _filter_options(func: Callable) -> Callable:
    options = [
        click.option(
            "--since",
            default="30d",
            show_default=True,
            help="Start: Nd (today and N-1 previous UTC days), YYYY-MM-DD, or 'all'.",
        ),
        click.option("--until", default=None, help="End date (UTC, exclusive), YYYY-MM-DD."),
        click.option("--workspace", default=None, help="Only this workspace (exact name)."),
        click.option("--model", default=None, help="Only this model key (exact)."),
        click.option(
            "--scope",
            type=click.Choice(["all", "root", "subagent"]),
            default="all",
            show_default=True,
            help="Main agent, subagents, or both.",
        ),
        click.option("--json", "as_json", is_flag=True, help="Print JSON instead of tables."),
    ]
    for option in reversed(options):
        func = option(func)
    return func


def _run(view: Callable[..., dict], render: Callable[[dict, Console], None], filtered: bool = True):
    """Decorate a subcommand body: parse filters, sync, query, print."""

    def decorator(body: Callable) -> Callable:
        @wraps(body)
        def command(as_json: bool = False, **kwargs):
            from archie_orchestrator import insights

            filter_keys = ("since", "until", "workspace", "model", "scope")
            inherited = _group_options(("as_json", *filter_keys))
            as_json = inherited.pop("as_json", as_json)
            kwargs.update({k: v for k, v in inherited.items() if k in kwargs})
            raw = {k: kwargs.pop(k) for k in filter_keys if k in kwargs}
            try:
                filters = insights.parse_filters(**raw) if filtered else None
            except insights.InsightsError as exc:
                raise click.BadParameter(str(exc)) from None
            conn = _open_index()
            try:
                extra = body(**kwargs) or {}
                args = (conn, filters) if filtered else (conn,)
                try:
                    data = view(*args, **extra)
                except insights.InsightsError as exc:
                    raise click.BadParameter(str(exc)) from None
            finally:
                conn.close()
            if as_json:
                click.echo(json.dumps(data, indent=2))
            else:
                render(data, _console())

        return command

    return decorator


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _filters_line(data: dict) -> str:
    f = data.get("filters") or {}
    parts = [f"since {f.get('since') or 'all time'}"]
    if f.get("until"):
        parts.append(f"until {f['until']}")
    for key in ("workspace", "model"):
        if f.get(key):
            parts.append(f"{key} {f[key]}")
    if f.get("scope", "all") != "all":
        parts.append(f"scope {f['scope']}")
    return "[dim]" + escape(", ".join(parts)) + "[/]"


def render_overview(data: dict, console: Console) -> None:
    t = data["totals"]
    console.print(_filters_line(data))
    summary = _table("Overview", [("Metric", "left"), ("Value", "right")])
    rows = [
        ("Cost", _money(t["cost"])),
        ("Sessions", _num(t["sessions"])),
        ("Turns", _num(t["turns"])),
        ("Requests", _num(t["requests"])),
        ("Input tokens", _num(t["input_tokens"])),
        ("Cache read tokens", _num(t["cache_read_tokens"])),
        ("Cache write tokens", _num(t["cache_write_tokens"])),
        ("Output tokens", _num(t["output_tokens"])),
        ("Cache hit", _pct(t["cache_hit_pct"])),
        ("Request errors", _num(t["errors"])),
        ("Turn errors", _num(t["turn_errors"])),
        ("Interruptions", _num(t["interruptions"])),
    ]
    for name, value in rows:
        summary.add_row(name, value)
    console.print(summary)
    models = _table(
        "Top models",
        [("Model", "left"), ("Requests", "right"), ("Cost", "right"), ("Cache hit", "right")],
    )
    for m in data["top_models"]:
        models.add_row(m["model"], _num(m["requests"]), _money(m["cost"]), _pct(m["cache_hit_pct"]))
    console.print(models)
    workspaces = _table(
        "Top workspaces",
        [("Workspace", "left"), ("Sessions", "right"), ("Requests", "right"), ("Cost", "right")],
    )
    for w in data["top_workspaces"]:
        workspaces.add_row(
            w["workspace"], _num(w["sessions"]), _num(w["requests"]), _money(w["cost"])
        )
    console.print(workspaces)


_TITLE_MAX = 60
_TITLE_MIN = 12


def render_sessions(data: dict, console: Console) -> None:
    console.print(_filters_line(data))
    shown = len(data["sessions"])
    columns = [
        ("Started (UTC)", "left"),
        ("Session", "left"),
        ("Turns", "right"),
        ("Requests", "right"),
        ("Cost", "right"),
        ("Errors", "right"),
        ("Duration", "right"),
    ]
    rows = [
        [
            (s["started_at"] or "—")[:16].replace("T", " "),
            s["session_id"],
            _num(s["turns"]),
            _num(s["requests"]),
            _money(s["cost"]),
            _num(s["errors"]),
            _duration(s["duration_s"]),
        ]
        for s in data["sessions"]
    ]
    # The title gets whatever width the fixed columns leave, or is dropped.
    probe = _table("", columns)
    for row in rows:
        probe.add_row(*row)
    # Each extra column costs its width plus a 3-character separator.
    spare = console.width - console.measure(probe).maximum - 3
    title_width = min(_TITLE_MAX, spare)
    title = f"Sessions ({shown} of {data['total']}, by {data['sort']})"
    if title_width >= _TITLE_MIN:
        columns.insert(2, ("Title", "left"))
        for row, s in zip(rows, data["sessions"], strict=True):
            row.insert(2, _one_line(s["title"], title_width))
    table = _table(title, columns)
    for row in rows:
        table.add_row(*row)
    console.print(table)


def render_session(data: dict, console: Console) -> None:
    s = data["session"]
    console.print(f"[bold]{escape(s['session_id'])}[/]  {escape(s['title'] or '')}")
    console.print(
        f"[dim]started {escape(s['started_at'] or '—')} · {_num(s['turns'])} turns · "
        f"{_num(s['requests'])} requests · {_money(s['cost'])} · cache hit "
        f"{_pct(s['cache_hit_pct'])} · {_duration(s['duration_s'])}[/]"
    )
    turns = _table(
        "Turns",
        [
            ("Turn", "right"),
            ("Status", "left"),
            ("Requests", "right"),
            ("Tools", "right"),
            ("Context", "right"),
            ("Cost", "right"),
            ("Errors", "right"),
        ],
    )
    for t in data["turns"]:
        turns.add_row(
            str(t["turn"]),
            t["status"],
            _num(t["requests"]),
            _num(t["tool_calls"]),
            _num(t["context_tokens"]),
            _money(t["cost"]),
            _num(t["errors"]),
        )
    console.print(turns)
    _print_tools(data["tools"], console, "Tools")
    if data["subagents"]:
        agents = _table(
            "Subagents",
            [
                ("Turn", "right"),
                ("Agent", "left"),
                ("Status", "left"),
                ("Requests", "right"),
                ("Cost", "right"),
            ],
        )
        for a in data["subagents"]:
            agents.add_row(
                str(a["turn"]), a["agent"], a["status"], _num(a["requests"]), _money(a["cost"])
            )
        console.print(agents)


def render_models(data: dict, console: Console) -> None:
    console.print(_filters_line(data))
    table = _table(
        "Models",
        [
            ("Model", "left"),
            ("Requests", "right"),
            ("Cost", "right"),
            ("Prompt tokens", "right"),
            ("Output tokens", "right"),
            ("Cache hit", "right"),
            ("p50", "right"),
            ("p95", "right"),
            ("Errors", "right"),
            ("Error %", "right"),
        ],
    )
    for m in data["models"]:
        table.add_row(
            m["model"],
            _num(m["requests"]),
            _money(m["cost"]),
            _num(m["input_tokens"] + m["cache_read_tokens"] + m["cache_write_tokens"]),
            _num(m["output_tokens"]),
            _pct(m["cache_hit_pct"]),
            _ms(m["p50_ms"]),
            _ms(m["p95_ms"]),
            _num(m["errors"]),
            _pct(m["error_pct"]),
        )
    console.print(table)


def _print_tools(rows: list[dict], console: Console, title: str) -> None:
    table = _table(
        title,
        [
            ("Tool", "left"),
            ("Calls", "right"),
            ("Errors", "right"),
            ("Error %", "right"),
            ("p50", "right"),
            ("p95", "right"),
            ("Mean size", "right"),
            ("p95 size", "right"),
        ],
    )
    for t in rows:
        table.add_row(
            t["name"],
            _num(t["calls"]),
            _num(t["errors"]),
            _pct(t["error_pct"]),
            _ms(t["p50_ms"]),
            _ms(t["p95_ms"]),
            _bytes(t["mean_bytes"]),
            _bytes(t["p95_bytes"]),
        )
    console.print(table)


def render_tools(data: dict, console: Console) -> None:
    console.print(_filters_line(data))
    _print_tools(data["tools"], console, "Tools")
    if data["top_errors"]:
        errors = _table(
            "Commonest tool errors", [("Tool", "left"), ("Count", "right"), ("Error", "wrap")]
        )
        for e in data["top_errors"]:
            errors.add_row(e["name"], _num(e["count"]), e["error"] or "")
        console.print(errors)


def render_subagents(data: dict, console: Console) -> None:
    console.print(_filters_line(data))
    agents = _table(
        "Subagents",
        [
            ("Agent", "left"),
            ("Launches", "right"),
            ("Errors", "right"),
            ("Requests", "right"),
            ("Cost", "right"),
            ("Cost / launch", "right"),
        ],
    )
    for a in data["agents"]:
        agents.add_row(
            a["agent"],
            _num(a["launches"]),
            _num(a["errors"]),
            _num(a["requests"]),
            _money(a["cost"]),
            _money(a["cost_per_launch"]),
        )
    console.print(agents)
    skills = _table("Skills", [("Skill", "left"), ("Loads", "right"), ("Sessions", "right")])
    for s in data["skills"]:
        skills.add_row(s["skill"], _num(s["loads"]), _num(s["sessions"]))
    console.print(skills)


def render_errors(data: dict, console: Console) -> None:
    console.print(_filters_line(data))
    t = data["totals"]
    console.print(
        f"Request errors {_num(t['request_errors'])} · interrupted requests "
        f"{_num(t['interrupted_requests'])} · no-usage requests {_num(t['no_usage_requests'])} · "
        f"turn errors {_num(t['turn_errors'])} · subagent errors {_num(t['subagent_errors'])}"
    )
    if data["top_messages"]:
        table = _table("Request errors", [("Model", "left"), ("Count", "right"), ("Error", "wrap")])
        for e in data["top_messages"]:
            table.add_row(e["model"], _num(e["count"]), (e["error"] or "")[:120])
        console.print(table)
    if data["turn_errors"]:
        table = _table(
            "Turn errors",
            [("When", "left"), ("Session", "left"), ("Turn", "right"), ("Error", "wrap")],
        )
        for e in data["turn_errors"]:
            table.add_row(
                (e["at"] or "—")[:16].replace("T", " "),
                e["session_id"],
                str(e["turn"]),
                (e["error"] or "")[:100],
            )
        console.print(table)
    if data["subagent_errors"]:
        table = _table(
            "Subagent errors",
            [("When", "left"), ("Session", "left"), ("Agent", "left"), ("Error", "wrap")],
        )
        for e in data["subagent_errors"]:
            table.add_row(
                (e["at"] or "—")[:16].replace("T", " "),
                e["session_id"],
                e["agent"],
                (e["error"] or "")[:100],
            )
        console.print(table)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _views():
    from archie_orchestrator import insights

    return insights


class _LazyView:
    """Resolve an insights function at call time so importing the CLI stays cheap."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __call__(self, *args, **kwargs):
        return getattr(_views(), self.name)(*args, **kwargs)


@click.group(name="stats", invoke_without_command=True)
@_filter_options
@click.pass_context
def stats(ctx: click.Context, **kwargs):
    """Usage insights from the local metrics index (default: overview)."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(overview_cmd, **kwargs)


@stats.command(name="overview")
@_filter_options
@_run(_LazyView("overview"), render_overview)
def overview_cmd():
    """Totals, top models and top workspaces."""


@stats.command(name="sessions")
@_filter_options
@click.option(
    "--sort",
    type=click.Choice(["started", "cost", "turns", "requests", "errors", "duration"]),
    default="started",
    show_default=True,
)
@click.option("--limit", type=click.IntRange(1, 200), default=50, show_default=True)
@_run(_LazyView("sessions"), render_sessions)
def sessions_cmd(sort: str, limit: int):
    """Sessions started in range."""
    return {"sort": sort, "limit": limit}


@stats.command(name="session")
@click.argument("session_id")
@click.option("--json", "as_json", is_flag=True, help="Print JSON instead of tables.")
def session_cmd(session_id: str, as_json: bool):
    """Turns, tools and subagents for one session."""
    from archie_orchestrator import insights

    as_json = _group_options(("as_json",)).get("as_json", as_json)
    conn = _open_index()
    try:
        data = insights.session_detail(conn, session_id)
    except KeyError:
        raise click.ClickException(f"Session not found: {session_id}") from None
    finally:
        conn.close()
    if as_json:
        click.echo(json.dumps(data, indent=2))
    else:
        render_session(data, _console())


@stats.command(name="models")
@_filter_options
@_run(_LazyView("models"), render_models)
def models_cmd():
    """Cost, cache hit, latency and errors per model."""


@stats.command(name="tools")
@_filter_options
@_run(_LazyView("tools"), render_tools)
def tools_cmd():
    """Calls, errors, duration and result size per tool."""


@stats.command(name="subagents")
@_filter_options
@_run(_LazyView("subagents"), render_subagents)
def subagents_cmd():
    """Launches and cost per subagent type, and skill loads."""


@stats.command(name="errors")
@_filter_options
@_run(_LazyView("errors"), render_errors)
def errors_cmd():
    """Request errors, turn errors and interruptions."""

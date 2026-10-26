# 075 — Usage insights: metrics index, `archie stats` and web dashboard

Status: planning
Branch: build-it/075-usage-insights (base: improve-tool-strategy @ b074cff)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/075-usage-insights

## Objective

Archie's usage data lives in session JSONL logs, but the only queryable index
(`~/.nexus/metrics.db`) holds just `llm_request` rows, and nothing presents it. This build
makes `metrics.db` a complete, metadata-only index of every session (requests, turns, tool
calls, compactions, sessions) and adds two read-only surfaces over it: an `archie stats`
command and a web dashboard in the orchestrator with charts, tables and filters.

## Context

User request: improve observability and insights for a single user on one machine. Wants an
`archie stats` command and, preferably, a web interface with graphs, tables and filtering.
The metrics DB must cover every use case of both surfaces: nothing should need to query or
filter the JSONL logs. An agent skill for self-analysis is explicitly deferred (the user has
separate plans for it).

Codebase facts found during discovery:

- `orchestrator/src/archie_orchestrator/metrics.py`: `MetricsWriter` stores only
  `llm_request` events, schema v3 (`PRAGMA user_version`), archiving a stale-version DB to
  `metrics.db.legacy.<UTC>`. `reset_and_backfill()` rebuilds from logs; `archie
  migrate-sessions` calls it. Dropped `llm_request` fields today: `stop_reason`, `error`,
  `subagent_index`, `purpose`.
- Ingestion is fed by `proxy.py:proxy_stream`, which enqueues `llm_request` frames only while
  a client WebSocket is attached. Requests made while no client is attached are never
  indexed (observed: one missing request in `tillo-platform-01m5mgsthh`).
- `GET /metrics` and `GET /sessions/{id}/metrics` (`app.py:_query_metrics`) return totals and
  a per-model breakdown; no CLI code calls them.
- The orchestrator already serves a session log viewer (plan 064): `/ui/sessions` and
  `/ui/sessions/{id}` in `web.py`, Jinja templates in `templates/viewer/`, vendored
  nimble.css and htmx, a strict CSP (`VIEWER_CSP`), no build step.
- Local data: 303 logs, 199 MB; a full JSON scan takes ~0.7 s. Event counts: 37k tool calls,
  26k requests, 2.8k turns, 138 turn errors, 80 interruptions, 72 compactions.
- CONTRIBUTING invariants: `llm_request` is the only cost/token source; recorded cost is
  never repriced; child `scope` is the launching task's `tool_use_id`.

## Baseline

- Check commands: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`
- Setup: `uv sync`
- Setup leftovers: none
- Failing before this work: 39 tests (30 failed, 9 errors), all in
  `tests/test_ws_integration.py`, `tests/test_runner.py`, `tests/test_plugin_enabled.py`,
  `tests/test_native.py`, `plugins/datadog/tests/test_datadog_monitors.py`,
  `plugins/incident_io/tests/test_incident_io_reads.py`. Cause seen in one: the
  `archie_plugins` package is already installed from `/opt/archie/plugins` in this
  environment. List saved at `/tmp/075-baseline-fail.txt`. Ruff check and format are clean.
- Not included: `plans/060-spec-container-aws-profiles.md`, `plans/070-build-nexus-plugins.md`
  (untracked in the user's checkout, unrelated)

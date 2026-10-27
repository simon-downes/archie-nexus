# 075 — Usage insights: metrics index, `archie stats` and web dashboard

Status: building
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
  indexed (observed at review: the logs hold 5 more `llm_request` events than the DB).
- `GET /metrics` and `GET /sessions/{id}/metrics` (`app.py:_query_metrics`) return totals and
  a per-model breakdown; no CLI code calls them.
- The orchestrator already serves a session log viewer (plan 064): `/ui/sessions` and
  `/ui/sessions/{id}` in `web.py`, Jinja templates in `templates/viewer/`, vendored
  nimble.css and htmx, a strict CSP (`VIEWER_CSP`), no build step.
- Local data: 303 logs, 212 MB; a full JSON scan takes ~0.7 s. Event counts: 37k tool calls,
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

## Decisions

| #   | Decision | Chosen | Rejected | Why | Source |
|-----|----------|--------|----------|-----|--------|
| D1  | Which views ship | All seven: overview, sessions, session detail, models, tools, subagents and skills, errors; each in the web UI and as an `archie stats` subcommand | A subset (1–5) | User choice | user Q1 |
| D2  | Who uses it | One user, one machine, local SQLite | OpenTelemetry/Prometheus, central dashboards | User: "just for me on this machine" | user (pre-interview) |
| D3  | Agent skill over the data | Not built | A stats skill now | User has separate plans for a self-analysis skill | user (pre-interview) |
| D4  | Where ingestion reads from | The session JSONL logs, incrementally by byte offset per file (`MetricsIndex.sync`) | Keep the proxy WebSocket feed; feed both | The proxy only sees frames while a client is attached, so detached work is lost (one request already missing). The log is the source of truth and is append-only, so one code path covers live updates and backfill | self |
| D5  | When the index syncs | An orchestrator background task every 15 s, plus a sync at the start of every dashboard request and every `archie stats` run | Sync only on query; only background | Queries never show stale data; the background task keeps the per-request sync to a few changed files | self |
| D6  | The proxy metrics feed | Removed (`_METRICS_MARKERS` enqueue and `MetricsWriter`) | Keep as a second writer | Two writers of the same rows add a conflict path and no data | self |
| D7  | What the index stores | Metadata only: IDs, timestamps, counts, sizes, statuses, names, model keys, skill and agent names, truncated error text. The only content kept: the session title (D25, ≤120 chars of the first user message) and the first line of an error tool result (≤200 chars). No other message content, tool inputs or tool output | Full text; no content at all | Keeps `metrics.db` small; the transcript viewer already serves content. A title is what makes a session recognisable in a table | self |
| D8  | Schema change | Schema v4; a non-v4 DB is archived by the existing `.legacy.<UTC>` mechanism and rebuilt from logs on first sync. The schema check, archive and every sync run while holding an exclusive `fcntl.flock` on `metrics.db.lock`; `reset_and_backfill` holds it across archive and rebuild. Archiving checkpoints the WAL (`PRAGMA wal_checkpoint(TRUNCATE)`) and moves any `-wal`/`-shm` files alongside | In-place `ALTER TABLE` migration; archive only at orchestrator startup | The DB is a derived index; the archive path already exists. The CLI, the background loop and page requests can all sync, so one lock is the single owner of archive and writes | self |
| D9  | Log rewritten (e.g. by `migrate-sessions`) | If the file is shorter than the stored offset, or the SHA-256 of its first line changed, delete that session's rows and re-ingest from byte 0 | Ignore; full rebuild | Offsets are meaningless after a rewrite; per-session reset is cheap | self |
| D10 | Log file deleted | Keep its rows | Delete them | Deleting a log shouldn't erase cost history; the index is still rebuildable from the logs that remain | self |
| D11 | Partial last line | Consume only through the last `\n`; the rest is read next sync | Parse it | The agent may be mid-append | self |
| D12 | Event timestamps | Only `llm_request`, `session_started` and `compaction_start` carry `sent_at`. Tool calls take their request's `sent_at` (the request always precedes its tool calls: 37,019/37,019 observed); turns take the first and last request times of that turn (any scope); a turn or compaction with no timestamp falls back to the session's `started_at` in queries | Add timestamps to events | Changing `shared/events.py` is a cross-package wire change; derivation covers every view, and the fallback keeps the 4 request-less error turns visible under a date filter | self |
| D13 | Subagent turn attribution | Child requests carry the parent's `turn` (6,291/6,291 observed), so a turn's cost is the sum of every request in the session with that turn | Walk the scope chain | Matches the data; `scope_inclusive_costs` remains the per-scope rollup | self |
| D14 | Where queries live | New `orchestrator/src/archie_orchestrator/insights.py`: pure functions over a `sqlite3.Connection` returning JSON-serialisable dicts; both the web routes and the CLI call them | Separate SQL in CLI and web; HTTP API for the CLI | One implementation can't drift. The CLI already depends on `archie-orchestrator` (`migrate-sessions` imports it) and must work when the orchestrator isn't running | self |
| D15 | Common filters | `since`, `until` (UTC dates, `until` exclusive), `workspace` (exact), `model` (exact `model_key`), `scope` (`all` / `root` / `subagent`). `since` accepts `Nd` (today and the N−1 previous UTC days), `all` or `YYYY-MM-DD`; default `30d` | Free-text query like the log viewer | Matches the stated "filtering" with fields the data has; one parser serves both surfaces | self |
| D16 | What each filter applies to | Dates: requests and tool calls by `timestamp`; turns, turn errors, interruptions and subagent runs by the turn's effective time (D12); compactions by effective time; the session list and the overview session count by session `started_at`. Workspace: every table via `session_id`. Model: requests and tool calls (via their request). Scope: requests and tool calls; turn and session counts ignore it | Apply everywhere; count sessions active in range | Turns and sessions have no single model or scope. "Sessions started in range" matches the session list, so the two numbers agree | self |
| D17 | Percentiles | Nearest-rank p50/p95 computed in Python from fetched values | SQLite extension | SQLite has no percentile function; at ~37k tool calls a fetch is cheap | self |
| D18 | Cache hit rate | `cache_read / (input + cache_read + cache_write)` | Over all tokens incl. output | Fraction of prompt tokens served from cache, the number that drives cost | self |
| D19 | Web pages | Server-rendered Jinja pages under `/ui/insights…`, extending `templates/viewer/base.html`, with the viewer CSP. Filter bar is a plain GET form; sorting is `sort=` links in table headers | SPA; htmx partials | Same stack as plan 064; full-page GET keeps every view linkable and testable | self |
| D20 | Charts | Vendor Chart.js 4.5.1 UMD (MIT) under `static/vendor/`; `static/insights/insights.js` reads chart data from `<script type="application/json">` blocks | Server-side SVG; CDN | User asked for graphs; tooltips and stacked bars need a library. Vendored for offline use like htmx. JSON data blocks are not executed, so `script-src 'self'` holds | self |
| D21 | CLI output | `rich` tables by default; `--json` prints the query function's dict | Plain text | `rich` is already a CLI dependency; `--json` makes output scriptable | self |
| D22 | Row caps | Sessions list 200 rows ("Showing 200 of N"), top error lists 20, tool table unlimited | Pagination | Same cap and wording as the log viewer (`MAX_ROWS`) | self |
| D23 | Invalid web filter values | Ignore that filter and show a notice naming it | HTTP 400 | Same behaviour as the log viewer's bad dates | self |
| D24 | Existing `GET /metrics` API | Keep; `requests` keeps its v3 columns so `_query_metrics` works unchanged | Remove | No caller found, but it's a documented route; keeping it costs nothing | self |
| D25 | Session title | First line of the first root `user_message`, ≤120 characters | No title | Sessions are unrecognisable by ID alone; one line is metadata-sized | self |
| D26 | Status vocabulary | Request "errors" = `status='error'`; `interrupted` and `no_usage` are counted and shown separately; error rate = errors / all requests | Count all non-completed as errors | 585 `no_usage` rows are mostly stream-accounting gaps, not failures; folding them in would bury real errors | self |
| D27 | Tool `duration_ms = 0` | Stored as NULL (not recorded); percentiles use non-NULL values | Keep 0 | `ToolResult.duration_ms` defaults to 0 and 3,842 older results never recorded it; zeros would skew p50 down | self |
| D28 | Duplicate event IDs within a log | First occurrence wins (`INSERT OR IGNORE`); a duplicate `llm_request` whose material columns differ is logged at error level (current `_process_batch` behaviour) | Raise `EventIdConflict` and skip the file | The tailer decodes lines itself, bypassing `SessionLog`'s conflict check; keeping the first wins keeps the rest of the session indexed | self |
| D29 | Subagent turn outcomes | Child `turn_complete`/`turn_error`/`turn_interrupted` set `status` and `error` on the `subagents` row (`scope`, `subagent_index`); `turns` stays root-only | Store child turns in `turns` | A child run is one turn; the errors view lists root turn errors (116) and subagent errors (22) separately | self |
| D30 | Page request during a running sync | Try the lock without blocking; if held, skip the sync and render from the current index with the notice "Index is updating; figures may be incomplete"; if `metrics.db` is missing or not v4, render the page's empty state with that notice. The CLI blocks on the lock, printing "Waiting for index sync…" to stderr after 1 s | Block page requests | A cold rebuild takes seconds to tens of seconds; the page should not hang | self |

## Requirements

**Index**

- R1 MUST index every `*.jsonl` under `<home_dir()>/sessions` into `metrics.db` schema v4 tables `sessions`, `requests`, `turns`, `tool_calls`, `compactions`, `subagents` and `ingest_files` (Data model).
  - AC: a fixture log with a user message, two root requests, a `task` call launching two children with requests, a `skill` call, a failing `shell` call, a compaction and a `turn_error` produces the expected row in each table.
- R2 MUST make sync incremental and idempotent: a second sync with no file changes writes nothing; appended lines are added; a trailing partial line is deferred.
  - AC: sync twice → identical row counts; append one request → `requests` +1; a line without `\n` is not indexed until completed.
- R3 MUST re-ingest a session whose log was rewritten (D9).
  - AC: truncate and rewrite a log with different events → the session's rows match the new content only.
- R4 MUST archive a non-v4 database and rebuild it from logs (D8).
  - AC: a v3 `metrics.db` is renamed to `metrics.db.legacy.*` and the new DB holds rows from the fixture logs.
- R5 MUST skip invalid lines and unreadable files without stopping the sync, recording the count/error in `ingest_files`.
  - AC: a log with one invalid JSON line indexes the valid events and `ingest_files.invalid_lines = 1`.
- R6 MUST keep request costs as recorded (CONTRIBUTING invariant): `SUM(cost_usd)` per session equals `session_accounting(path)["total_cost"]`.
  - AC: equality holds for the fixture log.
- R7 MUST keep indexed data metadata-only (D7).
  - AC: after indexing a log whose assistant messages, tool inputs and tool results contain a marker string (and whose second user message does too), the marker appears in no column except `tool_calls.error_summary` (≤200 chars, error results only); `sessions.title` holds only the first user message's first line, ≤120 chars.
- R8 MUST sync in the background in the orchestrator (D5) and no longer enqueue from the proxy (D6).
  - AC: the lifespan starts a sync task; `proxy.py` has no reference to the metrics queue; a request appended while no client is attached is indexed by the next sync.
- R9 MUST keep `archie migrate-sessions` rebuilding the index: `reset_and_backfill(db_path, paths)` archives the existing DB, syncs exactly those paths in strict mode, and raises on any file or SQLite error so the command reports failure.
  - AC: after `reset_and_backfill`, the old DB exists as `metrics.db.legacy.*` and the new DB's `requests` match the given logs; with a sync step patched to raise `sqlite3.OperationalError`, `reset_and_backfill` raises it; `archie migrate-sessions` on that failure exits 1 and does not print "rebuilt metrics".
- R10 MUST rebuild the local data (303 logs, 212 MB) cold in under 30 s, and run a no-change sync in under 0.5 s.
  - AC: with `ARCHIE_HOME_DIR=/tmp/075-home` holding a copy of `~/.nexus/sessions`, a script timing `MetricsIndex(...).sync()` in-process with no `metrics.db` (cold, < 30 s) and again (warm, < 0.5 s) meets the bounds; timings recorded in M1's Outcome.

**Queries (shared by CLI and web)**

- R11 MUST implement the filters in D15/D16 and reject an invalid value with `InsightsError` naming it.
  - AC: `parse_filters(since="7d")` on 2026-10-26 gives since 2026-10-20; `since="2026-13-01"` raises naming it; `scope="root"` excludes child requests from totals.
- R12 MUST return, for **overview**: total cost, sessions, turns, requests, input/output/cache read/cache write tokens, cache hit % (D18), request errors, turn errors, interruptions; daily cost per model (top 5 models, rest as `other`); top 10 workspaces and models by cost.
  - AC: fixture totals match hand-computed values; daily series has one entry per UTC day from the first to the last day with requests, zero-filled (revised, M3).
- R13 MUST return, for **sessions**: per session started, workspace, title, turns, requests, cost, tokens, request + turn error count, duration (started → last activity); sortable by `started` (default, desc), `cost`, `turns`, `requests`, `errors`, `duration`; capped at 200 with the total count.
  - AC: `sort="cost"` orders by cost desc; an unknown sort key raises `InsightsError`.
- R14 MUST return, for **session detail**: the session row; per turn: status, requests, cost, max root `context_tokens`, tool calls, errors; per request (sequence order): timestamp, scope, model, context tokens, cost; tools used in the session; subagents with agent, requests and cost.
  - AC: the fixture's turn cost includes its children's requests (D13); an unknown session raises `KeyError`.
- R15 MUST return, for **models**: per model requests, cost, tokens, cache hit %, p50/p95 `duration_ms` of completed requests, error rate.
  - AC: p50/p95 match nearest-rank values for a known set of durations.
- R16 MUST return, for **tools**: per tool name calls, errors, error %, p50/p95 duration, mean and p95 result bytes; plus the top 20 `(tool, error_summary)` pairs by count.
  - AC: a failing `shell` call appears in both outputs.
- R17 MUST return, for **subagents and skills**: per agent name launches, requests, cost and cost per launch; per skill name loads and sessions.
  - AC: a `task` call with agents `default` and `research` yields two agent rows with their children's costs; `skill` with `name: build-it` counts one load.
- R18 MUST return, for **errors**: request errors (D26) by model with the top 20 error messages; counts of `interrupted` and `no_usage` requests; root turn errors (session, turn, message) newest first, capped at 200; subagent errors (session, agent, message) newest first, capped at 200 (D29); and daily counts of request errors, turn errors and interruptions.
  - AC: the fixture's root `turn_error`, a child `turn_error` and an `error` request each appear in their list; a `no_usage` request is not counted as an error.

**CLI**

- R19 MUST add `archie stats` with subcommands `overview` (default), `sessions`, `session <id>`, `models`, `tools`, `subagents`, `errors`; options `--since`, `--until`, `--workspace`, `--model`, `--scope`, `--json`; `sessions` adds `--sort` and `--limit`. It syncs the index first and works without the orchestrator running.
  - AC (CliRunner, temp `ARCHIE_HOME_DIR`): `archie stats --json` prints the overview dict; `archie stats tools` prints a table containing `shell`; `--since bad` exits 2 naming the value; `stats session nope-01k0000000` exits 1 with "Session not found".

**Web**

- R20 MUST serve `GET /ui/insights` (overview), `/ui/insights/sessions`, `/ui/insights/sessions/{id}`, `/ui/insights/models`, `/ui/insights/tools`, `/ui/insights/subagents`, `/ui/insights/errors` as HTML with the shared filter bar (D15) and the viewer CSP, each syncing the index first.
  - AC: each returns 200 with `Content-Security-Policy` equal to `VIEWER_CSP`; the filter form's values round-trip in the query string; every page links to the other six views.
- R21 MUST show invalid filter values as a notice and ignore them (D23); unknown or malformed session IDs return 404.
  - AC: `?since=2026-13-01` → 200 with a notice naming it; `/ui/insights/sessions/.hidden` → 404.
- R22 MUST link sessions to the existing transcript (`/ui/sessions/{id}`) and to their insights detail page.
  - AC: a session row contains both links.
- R23 MUST render charts: overview daily cost stacked by model; session detail cost per turn and context tokens per request; errors daily counts; models cost per model. Chart data is embedded as JSON in `<script type="application/json">` and drawn by `static/insights/insights.js` with vendored Chart.js.
  - AC: the overview page contains a `<script type="application/json" id="chart-daily-cost">` whose JSON equals the overview query's daily series; templates contain no executable inline script and no `style=`.

**Docs**

- R24 MUST update `docs/architecture.md` (metrics section, routes table, ownership), `README.md` and `docs/event-spec.md` where they describe the metrics index.
  - AC: `rg -n "/ui/insights|archie stats|ingest_files" docs README.md` finds the new content; no statement that the orchestrator writes "only `llm_request` records" remains.

Out of scope: an agent skill (D3), authentication, OTel/Prometheus export, CSV export, changes to event schemas, editing or deleting logs, alerting, outcome/quality metrics, time-to-first-token (not recorded).

## Design

### Overview

`metrics.py` becomes `MetricsIndex`: it tails each session log from a stored byte offset, decodes canonical events, and folds them into seven metadata tables in one transaction per file. `insights.py` turns filters into SQL and returns plain dicts. `archie stats` and the `/ui/insights` pages both call `MetricsIndex.sync()` and then the same `insights` function, rendering it with `rich` or Jinja + Chart.js.

### Stack

| Dependency | New / reused | Why |
|---|---|---|
| sqlite3 (stdlib), Starlette, Jinja2 | reused | existing index and web stack |
| `rich`, `click` | reused (CLI deps) | tables and the command group |
| Chart.js 4.5.1 UMD, MIT | new vendored file `static/vendor/chart.umd.min.js` (+ licence in `static/vendor/LICENSES.md`) | D20 |

### Code structure

- `orchestrator/src/archie_orchestrator/metrics.py` (rewritten): `SCHEMA_VERSION = 4`, `MetricsIndex(db_path, sessions_dir)` with `sync(paths=None, *, strict=False, wait=True) -> SyncStats | None` (returns None when `wait=False` and the lock is held), `connect() -> sqlite3.Connection` (WAL, `busy_timeout=5000`, `row_factory=Row`); `reset_and_backfill(db_path, session_log_paths)` kept (archive then sync of those paths); `MetricsWriter` and its queue removed. Per-event fold functions are private.
- `orchestrator/src/archie_orchestrator/insights.py` (new): `InsightsError(ValueError)`, `Filters` dataclass, `parse_filters(since, until, workspace, model, scope, today=None)`, `overview`, `sessions`, `session_detail`, `models`, `tools`, `subagents`, `errors` — each `(conn, filters, …) -> dict`; `percentile(values, p)`.
- `orchestrator/src/archie_orchestrator/insights_web.py` (new): route handlers; reuses `web.templates`, `web._viewer_response`, `web.session_log_path`'s ID pattern (`web._SESSION_ID`).
- `orchestrator/src/archie_orchestrator/app.py`: lifespan runs `_metrics_sync_loop` (every `METRICS_SYNC_INTERVAL = 15.0` s, in a thread) instead of `MetricsWriter.run`; register the seven routes.
- `orchestrator/src/archie_orchestrator/proxy.py`: remove `_METRICS_MARKERS` and the enqueue.
- Templates (new): `templates/insights/_filters.html`, `_nav.html`, `overview.html`, `sessions.html`, `session.html`, `models.html`, `tools.html`, `subagents.html`, `errors.html`. `templates/viewer/base.html`: add an "Insights" nav link.
- Static (new): `static/vendor/chart.umd.min.js`, `static/insights/insights.js`, `static/insights/insights.css`.
- `cli/src/archie_cli/stats.py` (new): the `stats` click group and rich renderers; registered in `cli/src/archie_cli/cli.py` with `main.add_command`.
- Tests: `tests/test_orchestrator_metrics.py` (rewritten for `MetricsIndex`), `tests/test_insights.py`, `tests/test_cli_stats.py`, `tests/test_web_insights.py` (new); update `tests/test_proxy_metrics_integration.py` (drop enqueue tests, keep forwarding test) and `tests/test_canonical_session_events.py::test_canonical_metrics_are_idempotent`. Fixtures: extend `SessionLogWriter` in `tests/conftest.py` with `turn_complete`, `turn_error`, `turn_interrupted`, `compaction_*` helpers and `llm(status=, error=, sent_at=, duration_ms=, context_tokens=)` parameters.

### Data model (`metrics.db`, `PRAGMA user_version = 4`)

Lifecycle: derived; rebuilt from logs whenever archived. Timestamps are ISO-8601 UTC strings as recorded.

- `ingest_files`: `session_id TEXT PK`, `offset INT NOT NULL`, `size INT NOT NULL`, `mtime_ns INT NOT NULL`, `head_sha TEXT NOT NULL` (SHA-256 of the first line), `invalid_lines INT NOT NULL DEFAULT 0`, `error TEXT NULL`. A file whose `(size, mtime_ns)` equal the row is skipped without opening.
- `sessions`: `session_id TEXT PK`, `workspace TEXT NOT NULL` (`split_id`), `started_at TEXT NULL` (`session_started.sent_at`, else the ULID decode via `session_index.session_start`), `initial_model TEXT NULL`, `title TEXT NULL` (D25), `last_activity_at TEXT NULL` (max request `sent_at + duration_ms`).
- `requests`: existing v3 columns, plus `stop_reason TEXT NULL`, `error TEXT NULL` (≤500 chars), `subagent_index INT NULL`, `purpose TEXT NOT NULL`. `UNIQUE(session_id, event_id)`.
- `turns`: `session_id`, `turn INT`, `status TEXT NOT NULL` (`open` | `completed` | `error` | `interrupted`), `stop_reason TEXT NULL`, `error TEXT NULL` (≤500), `started_at TEXT NULL`, `ended_at TEXT NULL`. PK `(session_id, turn)`. Root scope only; created by the root `user_message`, or by the first root request of a turn with no user message.
- `tool_calls`: `session_id`, `tool_use_id`, `event_id`, `turn`, `iteration`, `scope NULL`, `subagent_index NULL`, `request_id`, `timestamp NULL`, `model_key NULL` (from the request), `name`, `skill TEXT NULL` (input `name` when `name = 'skill'`), `is_error INT NULL`, `duration_ms INT NULL`, `result_bytes INT NULL`, `result_lines INT NULL`, `error_summary TEXT NULL` (first non-empty line of an error result, ≤200). `UNIQUE(session_id, tool_use_id)`; `tool_result` updates the row.
- `compactions`: `session_id`, `request_id`, `turn`, `scope NULL`, `reason`, `model_key NULL`, `timestamp NULL`, `status TEXT NULL`, `tokens_before INT NULL`, `tokens_after INT NULL`, `cost_saved_usd REAL NULL`, `duration_ms INT NULL`, `error TEXT NULL` (≤500). `UNIQUE(session_id, request_id)`; `compaction_start` inserts, `compaction_complete` upserts. Legacy `context_compacted` inserts a `success` row.
- `subagents`: `session_id`, `scope` (the task `tool_use_id`), `subagent_index INT`, `agent TEXT NOT NULL`, `parent_scope TEXT NULL`, `turn INT`, `status TEXT NOT NULL DEFAULT 'open'`, `error TEXT NULL` (≤500) (D29). PK `(session_id, scope, subagent_index)`. One row per entry of the `task` input `tasks` (a list, or a JSON string of one); entries without a string `agent` record `"unknown"`.
- Indexes: `requests(timestamp)`, `requests(session_id)`, `requests(model_key)`, `tool_calls(name)`, `tool_calls(timestamp)`, `sessions(started_at)`.

### Data flow and error handling

- **Sync:** acquire `metrics.db.lock` (D8, D30) → ensure schema (archive non-v4) → list `*.jsonl` in the sessions dir, or use the explicit path list from `reset_and_backfill` → per file: stat; skip if unchanged; `BEGIN IMMEDIATE`; read `ingest_files`; rewrite check (D9) → delete the session's rows in every table; read bytes from `offset` to the last `\n`; decode each line with `decode_event(..., persisted=True)`; fold; update `ingest_files`; `COMMIT`.
- Invalid line → count in `invalid_lines`, log at debug, continue. Undecodable bytes / `OSError` → set `ingest_files.error`, log a warning, roll back that file, continue with the next. `sqlite3.Error` → log a warning, abandon this sync (the next one retries). In strict mode (`reset_and_backfill`), file and SQLite errors raise instead; invalid lines are still counted, not raised, and conflicting duplicates are still logged per D28.
- Duplicate event IDs: D28.
- `task` call whose input has no `tasks` list (or JSON-string list) → no `subagents` rows; children's requests still count under their scope.
- **Query:** web handler → `asyncio.to_thread(sync)` → parse filters (notices for invalid) → `asyncio.to_thread(query)` → render. A sync failure on a page request is logged and the page renders from the existing index with a notice "Index sync failed: <reason>".

### Non-functional

- Performance: R10 numbers. Each file is one transaction, so a cold build commits per session.
- Security: all filter values are bound SQL parameters; sort keys come from a whitelist mapping to fixed `ORDER BY` fragments; session IDs validated with `web._SESSION_ID`; Jinja autoescape; viewer CSP; the index holds no content (R7).

## Milestones

### [x] M1 — Log-tailing index and `archie stats` overview (tracer bullet)

- **Approach:** Rewrite `metrics.py` as `MetricsIndex` with the full v4 schema but folding only `session_started`, `user_message` (title), and `llm_request` (sessions + requests). Keep `reset_and_backfill` as archive + sync. Replace the lifespan writer task with `_metrics_sync_loop`; remove the proxy enqueue. Add `insights.parse_filters` and `insights.overview` (totals, daily cost, top workspaces/models; turn and error counts from `requests` only until M2). Add `cli/stats.py` with `overview` only, `--json` and the filter options. Rewrite `tests/test_orchestrator_metrics.py`; update the proxy and canonical-event tests. ⚠️ `tests/test_orchestrator_metrics_api.py` seeds its own v3 table and calls `_query_metrics`; it must still pass.
- **Test seam:** `MetricsIndex.sync()` against `session_logs` fixture logs; `insights.overview()`; `archie stats` via `CliRunner`.
- **Edge cases:** partial last line → deferred; rewritten log → reset; v3 DB → archived; locked DB → warning, no crash.
- **Deliverable:** `archie stats` prints cost, requests, tokens, cache hit % and per-model cost for the last 30 days from an index built from the logs.
- **Verify:** `uv run pytest tests/test_orchestrator_metrics.py tests/test_insights.py tests/test_cli_stats.py tests/test_orchestrator_metrics_api.py tests/test_proxy_metrics_integration.py tests/test_canonical_session_events.py tests/test_session_migration.py` passes (R9's CLI exit-1 AC lives in `tests/test_session_migration.py`); with `ARCHIE_HOME_DIR=/tmp/075-home` (copy of `~/.nexus/sessions`), `archie stats --since all --json` total cost is within $0.01 of the sum of `session_accounting(path)["total_cost"]` over the copied logs, and the R10 timings meet their bounds.
- **Outcome:** `MetricsIndex` (full v4 schema; all event types folded already, since the fold code was small enough to land with the schema) replaces `MetricsWriter`; the lifespan runs `_metrics_sync_loop`; the proxy no longer inspects frames. `insights.parse_filters`/`overview` and `archie stats` (all subcommands present; M2 tests them). Verify: the listed pytest files pass (24 + 18 new tests, plus migration/API/proxy/canonical suites); full suite shows no new failures against the 39-test baseline; ruff check/format clean. Real data copy (303 logs, 212 MB): cold sync 1.65 s, warm 0.007 s (R10); `archie stats --since all --json` cost 1068.407323 vs `session_accounting` sum 1068.4073 (diff 0.0).

### [x] M2 — Full index and the remaining `archie stats` views

- **Approach:** Fold `tool_call`/`tool_result`, `turn_complete`/`turn_error`/`turn_interrupted`, `compaction_*`/`context_compacted` and `task` launches into their tables. Add `insights.sessions`, `session_detail`, `models`, `tools`, `subagents`, `errors` and `percentile`; extend `overview` with turn counts, turn errors and interruptions. Add the six CLI subcommands with `rich` tables. Extend `SessionLogWriter` in `tests/conftest.py`.
- **Test seam:** `insights.*` over a synced fixture DB; CLI via `CliRunner`.
- **Edge cases:** `task` input `tasks` as a JSON string → parsed; tool result with no matching call → ignored; tool call whose request isn't indexed → `timestamp`/`model_key` NULL.
- **Deliverable:** all seven `archie stats` views work against real data.
- **Verify:** `uv run pytest tests/test_orchestrator_metrics.py tests/test_insights.py tests/test_cli_stats.py` passes (covering R1, R5–R7, R11–R19); on the data copy each subcommand runs and `stats tools` lists `shell` and `read`.
- **Outcome:** Added 31 insights tests (filters, all seven views, percentiles, date fallback, 200-row cap) and 15 more CLI tests (every subcommand in table and JSON form, literal `[exit: N]`, bad sort). CLI rendering: cells are escaped (Rich would otherwise eat `[exit: 2]`); error text wraps; the sessions title column takes only the width left over and is dropped below 12 characters. Verify: `uv run pytest tests/test_orchestrator_metrics.py tests/test_insights.py tests/test_cli_stats.py` passes (75); full suite 2161 passed with only the 39 baseline failures; ruff clean. Real data copy: every subcommand exits 0; `stats tools --since all` lists `shell` (5,886 calls) and `read` (10.3k).

### [x] M3 — Web dashboard pages with tables and filters

- **Approach:** `insights_web.py` handlers and seven templates extending `viewer/base.html`; follow `web.session_list_page` for threading and `_viewer_response` for the CSP. Filter bar partial with since/until/workspace/model/scope selects (workspace and model options from the index). Sort links on the sessions table. Add the nav link. Update docs (R24).
- **Test seam:** ASGI client (`httpx.ASGITransport`) as in `tests/test_web_session_list.py`.
- **Deliverable:** all seven pages render tables from the index with working filters.
- **Verify:** `uv run pytest tests/test_web_insights.py tests/test_web_session_list.py` passes (R20–R22); `rg -n "/ui/insights|archie stats|ingest_files" docs README.md` finds the new content and `rg -n "only .llm_request. records" docs` finds nothing (R24).
- **Outcome:** `insights_web.py` with seven handlers, templates under `templates/insights/`, `static/insights/insights.css`, nav links in both base templates. The filter bar is a GET form: since preset, an optional `from` date that overrides it, until, workspace, model, scope. Chart JSON blocks also landed here (M4 draws them). Docs updated (architecture metrics section rewritten, routes, data flow, proxy; README stats usage; event-spec). Verify: `uv run pytest tests/test_web_insights.py tests/test_web_session_list.py` passes (24 + session-list tests); `rg -n "/ui/insights|archie stats|ingest_files" docs README.md` finds 15 lines; `rg "only .llm_request. records|metrics writer|MetricsWriter" docs README.md` finds nothing. Full suite 2186 passed, only the 39 baseline failures. All pages return 200 against the real data copy.

### [ ] M4 — Charts

- **Approach:** Vendor Chart.js 4.5.1 (`https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js`) and add its MIT licence to `static/vendor/LICENSES.md`. Embed each chart's series with Jinja's `tojson` filter in `<script type="application/json" id="chart-…">`; `insights.js` finds `canvas[data-chart]`, reads the matching JSON, and draws (stacked bar for daily cost, bar for per-turn cost and per-model cost, line for context tokens and daily errors).
- **Test seam:** ASGI client for the JSON blocks; browser evidence for drawing.
- **Deliverable:** the four chart sets in R23 render in a browser.
- **Verify:** `uv run pytest tests/test_web_insights.py -k chart` passes; headless browser screenshot of `/ui/insights` and a session detail page shows drawn charts and no CSP errors in the console.
- **Outcome:**

## Deviations

- M1: the fold code for every event type and all seven query functions and CLI subcommands landed in M1 instead of M2; M2 now adds their tests, rendering fixes and any corrections. No scope change.
- M3: the filter bar gained a `from` date input that overrides the `since` preset (a `<select>` can't take a custom date). D15 unchanged otherwise.
- M3: daily series (overview cost, errors) are zero-filled across the whole day span instead of listing only days with data, so line and bar charts don't hide quiet days. R12's AC revised.

## Risks

- Chart.js under `style-src 'self'`: Chart.js sets canvas sizes through the CSSOM, which CSP allows; confirm in M4's browser check.
- The orchestrator may not start in this container (Docker absent); browser evidence may need the app started with `list_sessions` unavailable. Plan 064 hit the same constraint.
- The first sync after upgrade rebuilds the index from all logs (R10 bounds it); the old DB is archived, not deleted.

## Review

- Plan review: 1 pass; 3 ❌, 9 ⚠️, 7 ℹ️. All accepted. R9 now names the carried-over behaviours and a strict `reset_and_backfill`; R7/D7 allow the title explicitly; strict mode added to the error design; M1 Verify compares against `session_accounting` sums; D16 defines date filtering for turns/compactions/sessions; D26–D30 added (status vocabulary, zero durations, duplicates, subagent outcomes, lock and page-during-sync); `subagents` gains status/error; missing `tasks` handled; sync takes explicit paths; stale counts in D12/D13/Context updated; R10 and M3 Verify name commands. Second pass: blockers confirmed resolved; 4 ⚠️, 3 ℹ️ accepted (migrate test file named, R10 timed in-process, WAL files moved on archive, empty state when schema not v4, lock held across `reset_and_backfill`, strict-mode line handling, D25 reordered).

## Outcome

PR: pending
CI: pending

## Follow-ups

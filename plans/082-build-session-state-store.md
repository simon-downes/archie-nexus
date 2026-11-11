# 082 — Session state store and session API

Status: planning
Branch: build-it/082-session-state-store (base: main @ b13e411)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/082-session-state-store

## Objective

The orchestrator knows sessions only as running Docker containers: `GET /api/sessions` shells
out to `docker ps` on every call (and every proxied request), returns no state, and forgets a
session once its container stops. Replace that with an orchestrator-owned session record in a
new `~/.nexus/archie.db`, kept current by a session tracker that observes every running
session, and serve it through richer `/api/sessions` routes and an SSE feed. The metrics index
moves into the same database as `metrics_*` tables that join to the session record.

## Context

Roadmap items 3 and 7 (brain: `projects/archie/nexus-roadmap/session-tracker.md`,
`state-store.md`), reshaped in discussion with the user on 2026-11-11 as the starting point
for the multi-session TUI (prototype on branch `prototype/tui-workbench`, not part of this
build). Agreed in that discussion:

- `metrics.db` becomes `archie.db`: an orchestrator datastore for session state now, and user
  auth tokens and other canonical data later. The existing metrics tables gain a `metrics_`
  prefix and stay rebuildable from the logs.
- One canonical `sessions` table. It is never rebuilt; a session's row stops changing once the
  session ends, so it is the historical record too. Metrics tables join to it. The metrics
  sync never writes to it.
- Session names and states are not written to the session logs; logs stay append-only events.
- One `name` field plus `name_fixed` (user-set). Historical sessions are imported once with
  `name` = the session id; the first prompt is not used as a title.
- One `status` field merging orchestrator lifecycle and agent turn state: `starting`, `idle`,
  `working`, `blocked`, `error`, `stopping`, then terminal `stopped`, `exited`, `failed`.
  079's `done` is never emitted by the agent and is not stored.
- `unseen` is kept on the server, set when a root turn ends, cleared by clients.
- `POST /api/sessions` returns 202 with `status: starting`; startup continues in the
  background. `DELETE` returns the record. `PATCH` sets `name` and clears `unseen`.
  `GET /api/sessions/{id}` is new. `GET /api/sessions/feed` is SSE: a snapshot on every
  connect, then one full record per change. `/status` proxying is removed.
- Origin/Host checks and TUI credentials are out of scope. The `~/.nexus` container mount
  stays as it is (removing it is a future task). `persona/prompts/environment.md` stops
  mentioning the database: agents have no reason to query it.
- TUI changes only where needed to keep existing behaviour; the TUI refactor to the
  prototype is later work.

Codebase facts (main @ b13e411):

- Routes: `orchestrator/src/archie_orchestrator/api.py` (JSON under `/api`), `pages.py`
  (pages), `proxy.py` (`resolve_session()` calls `docker.list_sessions()` on every
  `/status`, `/events` and WebSocket connect). `list_sessions()` runs `docker ps` plus one
  `docker port` per container, synchronously.
- `SessionDescriptor` (`shared/src/archie_shared/session/descriptor.py`) is
  `{session_id, container_name, port, raw_docker_status}`; `StatusPayload` is unused.
- `lifecycle.start_session()` validates the workspace, checks the image, generates the id,
  runs `docker run` and blocks in `wait_for_ready` (30 s native, 45 s Kiro). The backend is
  passed only as `ARCHIE_BACKEND=kiro` and is not recorded.
- `metrics.py`: `MetricsIndex` folds logs into seven tables at `user_version` 4, under a
  `flock` on `metrics.db.lock`; a version mismatch, and `reset_and_backfill()` (used by
  `archie migrate-sessions`), archive the whole file. Consumers: `insights.py`,
  `insights_web.py`, `api.py` (`_query_metrics`), `cli/.../stats.py`, `cli.py`
  (`migrate-sessions`).
- Agent: `session_status` is sent on connect, after the root `user_message` (native only),
  after each turn and after compaction (`agent/.../harness.py`, `kiro/harness.py`). It carries
  `idle | working | blocked` and the pending question. The session bus disconnects a client
  whose 1024-frame queue overflows.
- The live viewer (`web.py` `_live_events`) already subscribes to an agent's `/stream`
  directly; the tracker follows that pattern.
- CLI consumers of the session API: `cli.py` `start`, `ls`, `attach`, `stop`, `shell`,
  `_require_kiro_session`; the TUI's quit-and-terminate (`tui/app.py`, DELETE accepting
  200/404).

## Baseline

- Check commands: `env -u ARCHIE_BACKEND -u ARCHIE_SESSION_ID scripts/dev test`,
  `scripts/dev ruff check .`, `scripts/dev ruff format --check <touched files>`
- Setup: `scripts/dev sync`
- Setup leftovers: none (`.venv-archie/` is ignored)
- Failing before this work: none (4818 passed in 88 s; Ruff clean)
- Not included: the user's checkout is on `prototype/tui-workbench`; its untracked
  `.DS_Store`, `plans/060-spec-container-aws-profiles.md`, `plans/070-build-nexus-plugins.md`

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Name shown by insights and `archie stats` (the first-prompt title goes away) | `name` when `name_fixed`, else blank | always `name` (repeats the id); drop the column | Named sessions stand out; unnamed ones don't repeat the id already in the row | user Q1 |
| D2 | `POST /api/sessions` fields | `workspace`, `backend`, optional `name` | also `model` and `prompt` now | A starting model needs an agent change; a first prompt brings in item 5's caps and headless rules; no caller needs either until the TUI refactor | user Q2 |
| D3 | Wire type for a session | Reshape `SessionDescriptor` in `shared/.../session/descriptor.py` to the session record | a new type name next to it | One name for the record across API, feed and CLI; every consumer changes anyway because the old fields go | self |
| D4 | Where the store and tracker live | New `orchestrator/.../store.py` (database, migrations, `sessions` CRUD) and `tracker.py` (lifecycle, agent subscriptions, feed) | in `metrics.py` or `api.py` | `metrics.py` stays the rebuildable index; handlers stay thin, as `api.py` keeps today | self |
| D5 | Version tracks in one file | Canonical schema: `PRAGMA user_version` with ordered in-place migrations. Metrics schema: `meta` row `metrics_version`; a mismatch drops and recreates only `metrics_*` | whole-file archive (today) | Archiving the file would destroy canonical rows | self |
| D6 | Historical import | Canonical migration 1 creates `sessions` and inserts one `stopped` row per existing log; runs once under the `archie.db.lock` flock | import on every metrics sync | The metrics sync must never write `sessions` (user); history needs rows for the join | self |
| D7 | Imported timestamps and model | `started_at` = `session_started.sent_at`, else the id's ULID time, else the log's mtime; `last_activity_at` = `ended_at` = the log's mtime; `model_key` = the last root `llm_request` model, else `session_started.model_key`; `backend` = `kiro` when that model starts with `kiro/` | max request end time (today's `last_activity_at`) | The log's mtime is the last append, needs no event decoding, and works for logs with no requests | self |
| D8 | Logs with no session row | The metrics sync skips the log and records `no session record` in `metrics_ingest_files.error` | insert a row from the sync | Keeps "metrics never writes `sessions`" absolute; after the import every orchestrator-started session has a row before its log exists | self |
| D9 | How the tracker learns state | One read-only WebSocket per running session to the agent's `/stream`, like `web.py` `_live_events` | polling `/status`; reading logs | Push, already proven in the live viewer; carries `session_status` and turn events | self |
| D10 | Status rules from events | Root `user_message` → `working`; `session_status` sets model, question and `idle`/`working`/`blocked` (`done` maps to `idle`); after a root `turn_error`, the following `idle` becomes `error` until the next root `user_message`; root `turn_complete` and `turn_error` set `unseen` | trusting `session_status` alone | Kiro sends no `working` status at turn start; the agent re-sends `idle` straight after a failed turn, which would hide the failure | self |
| D11 | Write rate | Status, question, model, unseen and name changes are written and published at once; a change to `last_activity_at` alone is written and published at most every 10 s per session | write every event | Text deltas and tool events arrive many times a second; the feed would flood | self |
| D12 | Database access from async code | Synchronous `sqlite3` in `store.py`, one short-lived connection per call, always called through `asyncio.to_thread` | `aiosqlite` (new dependency); calls on the event loop | A metrics file commit can hold the write lock for up to the 5 s busy timeout; no new dependency | self |
| D13 | Startup order and reconcile | Migrate, then reconcile with one `docker ps`, then subscribe, then start the metrics loop. A running container makes its row live (`idle`, `ended_at` cleared), adopting it from its log if there is no row; a non-terminal row with no container becomes `exited` (`ended_at` = log mtime or now), or `failed` ("orchestrator restarted during startup") if it was `starting` | trusting stored status | Docker is the truth for whether a container exists; the import marks running sessions `stopped` and this revives them | self |
| D14 | Ending detection | When a subscription closes: if the status is `stopping` or `stopped`, do nothing; else check `docker inspect`; running → resubscribe with 1, 2, 5 s back-off; gone → `exited` | treat every close as an end | The agent closes slow clients (queue overflow) and restarts drop sockets | self |
| D15 | Startup split | `lifecycle.prepare_start()` validates the workspace and image and builds the id and command, synchronously inside the POST; `lifecycle.run_start()` runs and waits in the background task; `start_session()` remains their composition | validate in the background | Bad input keeps its synchronous 400; only the slow part moves | self |
| D16 | Docker access in the tracker | A small `Runtime` object (list running, prepare, run, stop, is running) with a Docker implementation; tests pass a fake | patching module functions | ~30 test files patch `list_sessions` today; one seam is easier to fake than many | self |
| D17 | Agent port for proxying | The tracker keeps `session_id → port` in memory from `run_start` and reconcile; `resolve_session` and the live viewer use it | `docker ps` per request (today) | Removes N+1 Docker calls from every proxied request (architecture finding 5 for these paths) | self |
| D18 | `PATCH` on ended sessions | Allowed for `name` and `unseen` | 409 | Names show in insights; renaming history is useful and touches no lifecycle state | self |
| D19 | `DELETE` response | Waits for `stop_container`, returns 200 with the `stopped` record; 409 for `starting`, `stopping` and ended sessions; on a Docker failure the previous status is restored and 500 returned | 202 with `stopping` | Keeps `archie stop` simple; the feed still shows `stopping` in between | self |
| D20 | Feed filter | `?include=ended` only changes the snapshot; every change event is forwarded, including a session's final terminal one | filter change events too | Clients drop a session on its terminal event; ended rows never change again, so nothing else leaks | self |
| D21 | `GET /api/sessions/{id}/events` for ended sessions | Served from the log with `SessionLog(path).read(after)`; `CursorNotFound` → 409 as the agent does | 404 | Makes the record and its history consistent after the container goes, at little cost | self |
| D22 | `archie start` waiting | Poll `GET /api/sessions/{id}` every 0.5 s while `starting`, up to 60 s, inside the existing 080 status indicator; `failed` prints the stored `error` | a `?wait` flag on POST | One contract for every client; the poll is cheap | self |
| D23 | Old `metrics.db` | Left untouched; nothing reads it; the docs say it can be deleted | delete it during migration | It's rebuildable and deleting user files unasked is irreversible | self |

## Requirements

Session record and store

- MUST keep session records in `<home>/archie.db`, table `sessions`, one row per session, with
  the fields in Design → Data model.
  - AC: after `POST /api/sessions`, `sqlite3 archie.db 'select status from sessions'` shows the
    row; `tests/test_session_store.py` round-trips every field.
- MUST import every existing log once, as a `stopped` row with `name` = session id, using D7's
  sources, and never import again.
  - AC: a test with three logs (with and without `session_started` and requests, one Kiro)
    asserts each imported column; a fourth log added after migration gets no row.
- MUST never write the `sessions` table from the metrics sync, `archie stats` or
  `archie migrate-sessions`.
  - AC: a test runs `MetricsIndex.sync()` and `reset_and_backfill()` and asserts the
    `sessions` rows are byte-identical before and after.

Tracker and status

- MUST move a created session through `starting` → `idle` when the agent is ready, or to
  `failed` with the start error stored in `error`.
  - AC: tracker tests with a fake runtime cover both, including a Kiro startup-error message.
- MUST derive `working`, `blocked`, `idle`, `error`, `unseen`, `model_key` and `question` from
  the agent stream per D10.
  - AC: tracker tests feed frame sequences (native turn; Kiro turn without a working status;
    failed turn followed by `idle`; pending question) and assert the record after each.
- MUST mark a session `exited` when its container goes away without a `DELETE`, and reconcile
  per D13 at startup.
  - AC: tracker tests: subscription close with container gone → `exited` with `ended_at`;
    close with container running → resubscribe; each D13 reconcile case.
- MUST NOT call Docker in `GET /api/sessions`, `GET /api/sessions/{id}`, or when resolving a
  session for `/events`, `/stream` or the live viewer.
  - AC: tests run those routes with a runtime whose Docker methods raise; they succeed.

API

- MUST serve `GET /api/sessions` (non-ended by default, `?include=ended` for all) and
  `GET /api/sessions/{id}` (404 for an unknown id) as `SessionDescriptor` JSON.
  - AC: API tests for both filters and the 404.
- MUST accept `POST /api/sessions` `{workspace, backend?, name?}`, keep today's 400s for a bad
  body, workspace or backend, return 400 for an invalid `name` (not a string, blank after
  trimming, over 80 characters), and otherwise return 202 with a `starting` record.
  - AC: API tests for each 400 and the 202; `name` given → `name_fixed: true`.
- MUST accept `PATCH /api/sessions/{id}` with `name` (string sets it and `name_fixed`; `null`
  resets it to the id and clears `name_fixed`) and `unseen: false`; anything else is 400;
  unknown id 404.
  - AC: API tests for each field, `null`, an unknown key, `unseen: true`, and 404.
- MUST stop a session with `DELETE /api/sessions/{id}` per D19.
  - AC: API tests: 200 with `stopped` and `ended_at`; 409 for `starting` and ended; 404; Docker
    failure → 500 and the previous status restored.
- MUST serve `GET /api/sessions/feed` as `text/event-stream`: a `snapshot` event
  (`{"sessions": [...]}`, same filter as the list) on connect, a `session` event with the full
  record on each change per D11 and D20, and a `: ping` comment every 15 s.
  - AC: feed tests read the snapshot, then a `session` event after a PATCH, and a terminal
    event for a session that ends while `include=ended` is off.
- MUST remove `GET /api/sessions/{id}/status`.
  - AC: the route test asserts it is absent (404).
- MUST serve `GET /api/sessions/{id}/events` from the log once a session has ended (D21).
  - AC: API test returns the log's events, honours `after`, 409 for an unknown cursor.

Metrics

- MUST store the metrics index in `archie.db` as `metrics_ingest_files`, `metrics_requests`,
  `metrics_turns`, `metrics_tool_calls`, `metrics_compactions`, `metrics_subagents`, joined to
  `sessions` for workspace, start time, last activity and name; the metrics `sessions` table
  and `initial_model` are gone.
  - AC: metrics and insights tests pass against the new tables; `sqlite_master` has no
    `requests` or old `sessions` metrics table.
- MUST rebuild only the `metrics_*` tables on a metrics version change and in
  `archie migrate-sessions` (option renamed `--db`).
  - AC: test bumps the metrics version and asserts `sessions` is unchanged and metrics refill;
    CLI test for `migrate-sessions --db`.
- MUST show the name in insights and `archie stats` only when `name_fixed` (D1).
  - AC: insights page and stats tests: a fixed name appears; an unfixed one doesn't.

CLI, web and prompt

- MUST keep `archie start`, `ls`, `attach`, `stop`, `shell` and the TUI's quit-and-terminate
  working against the new API: `start` waits per D22 and reports a `failed` start's `error`;
  `--detach` prints the session and container; `ls` shows id, workspace, status and the name
  when fixed; `stop` reports 404 and 409; the TUI accepts 200, 404 and 409 on terminate.
  - AC: updated `tests/test_cli_start_stop.py`, `test_cli_attach.py`, `test_cli_profiles.py`,
    `test_cli_shell.py` pass, plus a new failed-start test.
- MUST show status, name (when fixed) and last activity on the web sessions page (`/`) instead
  of the Docker status and port.
  - AC: `tests/test_web_console.py` asserts the new columns; browser screenshot in the PR.
- MUST remove the `metrics.db` mention from `persona/prompts/environment.md` and not mention
  `archie.db` there.
  - AC: `rg -n 'metrics\.db|archie\.db' persona/` finds nothing.
- MUST update `docs/architecture.md`, `docs/session-protocol-walkthrough.md`, `docs/kiro.md`,
  `docs/event-spec.md` and `README.md` where they describe the routes, startup, the descriptor
  or `metrics.db`.
  - AC: `rg -n 'metrics\.db|raw_docker_status|/status\b' docs README.md` shows only
    intentional mentions (the old file, the agent's own `/status`).

Out of scope: `model` and `prompt` on POST (D2); repo, worktree and branch detection and
automatic naming; Origin/Host checks and client credentials; the `~/.nexus` mount; the attach
TUI clearing `unseen`; the TUI sidebar; caps; deleting `metrics.db`.

## Design

### Overview

The orchestrator gains a session store (`archie.db`, table `sessions`) and a session tracker.
The tracker owns every write to `sessions`: it creates the row on `POST`, runs the container
start in the background, subscribes to each running agent's `/stream` and folds the events into
the row, and publishes every change to feed subscribers. API handlers read the store and call
the tracker; no request path calls Docker except start and stop. The metrics index moves into
the same file under `metrics_*` names and joins to `sessions`.

### Code structure

- `shared/src/archie_shared/session/descriptor.py`: `SessionDescriptor` reshaped (Data model);
  new `type SessionRecordStatus` and `TERMINAL_STATUSES`. `StatusPayload` is unchanged.
- `orchestrator/src/archie_orchestrator/store.py` (new): `db_path(home)`, `connect()` (WAL,
  busy timeout, as `metrics.connect` today), `migrate(db, sessions_dir)` with the ordered
  canonical migrations under `flock` on `archie.db.lock`, the import (`import_log()` reused by
  reconcile's adoption), and `SessionStore` with `insert`, `get`, `list(include_ended)`,
  `update(session_id, **fields) -> SessionDescriptor`.
- `orchestrator/src/archie_orchestrator/tracker.py` (new): `Runtime` (Docker
  implementation over `docker.py`/`lifecycle.py`), `SessionTracker` (`start()`, `close()`,
  `create()`, `stop()`, `patch()`, `port()`, `live_ids()`, `subscribe_feed()`), the per-session
  subscription task and the frame folding of D10/D11.
- `orchestrator/src/archie_orchestrator/docker.py`: `list_sessions()` returns a new internal
  `RunningContainer(session_id, container_name, port, docker_status)` instead of
  `SessionDescriptor`; `container_running()` becomes public.
- `orchestrator/src/archie_orchestrator/lifecycle.py`: `prepare_start()`, `run_start()`;
  `start_session()` composes them and returns `RunningContainer`; `stop_session()` unchanged.
- `orchestrator/src/archie_orchestrator/api.py`: session handlers use
  `request.app.state.tracker`; new `session_get`, `session_patch`, `sessions_feed`,
  `proxy_events` ended-session branch; `/status` route removed; `_query_metrics` reads
  `archie.db` `metrics_requests`. Route order puts `/sessions/feed` before
  `/sessions/{session_id}`.
- `orchestrator/src/archie_orchestrator/proxy.py`: `resolve_session()` asks the tracker for the
  port; `proxy_status` removed.
- `orchestrator/src/archie_orchestrator/app.py`: lifespan runs D13's order, stores the tracker
  on `app.state.tracker`, closes it on shutdown.
- `orchestrator/src/archie_orchestrator/metrics.py`, `insights.py`, `insights_web.py`,
  `web.py` (`sessions_page`, `_active_ids`, `_resolve_live`), `templates/sessions.html`,
  `templates/insights/sessions.html`, `templates/insights/session.html`.
- `cli/src/archie_cli/cli.py` (`start`, `ls`, `stop`, `migrate-sessions`, row formatting,
  `_require_kiro_session` removed), `cli/src/archie_cli/stats.py`, `cli/src/archie_cli/tui/app.py`
  (terminate status codes only).
- Tests: new `tests/test_session_store.py`, `tests/test_session_tracker.py`,
  `tests/test_sessions_api.py`, `tests/test_sessions_feed.py`; a shared fake runtime and store
  fixture in `tests/session_fakes.py` (new). Existing tests that patch `list_sessions` or
  query metrics tables are updated.

### Data model

`sessions` (canonical, migration 1):

| Column | Type | Notes |
|---|---|---|
| `session_id` | TEXT PK | |
| `workspace` | TEXT NOT NULL | `split_id` at creation |
| `backend` | TEXT NOT NULL | `native` \| `kiro` |
| `name` | TEXT NOT NULL | defaults to `session_id` |
| `name_fixed` | INTEGER NOT NULL | 0/1 |
| `status` | TEXT NOT NULL | `starting idle working blocked error stopping stopped exited failed` |
| `error` | TEXT NULL | start failure message |
| `question` | TEXT NULL | `PendingQuestion` JSON |
| `unseen` | INTEGER NOT NULL | 0/1 |
| `model_key` | TEXT NULL | null until the first `session_status` |
| `started_at` | TEXT NOT NULL | ISO 8601 UTC |
| `last_activity_at` | TEXT NULL | |
| `ended_at` | TEXT NULL | set with a terminal status |

Indexes: `status`, `started_at`. `meta(key TEXT PK, value TEXT)` holds `metrics_version`.
Metrics tables keep today's columns under `metrics_` names, minus the metrics `sessions` table;
`metrics_requests.session_id` etc. join to `sessions.session_id` (no foreign key).

`SessionDescriptor` (JSON): `session_id: str`, `workspace: str`, `backend: "native"|"kiro"`,
`name: str`, `name_fixed: bool`, `status: SessionRecordStatus`, `error: str|None`,
`question: PendingQuestion|None`, `unseen: bool`, `model_key: str|None`, `started_at: str`,
`last_activity_at: str|None`, `ended_at: str|None`, `container_name: str|None` (computed:
`container_name(session_id)` unless ended). All required fields first; optional ones default
to `None`/`False`.

Lifecycle: a row is inserted by `create()` or by reconcile adoption, changes only through the
tracker until a terminal status, and afterwards only through `PATCH` (`name`, `unseen`).

### Data flow

- Create: `POST` → `prepare_start` (400 on bad input) → insert `starting` → publish → 202.
  Background: `run_start` in a thread → port stored → subscribe → the agent's connect
  `session_status` sets `idle`. Failure → `failed`, `error`, `ended_at`.
- Turn: `user_message` → `working` (published); deltas and tool events touch
  `last_activity_at` (throttled); `turn_complete` → `unseen`; `session_status` → `idle`.
- Stop: `DELETE` → `stopping` (published) → `stop_session` in a thread → `stopped`,
  `ended_at`; the subscription's close is ignored because of the status.
- Feed: each subscriber has an `asyncio.Queue` (256); the tracker puts every published record
  on every queue; a full queue drops that subscriber, whose stream ends and whose client
  reconnects to a fresh snapshot.

### Error handling

- Docker missing or failing during reconcile (`docker ps` exits non-zero or raises
  `OSError`): log it and skip the "no container → exited" step, because treating the failure
  as "nothing runs" would end every session; the tracker retries reconcile every 30 s until it
  succeeds. `list_sessions()` today returns `[]` on a non-zero exit, so it gains a way to
  report failure (raise `DockerError`).
- Subscription connect refused while the container runs: back-off per D14.
- Store write fails (`sqlite3.Error`): logged; the in-memory state is kept and the next change
  retries the write; the API returns 500 for writes it was serving.
- Start thread raises an unexpected exception: `failed` with `error` = the exception text.

### Non-functional

- No Docker calls on read or proxy paths (requirement above).
- Feed and DB writes for activity-only changes: at most one per session per 10 s.

### Config / infra

- New file `<home>/archie.db` (+ `-wal`, `-shm`, `archie.db.lock`). Old `metrics.db` ignored.

## Milestones

### [ ] M1 — Session records, tracker and the read and create API

- **Approach:** add `store.py` (migration 1 with the import) and `tracker.py`; reshape
  `SessionDescriptor`; give `docker.list_sessions()` its internal `RunningContainer`; split
  `lifecycle.start_session`. Wire the tracker in the lifespan (migrate, reconcile, subscribe;
  the metrics loop still uses `metrics.db` until M4). Rewrite `GET /api/sessions`, add
  `GET /api/sessions/{id}`, make `POST` return 202; `resolve_session` and the live viewer use
  the tracker's ports; remove `/status`. Update `archie start` (D22, remove
  `_require_kiro_session`), `ls`, `attach` and `shell` for the new descriptor, and the web
  sessions page. Follow `web.py` `_live_events` for the agent WebSocket and its frame decoding.
  ⚠️ The ASGI tests run without the lifespan: handlers read `app.state.tracker`, and tests set
  it from `tests/session_fakes.py`.
- **Test seam:** `SessionStore` and `SessionTracker` with a fake `Runtime` and a scripted fake
  agent WebSocket server (as `tests/test_web_session_live.py` does for the live viewer); the
  HTTP API through `httpx.ASGITransport`; CLI commands through Click's runner with mocked
  `httpx`.
- **Edge cases:** Docker failing during reconcile → rows kept; Kiro turn with no `working`
  status; failed turn then `idle` → `error`; start failure → `failed` with message; container
  running with a `stopped` imported row → revived.
- **Deliverable:** `GET /api/sessions` lists records with live status from the store, POST
  returns 202 and the session reaches `idle`, and `archie start`/`ls`/`attach`/`shell` work.
- **Verify:** `env -u ARCHIE_BACKEND -u ARCHIE_SESSION_ID scripts/dev test` passes with the new
  `tests/test_session_store.py`, `test_session_tracker.py` and `test_sessions_api.py`;
  `scripts/dev ruff check .` clean.
- **Outcome:**

### [ ] M2 — Stop, rename and seen

- **Approach:** `DELETE` per D19 through `tracker.stop()`; `PATCH` per the requirement through
  `tracker.patch()`; `archie stop` handles 404 and 409 bodies; `tui/app.py` terminate accepts
  200/404/409.
- **Test seam:** HTTP API via `ASGITransport`; tracker with the fake runtime; CLI runner.
- **Deliverable:** sessions can be stopped, renamed and marked seen through the API.
- **Verify:** full test command passes, including the new DELETE/PATCH tests in
  `tests/test_sessions_api.py`.
- **Outcome:**

### [ ] M3 — Sessions feed

- **Approach:** `GET /api/sessions/feed` as a `StreamingResponse` (follow `web.py` `_sse` and
  `session_live`), with `tracker.subscribe_feed()` queues, D11 throttling of activity-only
  changes, D20 filtering and a 15 s ping.
- **Test seam:** the ASGI app with a tracker; read the stream with `httpx` streaming.
- **Deliverable:** a client receives a snapshot and then each change.
- **Verify:** `tests/test_sessions_feed.py` passes; full suite passes.
- **Outcome:**

### [ ] M4 — Metrics in archie.db

- **Approach:** point `MetricsIndex` at `archie.db`, call `store.migrate()` before opening,
  rename the tables, drop the metrics `sessions` table and the `_ensure_session`/`SessionStarted`/
  `UserMessage` title and `last_activity_at` writes, apply D8's skip, replace
  `_archive_database` with D5's drop-and-recreate, keep the flock (now `archie.db.lock`).
  Update `insights.py` joins (`sessions` for workspace, start, last activity, name when fixed),
  `insights_web.py`, `api.py` `_query_metrics`, `stats.py`, `cli.py` `migrate-sessions --db`,
  and the insights templates.
- **Test seam:** `MetricsIndex` over a temp home with logs and store rows; insights query
  functions; `archie stats` and `migrate-sessions` via the CLI runner; `/insights` pages.
- **Deliverable:** insights, `archie stats` and `/api/metrics` read `archie.db`.
- **Verify:** full suite passes; the requirement's "sessions unchanged" test passes.
- **Outcome:**

### [ ] M5 — Ended-session history, prompt and docs

- **Approach:** the `/events` ended-session branch (D21); `environment.md`; the docs and
  README listed in the requirements.
- **Test seam:** HTTP API via `ASGITransport` with a log on disk; `tests/test_environment.py`.
- **Deliverable:** history of ended sessions is readable through the API; docs match.
- **Verify:** full suite passes; `rg -n 'metrics\.db|archie\.db' persona/` finds nothing.
- **Outcome:**

## Deviations

## Risks

- The tracker is one more WebSocket client per session on the agent's bus; a stalled tracker is
  disconnected by the agent's 1024-frame queue and resubscribes (D14).
- `unseen` is set on every root turn end and nothing clears it until the TUI refactor uses
  `PATCH`; until then it is true for most sessions.
- Imported `last_activity_at` uses the log's mtime, which a copy or `touch` can change.
- `archie stats` run before the orchestrator ever starts performs the one-off import itself.

## Review

## Outcome

PR: pending
CI: pending

## Follow-ups

- `model` and `prompt` on `POST /api/sessions` (roadmap item 5).
- Repo, worktree and branch detection from tool calls, and automatic naming from the worktree.
- The attach TUI and the future sidebar clearing `unseen` with `PATCH`.
- The prototype branch's `data.py` reads `metrics.db`; move it to the API when the TUI is
  refactored.

# 082 — Session state store and session API

Status: building
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
| D8 | Logs with no session row | The metrics sync skips the log and records `no session record` in `metrics_ingest_files.error`; it is not a strict-mode error. The one-off import always reads `<db dir>/sessions` (the home's logs); `migrate-sessions --sessions-dir` only chooses which logs are migrated and indexed | insert a row from the sync | Keeps "metrics never writes `sessions`" absolute; after the import every orchestrator-started session has a row before its log exists | self |
| D9 | How the tracker learns state | One read-only WebSocket per running session to the agent's `/stream`, like `web.py` `_live_events` | polling `/status`; reading logs | Push, already proven in the live viewer; carries `session_status` and turn events | self |
| D10 | Status rules from events | ("Root" means `scope is None`.) Root `user_message` → `working`; `session_status` sets model, question and `idle`/`working`/`blocked` (`done` maps to `idle`); after a root `turn_error`, the following `idle` becomes `error` until the next root `user_message`; root `turn_complete` and `turn_error` set `unseen`; a root `turn_interrupted` clears a pending `error` and doesn't set `unseen` (the user caused it); a `session_status` model starting with `kiro/` sets `backend` to `kiro` | trusting `session_status` alone | Kiro sends no `working` status at turn start; the agent re-sends `idle` straight after a failed turn, which would hide the failure; an adopted container's backend is otherwise unknown | self |
| D11 | Write rate | Status, question, model, unseen and name changes are written and published at once; a change to `last_activity_at` alone is written and published at most every 10 s per session, with a trailing flush at the end of the window so the last activity is never lost; every other write carries the latest `last_activity_at` | write every event | Text deltas and tool events arrive many times a second; the feed would flood | self |
| D12 | Database access from async code | Synchronous `sqlite3` in `store.py`, one short-lived connection per call, always called through `asyncio.to_thread` | `aiosqlite` (new dependency); calls on the event loop | The metrics sync and the tracker share one SQLite write lock, so a tracker write can wait behind a metrics file commit (up to the 5 s busy timeout); no new dependency | self |
| D12a | Read source | The tracker keeps every non-ended record in memory (loaded from the store at start) and is authoritative for them; `GET` routes and the feed snapshot read live records from memory and ended ones from the store. A failed store write is logged and retried with the next change; a record leaves memory only after its terminal write has committed, and a failed terminal write is retried every 5 s | read everything from the store | Reads never wait on the SQLite lock, and the API and the feed can't disagree after a failed write | self |
| D13 | Startup order and reconcile | Migrate (before any metrics lock is taken), then reconcile with one `docker ps`, then subscribe, then start the metrics loop. A running container makes its row live (`idle`, `ended_at` cleared), adopting it from its log if there is no row (with no readable log: `workspace` from `split_id`, `started_at` from the id's ULID time else now, `backend` `native` until D10 corrects it); a non-terminal row with no container becomes `exited` (`ended_at` = log mtime or now), `stopped` if it was `stopping`, or `failed` ("orchestrator restarted during startup") if it was `starting` | trusting stored status | Docker is the truth for whether a container exists; the import marks running sessions `stopped` and this revives them | self |
| D14 | Ending detection | When a subscription closes: if the status is `stopping` or `stopped`, do nothing; else check `docker inspect`; running → resubscribe with 1, 2, 5 s back-off; gone → `exited`. Back-off stays at 5 s while the container runs; the status doesn't change. Cancellation by `SessionTracker.close()` (orchestrator shutdown) skips this check entirely | treat every close as an end | The agent closes slow clients (queue overflow) and restarts drop sockets | self |
| D15 | Startup split | `lifecycle.prepare_start()` validates the workspace and image and builds the id and command, synchronously inside the POST; `lifecycle.run_start()` runs and waits in the background task; `start_session()` remains their composition | validate in the background | Bad input keeps its synchronous 400; only the slow part moves | self |
| D16 | Docker access in the tracker | A small `Runtime` object (list running, prepare, run, stop, is running) with a `DockerRuntime` implementation; tests pass a fake, and the lifespan uses `app.state.runtime` when a test has set it, else `DockerRuntime()` | patching module functions | 15 test files reference `list_sessions` today; one seam is easier to fake than many | self |
| D17 | Agent port for proxying | The tracker keeps `session_id → port` in memory from `run_start` and reconcile. `proxy.resolve_session(tracker, session_id) -> AgentEndpoint(session_id, port)` (new small struct in `proxy.py`) replaces the Docker lookup; `forward_http` takes the endpoint; handlers pass `request.app.state.tracker`; `web.py` `_resolve_live(tracker, session_id)` and `_active_ids()` → `tracker.live_ids()` follow | `docker ps` per request (today) | Removes N+1 Docker calls from every proxied request (architecture finding 5 for these paths) | self |
| D18 | `PATCH` on ended sessions | Allowed for `name` and `unseen` | 409 | Names show in insights; renaming history is useful and touches no lifecycle state | self |
| D19 | `DELETE` response | `Runtime.stop` calls `docker.stop_container(container_name(id))` directly (no `docker ps`) and then today's `_remove_empty_session_log`. DELETE waits for it and returns 200 with the `stopped` record; 409 for `starting`, `stopping` and ended sessions. If `stop_container` fails and `container_running()` says the container is gone, the result is still `stopped`; if it is still running, the previous status is restored, the subscription restarted if it ended meanwhile, and 500 returned. `Runtime.stop` returns whether it removed the log (`_remove_empty_session_log` gains a bool return) | 202 with `stopping`; `lifecycle.stop_session` (runs `docker ps`, raises `KeyError` on a vanished container) | Keeps `archie stop` simple; the feed still shows `stopping` in between; a container that vanished mid-stop is the outcome the user asked for | self |
| D19a | A stopped session that never received a prompt | Its log is deleted as today, and its `sessions` row is deleted too; the DELETE response and the feed's terminal event still carry the `stopped` record, and later reads return 404 | keep the row without a log; stop deleting empty logs | Matches today's "a session with no prompt leaves no trace" without a canonical row pointing at a missing log | self |
| D19b | Sessions in insights | Insights and `archie stats` list only sessions that have a `metrics_ingest_files` row (a log was indexed) | every `sessions` row | `failed` starts and never-prompted sessions have no log or metrics; before this change they never appeared | self |
| D20 | Feed filter | The subscriber's queue is registered before the snapshot is built, so no change is lost. `?include=ended` only changes the snapshot; every change event is forwarded, including a session's final terminal one | filter change events too | Clients drop a session on its terminal event; ended rows never change again, so nothing else leaks | self |
| D21 | `GET /api/sessions/{id}/events` for ended sessions | Served from the log with `SessionLog(path).read(after)`; `CursorNotFound` → 409 as the agent does; an ended session with no log (a `failed` start) → 200 with an empty body. A `starting` session (no port yet) → 503 for `/events` and close code 4003 for `/stream`, as today's "no port" case | 404 | Makes the record and its history consistent after the container goes, at little cost | self |
| D22 | `archie start` waiting | Poll `GET /api/sessions/{id}` every 0.5 s while `starting`, up to 60 s, inside the existing 080 status indicator; `failed` prints the stored `error`; another terminal status prints `Session ended during startup (<status>)`; a timeout prints `Session is still starting after 60 s; check archie ls` and sends no DELETE; all three exit 1 | a `?wait` flag on POST | One contract for every client; the poll is cheap | self |
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
  `archie migrate-sessions`, except the one-off canonical migration (D6), which runs in
  whichever process opens `archie.db` first.
  - AC: a test migrates first, then runs `MetricsIndex.sync()` and `reset_and_backfill()`
    and asserts the `sessions` rows are identical before and after.

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
- MUST NOT call Docker in `GET /api/sessions`, `GET /api/sessions/{id}`, the pages `/`,
  `/sessions` and `/sessions/{id}`, or when resolving a session for `/events`, `/stream` or the
  live viewer.
  - AC: tests run those routes with `archie_orchestrator.docker.subprocess.run` patched to
    raise; they succeed.

API

- MUST serve `GET /api/sessions` (non-ended by default, `?include=ended` for all) and
  `GET /api/sessions/{id}` (404 for an unknown id) as `SessionDescriptor` JSON.
  - AC: API tests for both filters and the 404.
- MUST accept `POST /api/sessions` `{workspace, backend?, name?}`, keep today's 400s for a bad
  body, workspace or backend, return 400 for an invalid `name` (not a string, blank after
  trimming, over 80 characters), and otherwise return 202 with a `starting` record.
  - AC: API tests for each 400 and the 202; `name` given → `name_fixed: true`.
- MUST accept `PATCH /api/sessions/{id}` with `name` (a string valid under POST's rule is
  stored trimmed and sets `name_fixed`; `null` resets it to the id and clears `name_fixed`) and
  `unseen: false`; anything else, including an invalid name, is 400; unknown id 404. POST
  stores its `name` trimmed too.
  - AC: API tests for each field, `null`, an unknown key, `unseen: true`, and 404.
- MUST stop a session with `DELETE /api/sessions/{id}` per D19.
  - AC: API tests: 200 with `stopped` and `ended_at`; 409 for `starting` and ended; 404; Docker
    failure with the container still running → 500 and the previous status restored; failure
    with the container gone → 200 `stopped`; a never-prompted session's row is gone afterwards.
- MUST serve `GET /api/sessions/feed` as `text/event-stream`: a `snapshot` event
  (`{"sessions": [...]}`, same filter as the list) on connect, a `session` event with the full
  record on each change per D11 and D20, and a `: ping` comment every 15 s.
  - AC: feed tests read the snapshot, then a `session` event after a PATCH, and a terminal
    event for a session that ends while `include=ended` is off.
- MUST remove `GET /api/sessions/{id}/status`.
  - AC: the route test asserts it is absent (404).
- MUST serve `GET /api/sessions/{id}/events` from the log once a session has ended (D21).
  - AC: API test returns the log's events, honours `after`, 409 for an unknown cursor, empty
    200 for a `failed` session with no log.

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
- MUST show the name in insights and `archie stats` only when `name_fixed` (D1), and list only
  sessions with indexed logs (D19b).
  - AC: insights page and stats tests: a fixed name appears; an unfixed one doesn't; a
    `failed` row with no log is not listed.

CLI, web and prompt

- MUST keep `archie start`, `ls`, `attach`, `stop`, `shell` and the TUI's quit-and-terminate
  working against the new API: `start` waits per D22 and reports a `failed` start's `error`;
  `--detach` prints the session and container (the `Agent: http://host:port` line is dropped); `ls` shows id, workspace, status and the name
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
    intentional mentions (the old file, the agent's own `/status`, `/api/auth/status`).

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
  `/sessions/{session_id}`. `get_metrics` and `get_session_metrics` build the `archie.db` path.
- `orchestrator/src/archie_orchestrator/proxy.py`: `resolve_session(tracker, session_id)` and
  `AgentEndpoint` (D17); `proxy_status` removed.
- `orchestrator/src/archie_orchestrator/app.py`: lifespan runs D13's order, stores the tracker
  on `app.state.tracker`, closes it on shutdown.
- `orchestrator/src/archie_orchestrator/metrics.py` (`schema_version`, `is_current` and
  `_open_current` switch from `PRAGMA user_version` to the `meta` row), `insights.py`,
  `insights_web.py`,
  `web.py` (`sessions_page`, `_active_ids`, `_resolve_live`), `templates/sessions.html`,
  `templates/insights/sessions.html`, `templates/insights/session.html`.
- `cli/src/archie_cli/cli.py` (`start`, `ls`, `stop`, `migrate-sessions`, row formatting,
  `_require_kiro_session` removed), `cli/src/archie_cli/stats.py`, `cli/src/archie_cli/tui/app.py`
  (terminate status codes only).
- `tests/conftest.py`: an autouse fixture sets `ARCHIE_HOME_DIR` to a per-test temp directory, so
  no test (the lifespan tests in `test_orchestrator_bounce.py` and `test_orchestrator_logging.py`
  included) can create or import into the real `~/.nexus/archie.db`; tests that set the variable
  themselves still override it.
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
- Stop: `DELETE` → `stopping` (published) → `Runtime.stop` in a thread (D19) → `stopped`,
  `ended_at` (row deleted per D19a when the log was removed); the subscription's close is
  ignored because of the status.
- Shutdown: `SessionTracker.close()` cancels subscriptions and flushes pending activity; no
  status changes.
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
- Store write fails (`sqlite3.Error`): logged; the in-memory record (D12a) stays authoritative
  and the next change retries the write. `POST` fails with 500 if its insert fails (nothing is
  started); `PATCH` and `DELETE` return 500 if their write fails.
- Start thread raises an unexpected exception: `failed` with `error` = the exception text.

### Non-functional

- No Docker calls on read or proxy paths (requirement above).
- Feed and DB writes for activity-only changes: at most one per session per 10 s.

### Config / infra

- New file `<home>/archie.db` (+ `-wal`, `-shm`, `archie.db.lock`). Old `metrics.db` ignored.

## Milestones

### [x] M1 — Session records and the read and create API

- **Approach:** add `store.py` (migration 1 with the import, `SessionStore`) and `tracker.py`
  with `Runtime`/`DockerRuntime`, in-memory live records (D12a), `create()` with the
  background start, and a subscription that applies only `session_status` (state, model,
  question) for now. Reshape `SessionDescriptor`; `docker.list_sessions()` returns
  `RunningContainer` and raises `DockerError` on a failing `docker ps`; split
  `lifecycle.start_session` (D15). Proxy and live viewer resolve ports through the tracker
  (D17), including `_active_ids()` → `tracker.live_ids()` and `proxy_status`'s lookup (removed
  in M2). `web.py` `sessions_page` reads tracker records with the template unchanged apart
  from the status column (`status` instead of `raw_docker_status`, no port), so the suite
  stays green; M2 finishes the page. Add the `tests/conftest.py` home-isolation fixture
  first. Lifespan: migrate, a minimal reconcile (adopt running containers, revive imported
  rows), subscribe; the metrics loop still uses `metrics.db` until M5. Rewrite
  `GET /api/sessions`, add `GET /api/sessions/{id}`, make `POST` return 202. Update
  `archie start` (D22, remove `_require_kiro_session`, drop the `Agent:` line), `ls`, `attach`
  and `shell`. Follow `web.py` `_live_events` for the agent WebSocket and frame decoding.
  ⚠️ ASGI tests run without the lifespan: handlers read `app.state.tracker`, set by tests
  from `tests/session_fakes.py`; lifespan tests set `app.state.runtime` (D16).
- **Test seam:** `SessionStore` over a temp home; `SessionTracker` with a fake `Runtime` and
  `websockets.connect` patched with a `FakeAgent`-style async iterator, as
  `tests/test_web_session_live.py:36-65,109` does; the HTTP API through
  `httpx.ASGITransport`; CLI commands through Click's runner with mocked `httpx`.
- **Edge cases:** start failure → `failed` with the message (Kiro startup-error text
  included); container running with a `stopped` imported row → revived; log without
  `session_started` or requests → D7 fallbacks.
- **Deliverable:** `POST` returns 202, the session reaches `idle`, `GET /api/sessions` lists it
  from memory and the store, and `archie start`/`ls`/`attach`/`shell` work.
- **Verify:** `env -u ARCHIE_BACKEND -u ARCHIE_SESSION_ID scripts/dev test` passes, including
  new `tests/test_session_store.py`, `tests/test_session_tracker.py` and
  `tests/test_sessions_api.py`; `scripts/dev ruff check .` is clean.
- **Outcome:** Built `store.py` (`archie.db`, WAL + busy timeout, `user_version` migrations
  under the `archie.db.lock` flock, migration 1 with the D7 import, `SessionStore`
  `insert`/`save`/`get`/`list`/`update`) and `tracker.py` (`Runtime`, `DockerRuntime`,
  `SessionTracker` with in-memory live records, the D12a write and terminal-retry rules,
  `create()` with the background `run_start`, a `/stream` subscription applying
  `session_status`, `start()` = migrate + load + minimal reconcile, `close()`, a listener hook
  for M4). Reshaped `SessionDescriptor` (+ `SessionRecordStatus`, `TERMINAL_STATUSES`,
  computed `container_name`); `docker.list_sessions()` returns `RunningContainer` and raises
  `DockerError`; `container_running()` public; `lifecycle.prepare_start`/`run_start`.
  `GET /api/sessions[?include=ended]`, `GET /api/sessions/{id}`, `POST` → 202; proxy, `/status`,
  the live viewer, `/`, `/sessions` and `/sessions/{id}` resolve through the tracker (no
  Docker); lifespan runs the tracker (`app.state.runtime` honoured). CLI `start` polls per D22,
  `_require_kiro_session` and the `Agent:` line removed, `ls`/picker rows show
  `SESSION ID, WORKSPACE, STATUS, NAME`. `tests/conftest.py` isolates `ARCHIE_HOME_DIR`;
  `tests/session_fakes.py` holds `FakeRuntime`, `FakeAgent`, `make_tracker`/`serve`/
  `use_runtime`. New tests: `test_session_store.py` (18), `test_session_tracker.py` (19),
  `test_sessions_api.py` (27); 23 existing test files moved to the tracker/record seams.
  Evidence: `env -u ARCHIE_BACKEND -u ARCHIE_SESSION_ID scripts/dev test` → 4900 passed
  (baseline 4818); `scripts/dev ruff check .` clean; `ruff format --check` on the 39 touched
  files clean; no `archie.db` appeared in the real `~/.nexus` after the run.

### [x] M2 — Turn status, reconcile and ending

- **Approach:** in `tracker.py`, the full D10 frame rules, D11 throttling with the trailing
  flush, D13's remaining reconcile cases with the 30 s retry when Docker fails, D14 end
  detection with back-off and the shutdown rule. Remove `/api/sessions/{id}/status` and
  `proxy_status`. Web `/` page shows status, name when fixed and last activity
  (`templates/sessions.html`).
- **Test seam:** `SessionTracker` with the fake runtime and `FakeAgent` frame scripts; pages
  through `ASGITransport` with a runtime whose Docker methods raise.
- **Edge cases:** Kiro turn without a `working` status; failed turn then `idle` → `error`;
  interrupted turn; Docker failing during reconcile → rows kept; close with the container
  running → resubscribe; shutdown → no status change.
- **Deliverable:** records follow turns and endings without a client attached.
- **Verify:** full test command passes, including the D10, D13 and D14 tracker tests and
  `tests/test_web_console.py`; Ruff clean.
- **Outcome:** `tracker.py` now folds the full D10 rules (root `user_message` → `working` and
  clears the failed-turn flag; root `turn_error` → flag + `unseen`, the next `idle` stored as
  `error` until the next root message; root `turn_complete` → `unseen`; root
  `turn_interrupted` clears the flag; `kiro/` models set `backend`; subagent events are
  activity only). D11: every event other than handshake/status/notice is activity, held in
  `_pending_activity` and merged into the next write; activity-only changes are written at
  most every `activity_interval` (default `ACTIVITY_INTERVAL` 10 s, injectable with `clock`)
  with a trailing flush task; `close()` writes pending activity and gives pending terminal
  writes one last attempt (D12a). D13: `start()` reconciles the loaded rows (stopping →
  `stopped`, starting → `failed` "orchestrator restarted during startup", other → `exited`,
  `ended_at` = log mtime or now); on a Docker failure nothing ends and reconcile retries
  every `RECONCILE_RETRY_INTERVAL` (30 s), touching only rows loaded at start and unknown
  containers, so sessions created meanwhile are left alone. D14: `resubscribe_delay()` gives
  1, 2, 5, 5… s, reset once a stream delivers a frame; cancellation skips the end check.
  `GET /api/sessions/{id}/status` and `proxy_status` removed (route test asserts the 404);
  `/` shows Session ID, Name (only when `name_fixed`), Workspace, Status and Last activity
  (UTC). Tests: 25 new tracker tests (each D10 sequence, D11 throttle/trailing flush/carry/
  close/end, each D13 case plus Docker failing and retry, D14 back-off, stopping, shutdown),
  a `/` columns test, the `/status` proxy tests moved to `/events`; `test_web_console.py`
  now sets `app.state.start_time` itself (it failed when run alone before). Evidence:
  `env -u ARCHIE_BACKEND -u ARCHIE_SESSION_ID scripts/dev test` → 4927 passed (M1: 4900);
  `tests/test_session_tracker.py` run 40 times in a row with no failure; `scripts/dev ruff check .`
  clean; `ruff format --check` on the touched files clean.

### [x] M3 — Stop, rename and seen

- **Approach:** `DELETE` per D19 and D19a through `tracker.stop()`; `PATCH` per the
  requirement through `tracker.patch()`; `archie stop` reports 404 and 409 bodies;
  `tui/app.py` terminate accepts 200/404/409.
- **Test seam:** HTTP API via `ASGITransport`; tracker with the fake runtime; CLI runner.
- **Deliverable:** sessions can be stopped, renamed and marked seen through the API.
- **Verify:** full test command passes, including the DELETE and PATCH tests in
  `tests/test_sessions_api.py`; Ruff clean.
- **Outcome:** `tracker.stop()` (D19, D19a): unknown → `KeyError`; `starting`, `stopping`
  and ended → new `SessionConflictError`; otherwise `stopping` is written and published,
  `Runtime.stop` runs in a thread (`DockerRuntime.stop` = `docker.stop_container` by name, no
  `docker ps`, then `_remove_empty_session_log`, which now returns whether it removed the log;
  `lifecycle.stop_session` is unchanged), the follow task is cancelled and the record becomes
  `stopped` + `ended_at`. A removed log puts the session in `_discard`, so its terminal write
  (`_persist`) deletes the row (`SessionStore.delete`, new) after the `stopped` record is
  published; a failed delete is retried like any terminal write (D12a). A failed stop asks
  `is_running`: gone → `stopped` (the log is left in place); running (or Docker can't tell) →
  the previous status is restored, the stream followed again if its task ended, and the
  error re-raised. While `stopping`, agent frames don't change the status, and `_still_running`
  re-checks the record after `is_running` so a close racing a stop can't mark `exited`.
  `tracker.patch(name=, unseen=)` (D18): live and pending-terminal records go through
  `_update(strict=True)` (a failed write raises `sqlite3.Error`, memory keeps the change);
  ended ones through `SessionStore.update`, then published. API: `DELETE` → 200 `stopped`
  record / 404 / 409 `{"error": ...}` / 500; new `PATCH /api/sessions/{id}` with the POST name
  validator shared (`_INVALID_NAME`), 400 for a non-object, empty object, unknown keys, an
  invalid name or `unseen` other than `false`, 404, 500 on a failed write. `archie stop` shows
  the 409 body; the TUI accepts 200/404/409 on terminate. M1's deviation is resolved: a
  session stopped with `archie stop` now ends as `stopped`, not `exited`. Tests: 17 tracker
  tests (stop sequence, row deletion and its retry, each conflict, unknown id, failure with
  the container running/gone, frames while stopping, the close/stop race, patch live/ended/
  unknown/write failure, `DockerRuntime.stop`), 28 API tests in `test_sessions_api.py` (each
  D19 AC including a never-prompted session through the real `DockerRuntime`, every PATCH
  field, `null`, each bad body, 404, ended rename, write failure live and ended), 3 `archie
  stop` tests (404, 409, 500), the TUI terminate test parametrised over 200/404/409, the old
  `stop_session`-patching DELETE tests moved to the tracker, `_remove_empty_session_log`
  return values. Evidence: `env -u ARCHIE_BACKEND -u ARCHIE_SESSION_ID scripts/dev test` →
  4977 passed (M2: 4927); `tests/test_session_tracker.py` + `tests/test_sessions_api.py` run
  40 times in a row with no failure; `scripts/dev ruff check .` clean; `ruff format --check`
  on the 14 touched files clean.

### [x] M4 — Sessions feed

- **Approach:** `GET /api/sessions/feed` as a `StreamingResponse` (follow `web.py` `_sse` and
  `session_live`), with `tracker.subscribe_feed()` queues (256, a full queue drops the
  subscriber), D20 filtering and a 15 s ping.
- **Test seam:** the ASGI app with a tracker; read the stream with `httpx` streaming.
- **Deliverable:** a client receives a snapshot and then each change.
- **Verify:** `tests/test_sessions_feed.py` passes; full suite passes; Ruff clean.
- **Outcome:** `tracker.subscribe_feed(maxsize=FEED_QUEUE_SIZE)` returns a `FeedSubscription`
  (context manager or `close()`, idempotent) whose `asyncio.Queue(256)` is registered as a
  tracker listener, so it gets every record `_publish` emits, D11's throttle included. A full
  queue drops that subscriber: it unregisters, its backlog is replaced by an end marker and
  `get()` returns None. `GET /api/sessions/feed` (`api.sessions_feed`, routed before
  `/sessions/{session_id}`) validates `include` with the list's 400, subscribes, then builds the
  snapshot with `tracker.list()` (D20; a store failure → 500 and the subscription closed), and
  streams `event: snapshot` `{"sessions": [...]}`, one `event: session` per published record
  whatever the filter, and `: ping` after `FEED_PING_INTERVAL` (15 s, module constant) idle;
  `Cache-Control: no-cache` and `X-Accel-Buffering: no` as the live view sends. Descriptors are
  encoded with `msgspec.json.encode` as the list route does (compact JSON, so data is one line;
  no `_sse` import from `web.py`). The generator closes the subscription when it ends or is
  cancelled on disconnect; a response whose body never starts is dropped at its first overflow.
  `httpx.ASGITransport` (0.28.1) collects the whole body before returning, so it can't read an
  endless stream; the feed tests drive the ASGI app directly (`Feed` in the test file: body
  chunks as sent, a gate on `send` to stall the client, `http.disconnect` to close). Tests
  (14, `tests/test_sessions_feed.py`): snapshot equals the list for both filters plus headers;
  empty snapshot; 400; 500 with no subscriber left; PATCH → `session` event equal to the PATCH
  body; PATCH of an ended session sent with `include=ended` off; DELETE → `stopping` then the
  terminal `stopped` record and a fresh snapshot without it; a change published while the
  snapshot is read arrives after it (D20); pings with a 0.05 s interval and an event after them;
  disconnect unsubscribes; a stalled client is dropped when its queue overflows behind the record
  it is sending, gets that record and then the end of the body; `/feed` doesn't shadow
  `GET /api/sessions/{id}`; and two
  `FeedSubscription` unit tests (order; overflow drops only the slow subscriber). Evidence: the
  full suite → 4991 passed (M3: 4977); `scripts/dev ruff check .` clean; `ruff format --check` on
  the 3 touched files clean; feed + API + tracker tests run 40 times in a row with no failure or
  hang (every wait in the feed tests is bounded by `asyncio.wait_for`). Checks ran on HEAD plus
  the M4 changes only: an M5 edit in progress in the same worktree broke collection of
  `tests/test_orchestrator_metrics.py` at the time.

### [x] M5 — Metrics in archie.db

- **Approach:** point `MetricsIndex` at `archie.db`; `sync()` and `reset_and_backfill()` call
  `store.migrate()` before taking their own lock (both use `archie.db.lock`, and a second
  `flock` in one process would block);
  rename the tables; drop the metrics `sessions` table and the `_ensure_session`,
  `SessionStarted`, `UserMessage` title and `last_activity_at` writes; apply D8's skip; replace
  `_archive_database` and the `user_version` use with D5's `meta` row and a drop-and-recreate
  of `metrics_*` (one transaction for the drop and create; the refill commits per file as
  today); keep the flock as `archie.db.lock`. Update `insights.py` joins (D1, D19b),
  `insights_web.py`, `api.py` `_query_metrics`, `stats.py`, `cli.py` `migrate-sessions --db`,
  the insights templates, and the lifespan's metrics loop.
- **Test seam:** `MetricsIndex` over a temp home with logs and store rows; insights query
  functions; `archie stats` and `migrate-sessions` via the CLI runner; `/insights` pages.
- **Deliverable:** insights, `archie stats` and `/api/metrics` read `archie.db`.
- **Verify:** full suite passes, including the "sessions unchanged" test; Ruff clean.
- **Outcome:** `metrics.py` indexes into `archie.db` as `metrics_ingest_files`,
  `metrics_requests`, `metrics_turns`, `metrics_tool_calls`, `metrics_compactions` and
  `metrics_subagents` (same columns, indexes renamed `idx_metrics_*`); the metrics `sessions`
  table, `initial_model`, `_ensure_session`, the `SessionStarted`/title/`last_activity_at`
  writes, `TITLE_CHARS`, `_end_time` and `_archive_database` are gone, and `connect` is
  `store.connect`. `sync()` and `reset_and_backfill()` run `MetricsIndex._migrate()` before
  their `archie.db.lock` flock; it calls `store.migrate(db)` (import from `<db dir>/sessions`)
  only when `store.schema_version` is below the latest, so an already-migrated database never
  waits on the lock and `sync(wait=False)` stays non-blocking. D5: `METRICS_VERSION = 5` in
  the `meta` row `metrics_version`; `schema_version`/`is_current`/`_open_current` read only
  that row; a missing or different version (and `reset_and_backfill`) drops every
  `metrics_*` table and recreates them plus the meta row in one `BEGIN IMMEDIATE`
  transaction, then refills per file. D8: `_sync_file` first checks `sessions`; with no row
  it records `no session record` (size -1, rechecked each sync), counts
  `SyncStats.skipped` (new) and is not an error in strict mode; `_record_error` now also
  resets `size` to -1 on conflict. `insights.py` joins canonical `sessions`; `name` is
  `CASE WHEN name_fixed THEN name END` (D1) in place of `title`, `initial_model` dropped;
  `duration_s` now uses the record's `last_activity_at`. D19b rule, one SQL fragment
  `_INDEXED` used by session lists and their `total`, the overview `sessions` count,
  `filter_options` workspaces and `session_detail`: a session is listed iff it has a
  `metrics_ingest_files` row with `error IS NULL` (its log was read successfully); request,
  tool, turn and model aggregates are not filtered. Templates show `Name` (fixed only);
  `archie stats` sessions column `Name`, `stats session` prints the name. `api.py` reads
  `store.db_path(home)` / `metrics_requests`; the lifespan's loop already used
  `default_index()`, now `archie.db` (no `app.py` change). `migrate-sessions --db` (default
  `<home>/archie.db`, prints `rebuilt metrics in <db>`, also maps `RuntimeError` from a
  too-new database to exit 1); nothing reads, moves or deletes `metrics.db`. Tests:
  `tests/session_fakes.py` gains `migrated_store`, `set_mtime`, `sessions_rows`; new tests for
  the version bump (stale `metrics_obsolete` dropped, sessions identical, refill), a missing
  version row, no rebuild when current, the version in `meta` not `user_version`, the exact
  table set (no `requests` / old `sessions` columns), sync + rebuild leaving `sessions`
  identical, the D8 skip (strict sync and rebuild, then indexed once a row exists), the import
  reading `<db dir>/sessions`, `metrics.db` untouched, insights/page/stats fixed vs unfixed
  names and a `failed` row with no log absent from lists, counts, workspaces and the session
  page (404 / "Session not found"), a log that failed to index not listed, `/api/metrics`
  reading `archie.db`, and `migrate-sessions --db` (rebuild keeps rows, default path, the old
  option rejected). Evidence: `env -u ARCHIE_BACKEND -u ARCHIE_SESSION_ID scripts/dev test` →
  5009 passed (M4 head: 4992 reported); `scripts/dev ruff check .` clean; `ruff format --check`
  on the 14 touched Python files clean; no `~/.nexus/archie.db` created by the run.

### [x] M6 — Ended-session history, prompt and docs

- **Approach:** the `/events` ended-session branch (D21); `environment.md`; the docs and
  README listed in the requirements; the browser screenshot of `/` for the PR.
- **Test seam:** HTTP API via `ASGITransport` with a log on disk; `tests/test_environment.py`.
- **Deliverable:** history of ended sessions is readable through the API; docs match.
- **Verify:** full suite passes; `rg -n 'metrics\.db|archie\.db' persona/` finds nothing;
  `rg -n 'metrics\.db|raw_docker_status|/status\b' docs README.md` shows only the old-file
  note, the agent's own `/status` and `/api/auth/status`; a screenshot of `/` with a running session exists.
- **Outcome:** `proxy.proxy_events` (D21): a session the tracker doesn't hold as live is looked
  up with `tracker.get()` (memory, so an ended record awaiting its terminal write counts, else
  the store); an ended one is answered from `tracker.log_path(id)` (the former private
  `_log_path`, now public) with `SessionLog(path).read(after)` in `asyncio.to_thread`, encoded
  with `encode_event` one line per event as `application/x-ndjson`, exactly as the agent's
  `events`; `CursorNotFound` → 409 `{"error": "cursor_not_found", "cursor": after}`; no log →
  200 with an empty body; a store read failure → 500 `Session store unavailable`, an unreadable
  or conflicting log (`LogAppendError`, `EventIdConflict`) → 500 like the agent's catch-all.
  Unknown → 404, `starting` → 503 and running → proxied, all unchanged. `environment.md` no
  longer lists `metrics.db` (and doesn't mention `archie.db`). Docs: `architecture.md` (lifecycle
  steps for the 202 start, background run and CLI poll, port lookup from the tracker, stop and
  restart; new "Session records and the tracker" section with the `SessionDescriptor` fields,
  the status vocabulary, the D10/D11/D14 folding, D12a writes, D13 reconcile and the feed; the
  route table with `?include=ended`, POST `name`/202, feed, `GET`/`PATCH`/`DELETE {id}` semantics,
  ended-session `/events`, no `/status` proxy; new "Orchestrator datastore" section (`sessions`/
  `meta`, `user_version` migrations under `archie.db.lock`, the D7 import, old `metrics.db` no
  longer read and deletable); "Orchestrator metrics" on `metrics_*` tables, the join, the D8 skip,
  D1/D19b, the `meta` `metrics_version` rebuild and `migrate-sessions --db`; ownership and change
  impact), `session-protocol-walkthrough.md` (component list, section 4 diagram and text for the
  202 + poll flow, connection step, ended-session `/events`, metrics wording, invariant 2, the
  end-to-end summary), `kiro.md` (non-blocking start; a failed start's reason is the record's
  `error`), `event-spec.md` (`migrate-sessions` rebuilds `metrics_*` in `archie.db`) and
  `README.md` (`archie.db`: session records plus a rebuildable index; old file deletable). Tests
  (7, `tests/test_sessions_api.py`): ended events equal the log's NDJSON with the content type,
  `after` honoured (tail and empty after the last), 409 body, empty 200 for a `failed` session
  with no log, an ended record still in memory served from the log, a running session with a log
  still proxied, unknown 404, store failure 500. Evidence: `env -u ARCHIE_BACKEND -u
  ARCHIE_SESSION_ID scripts/dev test` → 5016 passed (M5: 5009), `tests/test_environment.py` 12
  passed; `scripts/dev ruff check .` clean; `ruff format --check` on the 4 touched Python files
  clean; `rg -n 'metrics\.db|archie\.db' persona/` finds nothing; the docs grep shows only the
  old-file notes (`README.md:66`, `docs/architecture.md`), the agent's own `/status`
  (`architecture.md` lifecycle step 4 and agent-service paragraph, `event-spec.md:83`,
  `kiro.md:26`, the walkthrough's section 4) and `/api/auth/status`; `rg -n
  'initial_model|--metrics-db|ingest_files\b' docs README.md` shows only the new
  `metrics_ingest_files`. No `~/.nexus/archie.db` created by the run. The screenshot of `/` with
  a running session is pending; it is captured during delivery.

## Deviations

- M1: the subscription already ends sessions minimally: when a stream closes it asks
  `Runtime.is_running` and marks the record `exited` (container gone) or follows again after a
  fixed 1 s (still running); `stopping`/ended records are not checked. Without it a container
  that stops (including through `archie stop`, whose DELETE is unchanged until M3) would stay
  listed as live until the next orchestrator start. M2 replaces the fixed delay with D14's
  1, 2, 5 s back-off; until M3, a session stopped with `archie stop` ends as `exited`.
- M1: `SessionStore.save()` (an upsert of the whole record) was added next to
  `insert`/`get`/`list`/`update`; the tracker writes its in-memory record whole, which is what
  makes D12a's "retried with the next change" and reconcile adoption work without a separate
  insert path.
- M3: `DELETE` returns 200 with the `stopped` record even when writing that record fails;
  the terminal write is retried per D12a and readers see the record from memory meanwhile.
  Error handling said `DELETE` returns 500 on a failed write, but by then the container has
  stopped, and a 500 would tell `archie stop` the stop failed while a retry gets 409. `PATCH`
  keeps the 500.

## Risks

- The tracker is one more WebSocket client per session on the agent's bus; a stalled tracker is
  disconnected by the agent's 1024-frame queue and resubscribes (D14).
- `unseen` is set on every root turn end and nothing clears it until the TUI refactor uses
  `PATCH`; until then it is true for most sessions.
- Imported `last_activity_at` uses the log's mtime, which a copy or `touch` can change.
- `archie stats` run before the orchestrator ever starts performs the one-off import itself.

## Review

- Plan review: 1 pass. Fixed: the stop path and the empty-log case (D19, D19a, D19b, D21);
  D11 trailing flush; D22 timeout and other terminal statuses; D10 interrupted turns and
  backend from the model; D12 reworded plus the read source (D12a); D13 adoption without a
  log; D14 shutdown; D16 lifespan injection and test count; D17 proxy and viewer signatures;
  PATCH name validation; the metrics requirement's migration exemption; the `user_version`
  functions; pages in the no-Docker requirement; the dropped `Agent:` line; M1 split into M1
  and M2; Verify lines for the screenshot and docs grep; the live-viewer test pattern. No
  findings rejected. Two ❌ were fixed, so a second pass ran.
- Plan review pass 2: fixed the ❌ (lifespan tests would write the real `~/.nexus/archie.db`:
  autouse `ARCHIE_HOME_DIR` fixture); migrate before the metrics flock; terminal-write retry
  (D12a); restart the subscription after a failed stop; D8 strict mode and the import
  directory; D14 steady back-off; `starting` routes (D21); register-then-snapshot (D20);
  `Runtime.stop` reporting log removal; `stopping` → `stopped` at reconcile; "root" defined;
  the no-Docker AC patches `subprocess.run`; both metrics handlers named; the docs grep allows
  `/api/auth/status`; M1/M2 split of `sessions_page`, `proxy_status` and `live_ids()`. None
  rejected. Stopped after two passes as the skill requires.

## Outcome

PR: pending
CI: pending

## Follow-ups

- `model` and `prompt` on `POST /api/sessions` (roadmap item 5).
- Repo, worktree and branch detection from tool calls, and automatic naming from the worktree.
- The attach TUI and the future sidebar clearing `unseen` with `PATCH`.
- The prototype branch's `data.py` reads `metrics.db`; move it to the API when the TUI is
  refactored.

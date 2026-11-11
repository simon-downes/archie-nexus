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

## Review

## Outcome

PR: pending
CI: pending

## Follow-ups

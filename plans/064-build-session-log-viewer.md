# 064 — Session log viewer

Status: interviewing
Branch: build-it/064-session-log-viewer (base: main @ 02bfe1c)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/064-session-log-viewer
Updated: 2026-05-14

## Objective

There is no way to browse past sessions except by reading raw JSONL under
`~/.nexus/sessions/`. This build adds a read-only browser view to the orchestrator:
`/ui/sessions` lists every persisted session log (newest first) with workspace, models,
usage and an active marker, filtered by one search box; `/ui/sessions/<id>` renders the
transcript, and follows an active session live.

## Context

User request (paraphrased closely):

- `/ui/sessions` shows previous sessions, newest first, in a table: session id, workspace
  name, models used (ordered by turn count), usage (input, cache read/write, output token
  counts and total cost), and an active marker.
- One search input accepting `"quoted phrase" word before:2026-10-11 after:2026-07-11
  workspace:prefix-or-name`.
- A link to `/ui/sessions/<id>` showing the transcript in a structured, coloured view —
  not a JSON blob or a wall of text. Use the TUI as a guide, but don't mimic it.
- Active sessions receive live updates; the view is read-only.
- Stack: nimble.css, plain JavaScript, htmx. Other small focused libraries are fine; no
  React/Vue, no npm build step.
- Turn markdown must stay visually distinct from the interface chrome.
- The orchestrator probably can't run in this container: take care, test thoroughly.

Codebase facts found at intake:

- The orchestrator (`orchestrator/src/archie_orchestrator/`) is Starlette + Jinja2. `web.py`
  renders `/` (running sessions, meta-refresh) and `/ui/auth` from `templates/` with
  `static/style.css`. Tests drive the ASGI app with `httpx.ASGITransport`
  (`tests/test_web_auth_status.py`).
- Session logs are `<ARCHIE_HOME_DIR>/sessions/<session-id>.jsonl`, canonical events from
  `shared/src/archie_shared/events.py`, read with `SessionLog` (`shared/.../session/log.py`).
  `llm_request` is the only accounting source (CONTRIBUTING invariants).
- Session ids are `{workspace}-{10-char ULID timestamp}`; `split_id()` splits them.
- Running sessions come from `docker.list_sessions()` (shells out to `docker ps`).
- Live events: the agent WebSocket `/stream` sends `handshake`, `session_status`, then every
  public event including live-only `text_delta`; `/events?after=<id>` replays persisted
  history. The TUI's reconciliation is in `cli/src/archie_cli/tui/app.py`
  (`_render_canonical`).
- `shared/src/archie_shared/tool_summaries.py` formats tool summaries as Rich markup, meant
  for any presentation surface.
- Local data: 216 logs, 112 MB; the largest is 11 MB / 4,880 events. Decoding all logs takes
  ~0.8 s, one 11 MB log ~0.08 s.
- `markdown-it-py` 4.2 and `pygments` are already in `uv.lock` (via textual/rich).

## Baseline

- Check commands: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`
- Setup: `uv sync`
- Setup leftovers: none
- Failing before this work: none (1146 passed; ruff check and format clean)
- Not included: `persona/skills/aws-agent-access/`, `plans/060-spec-container-aws-profiles.md`
  (unrelated to this idea)

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|

## Requirements

## Design

## Milestones

## Change log

## Risks

## Review

## Outcome

PR: pending

## Follow-ups

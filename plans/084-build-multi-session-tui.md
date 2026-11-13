# 084 — Multi-session TUI

Status: planning
Branch: build-it/084-multi-session-tui (base: main @ b1edcf9)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/084-multi-session-tui

## Objective

The TUI shows one session per window, so working across several sessions means several
terminals and no view of what the others are doing. Replace it with a multi-session TUI:
a persistent sidebar listing every live session with its state, title and repo/branch,
and a session pane that switches between sessions, each with its transcript, in-pane
tool and subagent views, question panel, input and status bar. The orchestrator works out
each session's repo, worktree and branch so every client can show it. When done,
`archie start` and `archie attach` open the new TUI and the old single-session app is
gone.

## Context

The user's words (2026-11-12): "prototypes are designed to be thrown away not built on.
use the prototype as a guide for the outcome but build from main and make conscious
decisions about architecture, code placement, refactors, etc … we might be able to reuse
most of it, but consciously and with purpose not blind adoption." Decided with the user:
the new TUI replaces the old one; repo/branch detection lives in the orchestrator's session
tracker and may reuse the workspaces git reader; the new-session dialog only picks a
workspace (no model, first message or background options for now).

Reference for the intended outcome: branch `prototype/tui-workbench`
(`cli/src/archie_cli/tui/prototype/`, `plans/tui-tool-rows.md`) and brain
`projects/archie/nexus-roadmap/tui-prototype.md`, `tui-sidebar.md`.

Discovery (three read-only reviews, 2026-11-12) — facts on main @ b1edcf9:

- Current TUI (`cli/src/archie_cli/tui/`): `ArchieApp` (app.py, 1085 lines) owns one
  session's connection (`WSClient`), replay (`/events` with an `after` cursor, 409 → full
  refetch), `TranscriptBuilder`, accounting ledger, turn/question/shell state, and finds
  widgets with app-wide `query_one("#id")`. The widgets in `transcript_view.py` (windowed
  rows: ~200 mounted, pages of 80), `conversation.py` and `detail.py` have no `self.app`
  dependencies and can be instantiated per session; `models_provider.py` reads app state.
  Reconnect backs off up to 30 s; the first connect never retries. `!cmd` runs
  `docker exec` on the host (local profile only). External editor, copy block, ctrl+x stop
  child, turn status rows, quit-and-terminate exist. 168 tests in `tests/test_tui_*.py`;
  about half drive `ArchieApp` internals.
- Prototype verdicts: `app.py`, `data.py` (mocks, `metrics.db`) and the runtime class swap
  (`rows.install()` reassigns `transcript_view.ToolRow`/`UserMessage` process-wide) are
  discarded. Reusable with changes: `rows.py` (pure two-line row model; should use
  structured `ToolResult` fields and theme colours), `dialogs.py` frame (pane scope is only
  a visual trick on a `ModalScreen`), `commands.py` providers (duck-typed app), `panes.py`
  (crumbs and section bands; views don't follow live updates), `widgets.py` pieces
  (`SessionList` lays rows out by hand and calls `set_options` on every refresh).
  Fragile: duplicate-view race on fast switching; every view stays mounted; sync HEAD reads
  on the UI thread; overridden `App.notify`.
- Location: root `tool_call` events (persisted, same JSON on the stream) carry `name` and
  `input` (`shell` `command`/`cwd`, `read`/`edit`/`write`/`grep`/`glob`/`code` `path`);
  native and Kiro identical. 368 logs, 32k root calls. A prefiltered full scan of the
  largest log (11.5 MB) takes ~15 ms; a HEAD read ~0.23 ms. `workspaces._Reader` gives
  confined, untrusted-file git reads; `repo()` rejects linked worktrees, so path →
  worktree resolution is new code. The tracker has no config today.
- Server pieces already on main: `/api/sessions` + SSE feed (`snapshot`, `session` events),
  per-session `/stream` and `/events`, `PATCH name|unseen`, `DELETE`, `POST {workspace,
  backend, name}`, `/api/workspaces`, model titles (`name`/`name_source`).

## Baseline

- Check commands: `env -u ARCHIE_BACKEND -u ARCHIE_SESSION_ID scripts/dev test`,
  `scripts/dev ruff check .`, `scripts/dev ruff format --check <touched files>`
- Setup: `scripts/dev sync`
- Setup leftovers: none (`.venv-archie/` is ignored)
- Failing before this work: none (5192 passed; Ruff clean)
- Not included: the user's checkout's untracked `.DS_Store`,
  `plans/060-spec-container-aws-profiles.md`, `plans/070-build-nexus-plugins.md`

## Decisions

| #   | Decision | Chosen | Rejected | Why | Source |
|-----|----------|--------|----------|-----|--------|
| D1  | Old TUI | Replaced: `archie start`/`attach` open the new app; the old `ArchieApp` and its single-session pieces are deleted | ship alongside | One TUI to maintain | user |
| D2  | Repo/branch source | The orchestrator's tracker works it out and puts it on the session record | client reads logs and `.git/HEAD` (prototype) | Works for remote profiles and the web client | user |
| D3  | New-session dialog | Workspace (from `/api/workspaces`) and backend (Archie/Kiro, default: the on-screen session's backend, else Archie); no model, first message, name or background options | the prototype's full dialog | Future work adds the rest | user Q1 |
| D4  | Prototype | Reference for the outcome only; code is rewritten on main, adopting pieces deliberately (listed in Code structure) | port the prototype | User's instruction; its app, data layer and class swap are mock-shaped | user |
| D5  | Package layout | Rewrite `cli/src/archie_cli/tui/` in place: new `app.py`, `client.py`, `session.py`, `pane.py`, `sidebar.py`, `detail.py` (in-pane), `dialogs.py`, `commands.py`, `notices.py`; keep and adapt `transcript_view.py`, `conversation.py`, `input.py`, `question.py`, `status.py`, `theme.py`; delete `quit_screen.py`, `throbber.py`, `models_provider.py` | a new `tui2/` package beside the old | The old app is deleted (D1); a second package would leave dead code and duplicate imports | self |
| D6  | Per-session split | `SessionController` (no widgets): connection, replay cursor, `TranscriptBuilder`, ledger, turn/question/shell state, change listeners. `SessionPane` (widget): header, transcript, activity line, question panel, input, status bar, detail-view stack, bound to one controller | one class per session doing both (today's `ArchieApp`) | The controller can outlive its widgets; tests drive it without Textual | self |
| D7  | Hidden sessions | Only the on-screen session has a mounted pane. Controllers of visited sessions stay connected (LRU of 5); switching back mounts a new pane from the controller's builder with no refetch | keep every pane mounted (078: keypress cost grows with widget count); refetch on every switch (0.7 s+ on large logs) | Bounded DOM, instant return to recent sessions | self |
| D8  | Sidebar data | The SSE feed only; no stream per background session. Notices come from record transitions on the feed | a stream per live session | The feed already carries status, question, unseen, name, location | self |
| D9  | Connection lifecycle | A controller connects when its record is live and not `starting`; an ended record loads `/events` (log) with no stream. Stream loss retries with 0.5→8 s back-off for as long as the feed says the session is live; the feed reconnects forever with the same back-off and its snapshot re-syncs everything | today's 30 s give-up and "relaunch" | An orchestrator restart must not strand sessions | self |
| D10 | Two-line tool rows | Pure model in new `shared/src/archie_shared/tool_rows.py` (from `ToolNode`, using `status`, `duration_ms`, `result_lines`, `result_bytes` and the input; no tool-result regexes where a structured field exists), rendered by `transcript_view.ToolRow` (changed in place) | the prototype's runtime class swap; rows in `cli` only | Shared next to `tool_summaries.py` so the web client can reuse it (CONTRIBUTING: reusable formatting in shared); the old one-line row has no other user after D1 | self |
| D11 | Row spec | `plans/tui-tool-rows.md` brought from the prototype branch, corrected: a running row shows the spinner glyph (no elapsed time: `ToolNode` has no start time), failed shell rows show `exit N` and the last output line (no line count) | the prototype's row code as the spec | The doc is the reviewed spec; the code drifted from it | self |
| D12 | Detail views | In-pane widgets on a per-pane stack (`ToolDetailView`, `SubagentView`), fed the controller's changes so they follow live updates; Esc pops | today's `DetailScreen` modals; the prototype's static panes | Spec keeps the sidebar visible; live tools must update | self |
| D13 | Dialogs | `ModalScreen`s from a shared `Dialog` frame (adapted from the prototype's `dialogs.py`); session dialogs are placed over the pane, app dialogs over the window | an overlay layer inside the pane | Rename/Stop are brief; true pane-scoped focus handling adds complexity for no use | self |
| D14 | Session list widget | A `VerticalScroll` of `WorkspaceBand` and `SessionRow` widgets keyed by workspace/session ID and updated in place; each row lays itself out in `render()` from its own width | `OptionList` with hand-padded prompts and `set_options` on every refresh (prototype) | No full rebuilds, no width hacks | self |
| D15 | Animation | One app timer at 8 Hz that refreshes only working session rows and the active pane's activity line, running only while something is working | a timer per widget (today's `Throbber`) | One clock, idle when idle | self |
| D16 | Notices | A `NoticeStack` in the sidebar fed by `app.notice(title, text, severity)`; `App.notify` is not overridden | overriding `App.notify` (prototype) | Textual internals keep their own channel | self |
| D17 | `unseen` | PATCH `unseen: false` when a session with `unseen` is on screen: on switch and on a feed update while it is shown | clear on leaving (prototype) | The server sets `unseen` at every root turn end, including while watched | self |
| D18 | Entry points | `archie start` creates, waits as today, then opens the TUI on it; `archie attach [id]` opens it on the resolved session, or with no ID on the newest live session; no live sessions shows an empty pane prompting ctrl+n. One TUI window talks to one orchestrator profile | a separate `archie tui` command | Keeps the commands users know | self |
| D19 | Attach features kept | Reconnect (D9), `!` direct shell (local profile only; otherwise a client error row), external editor, copy, turn status rows, model switch, stop child (from the subagent view) | — | Parity | self |
| D20 | Attach features dropped | Subagent picker (subagents open from rows), quit-and-terminate (Quit closes the window; Stop is a dialog), the Footer (key hints live in the sidebar) | keep | Roadmap item 4: sidebar navigates, palette acts | self |
| D21 | Location fields | `SessionLocation {repo: str, worktree: str \| None, branch: str \| None, commit: str \| None, missing: bool}` in `shared/src/archie_shared/workspaces.py`; `SessionDescriptor.location: SessionLocation \| None`; stored as JSON in a `location` column (migration 3) | memory only | Ended sessions on screen and `?include=ended` read the store | self |
| D22 | Location algorithm | Candidates from root `tool_call`s: shell (`cwd`, default `/workspace`; `cd` targets followed in order; `git -C`), edit/write `path`, read/grep/glob/code `path`. Rank git-running shells, then edits/writes, then the rest, within the latest turn that has a resolvable candidate (newest wins), falling back to earlier turns. Subagent calls are ignored | the prototype's whole-history ranking | One early git call shouldn't outrank later work in another repo | self |
| D23 | When location is computed | On each root `ToolResult` of a candidate call (the command has run), on every stream connect (log scan, ~15 ms worst case), and a HEAD re-read every 30 s for live sessions with a location; all in a per-session coalescing background job off the event loop | at `tool_call` time; on feed reads | A `git worktree add X && cd X` path only exists after the call | self |
| D24 | Path → location | New public `workspaces.locate(workspace_dir, container_path) -> SessionLocation \| None` built on `_Reader` (confined reads): walk up to a `.git`; a dir is a main checkout, a file is a linked worktree resolved through its `commondir` back to the repo; a path inside a `.git` component whose worktree is gone reports `missing` | separate git code in the tracker | One confined reader for untrusted workspace files | self |
| D25 | Tracker config | `SessionTracker(..., workspace_root: Callable[[], Path] \| None)`, the lifespan passes `lambda: expand_workspace_root(config_source.latest())`; None turns location off | read config inside the tracker | Same injection pattern as the titler | self |
| D26 | Test seam | `ArchieApp(client=...)` where `client` implements an `OrchestratorClient` protocol; tests pass `FakeOrchestrator` (feed queue, per-session event lists and stream queues, recorded HTTP calls and commands) and drive the app with `run_test`. Controllers and the row model get unit tests | patching httpx/websockets | Deterministic app tests without a server | self |
| D27 | Theme | The Archie theme gains a `pane` variable (`#171717`) and the prototype's colours as named constants in `theme.py` | `dataclasses.replace` on the registered theme (prototype) | One source of colours | self |
| D28 | Web link | The sidebar info line opens the orchestrator's `/` page (profile `public_url`, else `http://host:port`) | `/ui/sessions` (prototype; removed in 081) | Current route | self |

## Requirements

Location (server)

- MUST add `location` (D21) to `SessionDescriptor`, store it in `archie.db` (migration 3,
  JSON column, existing rows null) and serve it on every `/api/sessions` route and the feed.
  - AC: store round-trip test; migration test 2→3 keeps rows and adds a null `location`.
- MUST work out a live session's location per D22–D24 and publish it when it changes.
  - AC: `tests/test_session_location.py`: git outranks a later edit in the same turn; a
    later turn's edit outranks an earlier turn's git; relative paths and `cwd`; `cd`
    chains in order; a bare git shell in a workspace that is a repo gives `repo: ""`;
    a linked worktree under `.git/build-it-worktrees/x` gives repo + worktree + branch;
    a deleted worktree gives `missing: true`; paths outside `/workspace` and subagent
    calls are ignored; detached HEAD gives `branch: null` and the commit.
  - AC: tracker tests: a root `tool_result` after an edit call publishes the location;
    a connect scans the log; a HEAD change is published by the sweep; files are read only
    through the confined reader (a symlinked HEAD outside the workspace gives no branch).

TUI

- MUST open the new TUI from `archie start` and `archie attach [id]` (D18) and remove the
  old app.
  - AC: CLI tests assert the new `ArchieApp` is constructed with the base URL and the
    focused session ID; `rg -n "quit_screen|throbber|models_provider" cli` finds nothing.
- MUST show every live session in the sidebar from the feed, grouped by workspace (bands
  sorted by name, sessions oldest first), two lines each: state glyph, name and age; then
  `⎇ repo · branch` (worktree name when set, `no repo` without a location). Ended sessions
  are hidden except the one on screen.
  - AC: app tests with a fake feed: grouping and order; a `session` event updates one row in
    place; an ended session disappears unless shown; glyphs per status, including
    `starting`, `stopping` and `failed`.
- MUST switch the pane between sessions by click, alt+↑/↓ and alt+1…9, keeping at most 5
  controllers and one mounted pane (D7).
  - AC: app test: switching A→B→A refetches A's events once in total; only one
    `SessionPane` exists; a sixth visited session evicts the least recent controller.
- MUST render a session's transcript from `/events` and its stream, send messages,
  interrupt with Esc, answer questions, show turn status rows and the status bar (model,
  tokens, context, cost) as today.
  - AC: ported controller tests (from `test_tui_canonical`, `test_tui_question`,
    `test_tui_streaming`) pass against `SessionController`/`SessionPane`.
- MUST render tool calls as two-line rows per `plans/tui-tool-rows.md` (D10, D11) and
  user messages with a bar and no header.
  - AC: `tests/test_tool_rows.py` covers every rule in the doc's per-tool table.
- MUST show the activity line (Thinking/Working/Responding, blank between turns but
  keeping its height) in place of the throbber.
  - AC: app test: the pane's height is the same idle and working.
- MUST open tool and subagent detail views inside the pane, follow live updates, and pop
  with Esc; the sidebar stays visible.
  - AC: app test: clicking a running tool row, then its result arriving, updates the open
    view; Esc returns to the transcript at the same scroll position.
- MUST clear `unseen` per D17 and post sidebar notices for feed transitions of other
  sessions: a turn ended with a reply, a question, a failed turn, an ended session.
  - AC: app tests with the fake client: PATCH sent when shown; one notice per transition;
    none for the on-screen session.
- MUST survive an orchestrator restart (D9).
  - AC: app test: the fake feed and stream drop, then come back; the pane resumes with an
    `after` replay and no "relaunch" message.
- MUST provide New session (workspace + backend, D3), Rename, Stop, Quit (closes the
  window, sessions keep running) and Keys dialogs, the action palette (New session, Rename,
  Change model, Compact (native sessions only), Stop, Open web interface, Keyboard
  shortcuts, Quit; session actions hidden for ended sessions) and the model picker (alt+m).
  - AC: app tests: each dialog's request (POST body, PATCH body, DELETE) on the fake client;
    palette lists for a Kiro, a native and an ended session.
- MUST keep `!` direct shell (local profile only), the external editor and copy (D19).
  - AC: ported `test_tui_shell`, `test_tui_copy_paste`; a remote-profile `!` shows a client
    error row.

Out of scope: model, first message, name and background options when creating a session;
session browser for ended sessions; web client changes; title display rules beyond `name`;
auth.

## Design

### Overview

Server first: the tracker gains a location detector that feeds `SessionDescriptor.location`.
Then the TUI is rebuilt around three layers: an `OrchestratorClient` (HTTP, SSE feed,
WebSocket streams), per-session `SessionController`s (state and connection, no widgets),
and the app's widgets (sidebar from the feed; one `SessionPane` bound to the on-screen
controller).

### Stack

- Reused: Textual, httpx (SSE via `httpx.AsyncClient.stream`), websockets (`WSClient`),
  `archie_shared.transcript`. No new dependencies.

### Code structure

Server:
- `shared/src/archie_shared/workspaces.py`: `SessionLocation`.
- `shared/src/archie_shared/session/descriptor.py`: `location` field.
- `orchestrator/src/archie_orchestrator/workspaces.py`: `locate()` (+ `read_head` helper for
  the sweep), on `_Reader`.
- `orchestrator/src/archie_orchestrator/location.py` (new): candidate extraction from
  `ToolCall` inputs (shell parsing, path normalisation), `LocationTracker` per session
  (candidates by turn and rank, pending calls by `tool_use_id`), `scan_log(path)`.
- `orchestrator/src/archie_orchestrator/tracker.py`: `workspace_root` callable; feeds
  `ToolCall`/`ToolResult` frames to the session's `LocationTracker`; location jobs
  (pattern: `request_title`/`_title_job`); log scan in `_follow` on connect; 30 s sweep task.
- `orchestrator/src/archie_orchestrator/store.py`: migration 3, `location` column.
- `orchestrator/src/archie_orchestrator/app.py`: lifespan wiring.

TUI (`cli/src/archie_cli/tui/`):
- `client.py` (new): `OrchestratorClient` protocol and `HttpOrchestrator` (feed SSE parser,
  `events(session_id, after)`, `stream(session_id)` returning a `WSClient`, `patch`,
  `delete`, `create`, `workspaces`).
- `session.py` (new): `SessionController` — state moved from today's `ArchieApp`
  (connection, replay, `_render_canonical` logic, ledger/accounting, question, turn
  state, direct shell), emitting typed updates to listeners (the pane).
- `pane.py` (new): `SessionPane`, `SessionHeader`, `ActivityLine`, the view stack.
- `sidebar.py` (new): `Sidebar` (banner, info line, filter, `SessionList` of
  `WorkspaceBand`/`SessionRow`, notices, key hints).
- `notices.py` (new): `NoticeStack`.
- `detail.py` (rewritten): `ToolDetailView`, `SubagentView` widgets with crumb line and
  section bands (adapted from the prototype's `panes.py`).
- `dialogs.py` (new, adapted from the prototype): `Dialog`, `ConfirmDialog`,
  `RenameDialog`, `NewSessionDialog` (workspace + backend), `KeysDialog`, `QuitDialog`.
- `commands.py` (new, adapted from the prototype): `SessionCommands`, `ModelCommands`
  over a typed `AppActions` protocol.
- `app.py` (rewritten): `ArchieApp(client, profile_label, focus=None)`: layout, feed loop,
  controllers LRU, switching, actions, animation timer.
- `transcript_view.py`: `ToolRow` becomes the two-line row; `UserMessage` styling in
  `conversation.py`.
- `status.py`: no version; session ID only when named.
- `theme.py`: `pane` variable and colour constants; `archie.tcss` rewritten for the layout.
- Deleted: `quit_screen.py`, `throbber.py`, `models_provider.py`, the old `DetailScreen`s.
- `shared/src/archie_shared/tool_rows.py` (new): row model.
- `plans/tui-tool-rows.md`: spec (D11).
- `cli/src/archie_cli/cli.py`: `start`/`attach` construct the new app.
- Tests: new `tests/test_session_location.py`, `tests/test_tool_rows.py`,
  `tests/test_tui_app.py` (sidebar, switching, dialogs, palette, notices, reconnect),
  `tests/tui_fakes.py` (`FakeOrchestrator`); ported/rewritten `test_tui_canonical`,
  `test_tui_question`, `test_tui_detail`, `test_tui_streaming`, `test_tui_subagents`,
  `test_tui_shell`, `test_tui_copy_paste`, `test_tui_window`; deleted `test_tui_quit`,
  `test_tui_models_provider`; CLI tests updated.

### Data model

`SessionLocation`: `repo: str` (relative to the workspace, `""` = the workspace itself),
`worktree: str | None` (relative path), `branch: str | None`, `commit: str | None`,
`missing: bool = False`. `sessions.location TEXT NULL` (JSON), migration 3.

### Data flow

1. Start: `ArchieApp` opens the feed; the snapshot builds the sidebar; the focused (or
   newest live) session's controller is created and its pane mounted.
2. Controller: if the record is live and not starting, GET `/events` (full on first load,
   `after` cursor afterwards), connect the stream, buffer frames until the replay lands,
   then apply; if ended, GET `/events` only.
3. Feed `session` event: update the sidebar row; if it is the on-screen session, update the
   header and clear `unseen` (D17); for others, compare with the previous record and post
   a notice. A record leaving `starting` lets a waiting controller connect.
4. Switch: the old pane is removed; the controller stays (LRU); the new pane mounts from
   its controller's builder.
5. Failures: feed drop → retry with back-off, snapshot re-syncs; stream drop → controller
   retries while the record is live; `/events` 409 → full reload of that session;
   HTTP errors → a client error row in the pane or a notice.

### Non-functional

- Switching to a visited session mounts from memory (target < 300 ms on the 11 MB
  session, measured in M3); a new session's first load is today's replay cost.
- One 8 Hz timer at most (D15); no per-row timers.
- Location work runs off the event loop; the 30 s sweep reads one HEAD per live session.

### Config / infra

- `archie.db` migration 3 on the next orchestrator start.

## Milestones

### [ ] M1 — Sessions carry their repo, worktree and branch

- **Approach:** D21–D25. `workspaces.locate` on `_Reader` (follow `_gitdir_from_file`,
  `_head`, `_resolve`; add `commondir` handling for worktrees). `location.py` with the
  prototype's `location.py` as the algorithm reference (`git show
  prototype/tui-workbench:cli/src/archie_cli/tui/prototype/location.py`) plus D22's
  changes. Tracker: location jobs like `request_title` (coalescing, `_background`), scan on
  connect, sweep task started in `start()`, cancelled in `close()`. Migration 3 like
  migration 2. Docs: architecture record table, a "Session location" paragraph, datastore.
  ⚠️ Resolve on `ToolResult`, not `ToolCall`. ⚠️ All reads through `_Reader`.
- **Test seam:** `workspaces.locate` and `location` functions with real git repos in
  `tmp_path`; `SessionTracker._on_frame` with frames; store migration.
- **Deliverable:** `GET /api/sessions` shows `location` for a session that edited a repo.
- **Verify:** `scripts/dev test tests/test_session_location.py tests/test_session_store.py
  tests/test_session_tracker.py` pass; full suite; a scan of 10 real logs (worktree copy of
  `/workspace` as the archie workspace) prints a location per session.
- **Outcome:**

### [ ] M2 — The new TUI lists sessions and shows the selected one

- **Approach:** `client.py`, `session.py`, `pane.py`, `sidebar.py`, `app.py` per Code
  structure. Move state and logic from today's `app.py` into `SessionController` (keep
  method names where tests rely on them). Pane uses existing `Conversation`, `MessageInput`,
  `QuestionPanel`, `StatusBar`; tool rows unchanged in this milestone. Wire `start`/`attach`.
  Port the controller-level tests; delete the old app and tests that only covered it.
  ⚠️ Scope every widget query to the pane. ⚠️ Replay must yield between chunks so the
  sidebar stays responsive.
- **Test seam:** `ArchieApp(client=FakeOrchestrator)` with `run_test`; `SessionController`.
- **Deliverable:** `archie attach` shows the sidebar of live sessions and the selected
  session's live transcript; switching works; messages send.
- **Verify:** `scripts/dev test tests/test_tui_app.py tests/test_tui_canonical.py
  tests/test_tui_question.py tests/test_cli_attach.py tests/test_cli_start_stop.py` pass;
  full suite.
- **Outcome:**

### [ ] M3 — Two-line tool rows, activity line and the pane's look

- **Approach:** D10, D11, D15, D27. `tool_rows.py` model; `ToolRow` renders it;
  `UserMessage` bar; `ActivityLine` replaces `Throbber`; header badge; status bar changes;
  `archie.tcss` and theme per the prototype's values (prototype `prototype.tcss` as
  reference). Measure switch time on the largest log.
- **Test seam:** `tool_rows` functions; `run_test` for the activity line height.
- **Deliverable:** the pane looks like the prototype.
- **Verify:** `scripts/dev test tests/test_tool_rows.py tests/test_tui_app.py` pass; full suite.
- **Outcome:**

### [ ] M4 — Tool and subagent views inside the pane

- **Approach:** D12. `detail.py` widgets fed by the controller's changes; stack on the pane;
  stop child from the subagent view.
- **Test seam:** `run_test` with the fake client.
- **Deliverable:** clicking a row opens a live-updating view; Esc returns.
- **Verify:** `scripts/dev test tests/test_tui_detail.py tests/test_tui_subagents.py` pass;
  full suite.
- **Outcome:**

### [ ] M5 — Notices, unseen and reconnect

- **Approach:** D9, D16, D17. Feed transitions → notices; PATCH unseen; feed and stream
  reconnect.
- **Test seam:** `run_test` with a fake client that drops and restores connections.
- **Deliverable:** background sessions announce replies and questions; a restart recovers.
- **Verify:** `scripts/dev test tests/test_tui_app.py -k "notice or unseen or reconnect"`
  pass; full suite.
- **Outcome:**

### [ ] M6 — Dialogs, palette and the model picker

- **Approach:** D3, D13, D28. `dialogs.py`, `commands.py`; filter box; web link; keys.
- **Test seam:** `run_test` with the fake client.
- **Deliverable:** sessions can be created, renamed, stopped; the palette acts on the
  on-screen session.
- **Verify:** `scripts/dev test tests/test_tui_app.py -k "dialog or palette or filter"` pass;
  full suite.
- **Outcome:**

### [ ] M7 — Direct shell, editor, copy and docs

- **Approach:** D19. Move `!` shell and editor into the controller/pane (editor must not
  leave other connections dead: reconnect every controller after it returns); docs
  (`README.md`, `docs/architecture.md` TUI sections).
- **Test seam:** ported shell and copy tests.
- **Deliverable:** parity with the old TUI's remaining features.
- **Verify:** full suite; Ruff; a manual run against the live orchestrator from this
  container is not possible (no host access) — recorded as not verified.
- **Outcome:**

## Deviations

## Risks

- The TUI can't be run against the user's real orchestrator from here; app behaviour is
  verified with the fake client and `run_test` only.
- Moving `ArchieApp` logic into a controller may change subtle replay/reconnect behaviour
  covered only by tests that drive internals.
- Location heuristics (shell parsing) will sometimes pick the wrong repo.

## Review

## Outcome

PR: pending
CI: pending

## Follow-ups

- Model, first message and background options in the new-session dialog (roadmap item 5).
- A session browser for ended sessions.
- Web client parity (roadmap item 6).

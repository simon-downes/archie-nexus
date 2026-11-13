# 084 — Multi-session TUI

Status: building
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
| D3  | New-session dialog | Workspace (from `/api/workspaces`) and backend (Archie/Kiro, default: the on-screen session's backend, else Archie). After the 202 the TUI switches to the new session; its pane shows "Starting…" until the record leaves `starting` (or shows the stored `error` for `failed`) | the prototype's full dialog; staying on the current session | Future work adds model/first message; switching is what `archie start` does | user Q1 + self |
| D4  | Prototype | Reference for the outcome only; code is rewritten on main, adopting pieces deliberately (listed in Code structure) | port the prototype | User's instruction; its app, data layer and class swap are mock-shaped | user |
| D5  | Package layout | Rewrite `cli/src/archie_cli/tui/` in place: new `client.py`, `session.py`, `pane.py`, `sidebar.py`, `notices.py`, `dialogs.py`, `commands.py`; rewritten `app.py`, `detail.py`; kept and adapted `transcript_view.py`, `conversation.py`, `input.py`, `question.py`, `status.py`, `theme.py`; deleted `quit_screen.py`, `throbber.py`, `models_provider.py` | a new `tui2/` package beside the old | The old app is deleted (D1); a second package leaves dead code | self |
| D6  | Controller ↔ pane contract | `SessionController` (no widgets) owns: connection, replay cursor, `TranscriptBuilder`, a `timeline` (ordered list of root entries: model nodes from the builder's root appends and local entries — turn status, client error, shell output — as they happen), accounting and status-bar values, question, turn state and activity word, direct-shell state, input draft and history. Local entries record an anchor: the id of the last root node when they were added (None before any). It notifies at most one attached `SessionListener` (the pane) through: `on_changes(changes)` (builder changes; the pane forwards them to its transcript and open detail views), `on_local(entry)`, `on_status()`, `on_turn()`, `on_question()`, `on_loaded()` (the first replay landed) and `on_reset()` (a 409 rebuilt the builder and cleared accounting; the pane drops its rows, pops its detail views and re-renders). `SessionPane.attach(controller)` renders the current state, then receives deltas: `TranscriptView.load(builder, locals)` rebuilds model rows from the builder's current root nodes in order (replaced streams and removed rows are already resolved there, and turns carry their items) and inserts each local entry after its anchor (at the end when the anchor is gone); status, question and turn come from the controller. `detach()` saves the input draft into the controller | emitting widget calls from the controller; the pane re-deriving state from events | One owner per piece of state; a newly mounted pane is a pure function of the controller; tests assert controller state and a recording listener instead of patching `query_one` | self |
| D7  | Hidden sessions | Only the on-screen session has a mounted pane. Controllers of visited sessions stay connected (LRU of 5; the on-screen controller and any controller with a running `!` shell or an answer in flight are never evicted). Switching back mounts a new pane from the controller (D6) with no refetch; the pane opens at the latest rows, with the input draft restored and the question panel at its recommended option | every pane mounted (078: keypress cost grows with widget count); refetch on switch (0.7 s+ on large logs) | Bounded DOM; instant return; client rows survive | self |
| D8  | Sidebar data | The sidebar and notices read only the SSE feed; streams exist only for controllers (D7) | a stream per live session for the sidebar | The feed carries status, question, unseen, name and location | self |
| D9  | Connection lifecycle | A controller connects when its record is live and not `starting`; an ended record loads `/events` (log) with no stream. Stream loss retries with 0.5→8 s back-off while the feed says the session is live; a 4004 close or an ended record stops retrying. The feed reconnects forever with the same back-off and its snapshot re-syncs every row and controller. The on-screen pane shows one "Connection lost; reconnecting…" client row per outage; background controllers retry silently | today's 30 s give-up and "relaunch"; a notice per attempt | An orchestrator restart must not strand sessions. M1 keeps today's stop rule (4004 or a 30 s give-up) because the feed arrives in M2; M6 replaces it. After a feed reconnect, an on-screen session missing from the snapshot (it ended meanwhile) is fetched with `GET /api/sessions/{id}`. A feed with no bytes for 40 s (the server pings after 15 s of silence) counts as dead | self |
| D10 | Switching | Switches are serialised (an `asyncio.Lock` in the app) and carry a generation number; a switch whose number is stale when it gets the lock does nothing. Detach/attach are synchronous on the event loop, so a controller still replaying keeps building and the next attached pane renders its state | the prototype's unguarded `switch_to` | Fixes the duplicate-view race | self |
| D11 | Two-line tool rows | Pure model in new `shared/src/archie_shared/tool_rows.py` (from `ToolNode`: `status`, `duration_ms`, `result_lines`, `result_bytes` and the input; result text only where no structured field exists), rendered by `transcript_view.ToolRow` (changed in place) | the prototype's runtime class swap; rows in `cli` only | Precedent: `shared/src/archie_shared/tool_summaries.py` and `transcript.tool_headline` already format tool calls for both clients; the old one-line row has no other user after D1 | self |
| D12 | Row spec | `plans/tui-tool-rows.md` brought from the prototype branch, with: a running row shows the animated spinner glyph and no elapsed time (`ToolNode` has no start time); failed shell rows show `exit N` and the last output line, no line count; metrics stay left-aligned on line 1 (the doc's open question); no `description` input is added, so shell rows use `$ <first command line>` and exec rows `python · N lines` | the prototype's row code as the spec | The reviewed doc is the spec; the code drifted | self |
| D13 | Detail views | In-pane widgets on a per-pane stack (`ToolDetailView`, `SubagentView`), fed `on_changes` so they follow live updates; Esc pops | today's `DetailScreen` modals; the prototype's static panes | Spec keeps the sidebar visible; live tools must update | self |
| D14 | Dialogs | `ModalScreen`s from one `Dialog` frame (adapted from the prototype's `dialogs.py`); session dialogs placed over the pane, app dialogs over the window | an overlay layer inside the pane | Rename/Stop are brief; pane-scoped focus adds complexity for no use | self |
| D15 | Session list widget | A `VerticalScroll` of `WorkspaceBand` and `SessionRow` widgets keyed by workspace/session ID and updated in place; each row lays itself out in `render()` from its own width | `OptionList` with hand-padded prompts and `set_options` per refresh (prototype) | No full rebuilds or width hacks | self |
| D16 | Animation | One app timer at 8 Hz, running only while something animates, plus one 30 s timer that redraws sidebar ages; widgets that animate (working/starting/stopping session rows, the activity line, running tool and subagent rows in the mounted pane) register while they animate and are refreshed on each tick | a timer per widget (today's `Throbber` uses `auto_refresh = 1/15`, `throbber.py:115`) | Animated widgets in several places need one clock that stops when nothing animates | self |
| D17 | Notices | A `NoticeStack` in the sidebar fed by `app.notice(title, text, severity)`; `App.notify` is not overridden | overriding `App.notify` (prototype) | Textual internals keep their own channel | self |
| D18 | `unseen` | PATCH `unseen: false` when a session with `unseen` is on screen: on switch and on a feed update while shown | clear on leaving (prototype) | The server sets `unseen` at every root turn end, including while watched | self |
| D19 | Entry points | `ArchieApp(client: OrchestratorClient, *, profile: ProfileInfo, focus: str \| None)`, `ProfileInfo(label, web_url, local)`. `archie start` creates, waits as today, then opens it focused on the new session; `archie attach [id]` focuses the resolved session (prefix match; several matches still pick by number), and with no ID the newest live session; no live sessions shows an empty pane prompting ctrl+n. One window talks to one orchestrator profile. `archie stop` keeps its picker | a separate `archie tui` command | Keeps the commands users know | self |
| D20 | Local profile | `local` is true for the default profile with no `ARCHIE_HOST`, the rule `archie shell` uses (`cli.py:545-563`) | host is a loopback address (breaks `host: 0.0.0.0`, which the `serve` docstring recommends) | `!` runs `docker exec` on the client's machine, which only reaches containers of a local orchestrator | self |
| D21 | Attach features kept | Reconnect (D9), `!` direct shell (only when `local`; otherwise a client error row), external editor (afterwards the feed and every controller reconnect, since `suspend()` stalls them all), copy, turn status rows, model switch, stop child (from the subagent view), the protocol-version warning (a client error row) | — | Parity | self |
| D22 | Attach features dropped | Subagent picker (subagents open from rows), quit-and-terminate (Quit closes the window; Stop is a dialog), the Footer (key hints live in the sidebar), the roadmap's "toggle sidebar" action (the layout always shows it) | keep | Roadmap item 4: sidebar navigates, palette acts | self |
| D23 | Keys | alt+↑/↓ and alt+1…9 switch sessions; tab/shift+tab move between filter, list, transcript and input; ctrl+f focuses the filter; enter/shift+enter send/new line; ctrl+n new session; alt+m change model; ctrl+p palette (Rename, Stop, Compact and Open web interface are palette-only); ctrl+c copy; ctrl+g editor; f1 keys; ctrl+q quit; ctrl+x stops the child shown in an open subagent view. Esc, first match wins: close the open dialog; kill a running `!` shell; pop a detail view; dismiss the question; interrupt a running turn; a second Esc within 0.5 s clears the input | the prototype's list without precedence | Navigation before interruption (no dismissing a question from behind a detail view); today's 0.5 s and order for the rest | self |
| D24 | Compact | Palette action sends `/compact` as a message (`agent/.../compaction.py:62`); hidden for Kiro sessions | a new command | No compact command exists | self |
| D25 | Location fields | `SessionLocation {repo: str, worktree: str \| None, branch: str \| None, commit: str \| None, missing: bool = False}` in `shared/src/archie_shared/workspaces.py`; `SessionDescriptor.location: SessionLocation \| None = None`; paths in `repo` and `worktree` are relative to the workspace (`""` = the workspace itself), or absolute host paths when outside it, as `Repo.path`/`Worktree.path`; stored as JSON in a `location` column (migration 3; `store.COLUMNS`, `_column_value` and `_from_row` like `question`) | memory only | Ended sessions on screen and `?include=ended` read the store | self |
| D26 | Location algorithm | Candidates from root `tool_call`s: shell (`cwd`, default `/workspace`; `cd` targets followed in order, stopping at one with `$`, `~` or a backtick; `git -C`), edit/write `path`, read/grep/glob/code `path`. Rank git-running shells, then edits/writes, then the rest, within the latest turn that has a resolvable candidate (newest wins), falling back to earlier turns. Subagent calls are used only when the root has no resolvable candidate | the prototype's whole-history ranking; counting subagent calls equally | One early git call shouldn't outrank later work in another repo; subagents are a third of calls and often search elsewhere, but some sessions only touch repos through them | self |
| D27 | When location is computed | On each root `ToolResult` whose `tool_use_id` matches a candidate call (and a scoped one while the root has no resolvable candidate) (the command has run), on every stream connect (log scan, ~15 ms worst case), and a HEAD re-read every 30 s for live sessions with a location; all in a per-session coalescing background job off the event loop (pattern: `request_title`/`_title_job`, guarded by `except Exception`) | at `tool_call` time; on feed reads | A `git worktree add X && cd X` path only exists after the call | self |
| D28 | Path → location | New public `workspaces.locate(workspace_dir, container_path) -> SessionLocation \| None` on `_Reader`: walk up to a `.git`; a dir is a main checkout, a file a linked worktree resolved through `commondir` back to its repo; a path inside a `.git` component whose worktree checkout is gone gives the worktree from `.git/worktrees/<name>` with `missing: true`; a workspace that no longer resolves gives None | separate git code in the tracker | One confined reader for untrusted workspace files | self |
| D29 | Tracker config | `SessionTracker(..., workspace_root: Callable[[], Path] \| None = None)`; the lifespan passes `lambda: expand_workspace_root(config_source.latest())`; None turns location off | read config inside the tracker | Same injection as the titler | self |
| D30 | Sidebar row content | Line 1: glyph, `name`, age since `last_activity_at` (else `started_at`) as `now/45s/12m/3h/5d`. Line 2: `⎇ <repo name> · <branch>` where repo name is the last part of `location.repo` (the workspace name when `repo` is `""`), branch is `branch`, else the commit's first 7 characters, else `?`; `no repo` without a location. Glyphs: starting ◌ muted (animated), working spinner primary, blocked `?` warning, error `✕` error, idle `●` success (bold name when `unseen`), stopping ◌ muted (animated), stopped/exited `·` muted, failed `✕` muted | showing the worktree name | Names are titles now; the branch identifies the worktree | self |
| D31 | Status bar | Model, tokens, context, cost; the branch comes from the record's location (not `SessionStatus.git_branch`); session ID only when the session has a non-ID name; no version | today's bar | One branch source | self |
| D32 | Test seam | `ArchieApp(client=FakeOrchestrator(...))` with `run_test`; `FakeOrchestrator` (in `tests/tui_fakes.py`) implements `OrchestratorClient`: a feed queue, per-session event lists and stream queues, recorded HTTP calls and commands, and connection drops. Controllers are tested with a recording `SessionListener`; the row model with plain unit tests | patching httpx/websockets | Deterministic app tests without a server | self |
| D33 | Theme | The Archie theme gains a `pane` variable (`#171717`); the prototype's colours become named constants in `theme.py` | `dataclasses.replace` on the registered theme (prototype) | One source of colours | self |
| D34 | Web link | The sidebar info line opens the orchestrator's `/` page (`ProfileInfo.web_url`: `public_url`, else `http://host:port`) | `/ui/sessions` (prototype; removed in 081) | Current route | self |

## Requirements

Controller (prefactor)

- MUST move per-session state and behaviour out of today's `ArchieApp` into
  `SessionController` per D6 without changing what the user sees in the single-session app.
  - AC: ported tests in `tests/test_tui_canonical.py`, `test_tui_question.py`,
    `test_tui_streaming.py`, `test_protocol_version.py`, `test_kiro_translate.py` (TUI part)
    and `test_orchestrator_bounce.py` (reconnect, with the 4004 rule) assert on the
    controller and a recording listener; the full suite passes.

TUI

- MUST open the new TUI from `archie start` and `archie attach [id]` per D19 and remove the
  old app.
  - AC: CLI tests (`test_cli_attach.py`, `test_cli_start_stop.py`, `test_cli_profiles.py`)
    assert `ArchieApp` gets an `HttpOrchestrator` for the profile URL, the right
    `ProfileInfo` and the focused ID; `test_attach_no_sessions` now expects the app with no
    focus; `rg -n "quit_screen|throbber|models_provider|DetailScreen" cli tests` finds nothing.
- MUST show every live session in the sidebar from the feed, grouped by workspace (bands
  sorted by name with a count, sessions oldest first by `started_at`), each per D30. Ended
  sessions are hidden except the one on screen.
  - AC: app tests with the fake feed: grouping and order; a `session` event updates one row
    in place; an ended session disappears unless shown; one glyph per status in D30; line 2
    for repo, workspace-as-repo, detached HEAD and no location.
- MUST switch sessions by click, alt+↑/↓ and alt+1…9 per D7 and D10.
  - AC: app tests: A→B→A fetches A's full `/events` once; exactly one `SessionPane` exists
    after rapid switches; a client error row and the input draft survive a switch away and
    back; a sixth visited session evicts the least recent evictable controller.
- MUST render the transcript from `/events` and the stream, send messages, interrupt,
  answer questions, show turn status rows and the status bar per D31.
  - AC: app tests drive a fake session through a turn with a tool call, a question and an
    answer; the status bar shows the location's branch.
- MUST render tool calls as two-line rows per D11/D12 and user messages with a bar and no
  header.
  - AC: `tests/test_tool_rows.py` covers every row in the spec's per-tool table; a widget
    test asserts a user message has no "You" header.
- MUST show the activity line (Thinking/Working/Responding; blank between turns but
  keeping its height) in place of the throbber.
  - AC: app test: the activity line has the same height idle and working, and its word
    follows `iteration_start` → `text_delta` → `tool_call`.
- MUST open tool and subagent views inside the pane per D13; Esc follows D23.
  - AC: app test: open a running tool's view, deliver its result, the view updates; Esc
    with a view open during a running turn pops the view and sends no interrupt.
- MUST clear `unseen` per D18 and post notices for other sessions' feed transitions: a root
  turn ended (`unseen` set), a question (`blocked`), a failed turn (`error`), an ended
  session.
  - AC: app tests: PATCH sent when shown; one notice per transition; none for the on-screen
    session.
- MUST survive an orchestrator restart per D9.
  - AC: app test: feed and stream drop, then return; the pane resumes with an `after` replay,
    one "reconnecting" row, no "relaunch" text.
- MUST provide New session (D3), Rename, Stop, Quit (closes the window; sessions keep
  running) and Keys dialogs; the palette (New session, Rename, Change model, Compact per D24,
  Stop, Open web interface, Keyboard shortcuts, Quit; Rename, Change model, Compact and Stop
  hidden for ended sessions); the model picker (alt+m); the sidebar filter (name, ID,
  workspace, repo, branch).
  - AC: app tests: POST body (workspace, backend) and the switch to the new session; PATCH
    and DELETE bodies; palette lists for a Kiro, a native and an ended session; filter
    matches by branch.
- MUST keep `!` direct shell (D20, D21), the external editor (reconnecting the feed and all
  controllers afterwards) and copy.
  - AC: ported `test_tui_shell`, `test_tui_copy_paste`; a non-local profile's `!` shows a
    client error row; after the editor returns, the fake client sees a feed reconnect.

Location (server)

- MUST add `location` (D25) to `SessionDescriptor`, store it (migration 3) and serve it on
  every `/api/sessions` route and the feed.
  - AC: store round-trip; migration 2→3 keeps rows with a null `location`.
- MUST work out a live session's location per D26–D28 and publish changes.
  - AC: `tests/test_session_location.py`: git outranks a later edit in the same turn; a
    later turn's edit outranks an earlier turn's git; relative paths and `cwd`; `cd` chains
    in order; a bare git shell in a workspace that is a repo gives `repo: ""`; a worktree
    under `.git/build-it-worktrees/x` gives repo, worktree and branch; a deleted worktree
    gives `missing: true`; paths outside `/workspace` are ignored; subagent calls count only
    when the root has none; detached HEAD gives `branch: null` and the commit; a HEAD
    symlinked outside the workspace gives no branch.
  - AC: tracker tests: a root `tool_result` after an edit publishes the location; a
    connect scans the log; the sweep publishes a HEAD change; a failing location job is
    logged and the stream keeps being followed.

Out of scope: model, first message, name and background options when creating a session;
a session browser for ended sessions; web client changes; auth.

## Design

### Overview

The current app's per-session logic first moves into `SessionController` behind the
existing single-session UI. The new app then puts a feed-driven sidebar beside one
`SessionPane` bound to the on-screen controller. Later slices add the row design, in-pane
views, notices and reconnect, dialogs and palette, and parity features. The tracker gains
per-session location, which the sidebar and status bar show.

### Stack

- Reused: Textual, httpx (SSE via `AsyncClient.stream`), websockets (`WSClient`),
  `archie_shared.transcript`. No new dependencies.

### Code structure

TUI (`cli/src/archie_cli/tui/`):
- `client.py` (new): `OrchestratorClient` protocol, `HttpOrchestrator` (SSE feed parser,
  `events(id, after)`, `stream(id)` → `WSClient`, `patch`, `delete`, `create`,
  `workspaces`), `ProfileInfo`.
- `session.py` (new): `SessionController`, `SessionListener` protocol, timeline entries.
  Logic moves from today's `app.py`: `_replay_events`, `_render_canonical`,
  `_accumulate_ledger`/`_update_accounting_status`, `_receive_loop`/`_reconnect`, question
  and answer, `_end_turn`/turn metrics, `_run_direct_shell`, cancel logic.
- `pane.py` (new): `SessionPane` (attach/detach, view stack), `SessionHeader`, `ActivityLine`.
- `sidebar.py` (new): `Sidebar` (banner and info line from the prototype's `app.py`
  constants and `web_target`, filter, `SessionList` with `WorkspaceBand`/`SessionRow`, key
  hints), `age()`.
- `notices.py` (new): `NoticeStack`/`Notice` (adapted from the prototype's `widgets.py`).
- `detail.py` (rewritten): `ToolDetailView`, `SubagentView` with the crumb line and section
  bands from the prototype's `panes.py`, following `on_changes`.
- `dialogs.py` (new, adapted from the prototype): `Dialog`, `ConfirmDialog`, `RenameDialog`,
  `NewSessionDialog`, `KeysDialog`, `QuitDialog`.
- `commands.py` (new, adapted from the prototype): `SessionCommands`, `ModelCommands` over a
  typed `AppActions` protocol; the price text moves from `models_provider._cost_summary`
  into `commands.py`.
- `app.py` (rewritten): `ArchieApp` per D19: layout, feed loop, controller LRU, switching
  (D10), animation timer (D16), actions and keys (D23).
- `transcript_view.py`: `TranscriptView.load(builder, locals)` (D6); `ToolRow` renders the two-line
  model; `conversation.UserMessage` drops its header.
- `status.py`, `theme.py`, `archie.tcss` per D31, D33.
- Deleted: `quit_screen.py`, `throbber.py`, `models_provider.py`, `DetailScreen` classes.
- `cli/src/archie_cli/cli.py`: `start`/`attach` build `HttpOrchestrator` + `ProfileInfo`.

Shared: `shared/src/archie_shared/tool_rows.py` (new); `workspaces.SessionLocation`;
`session/descriptor.py` `location`. Spec: `plans/tui-tool-rows.md` (D12).

Server: `orchestrator/.../workspaces.py` `locate()` and a HEAD re-read helper;
`orchestrator/.../location.py` (new: candidate extraction, per-session `LocationState`,
`scan_log`); `tracker.py` (D27, D29); `store.py` migration 3; `app.py` lifespan.

Tests and when they change:
- M1 (controller): rewrite the state assertions of `test_tui_canonical.py`,
  `test_tui_question.py`, `test_protocol_version.py`, `test_kiro_translate.py` (TUI part) and
  `test_orchestrator_bounce.py` (reconnect part) against the controller and a recording
  listener; widget assertions (input disabled, panel text, streaming fragments in
  `test_tui_streaming.py`) stay on the host app. The thin host keeps temporary forwarding
  shims (`_ws`, `_apply_event`, `_replay_events`, `_update_accounting_status`,
  `_estimate_context_pct`) so `test_tui_detail.py`, `test_tui_subagents.py`,
  `test_tui_quit.py`, `test_tui_models_provider.py` and `test_tui_window.py` keep working;
  their `archie_cli.tui.app.httpx.AsyncClient` patches move to `archie_cli.tui.client`. The
  shims go with the old app in M2. M1 creates `tests/tui_fakes.py` with a fake client for
  events, stream and delete.
- M2 (new app): `test_tui_detail.py`, `test_tui_subagents.py`, `test_tui_copy_paste.py`,
  `test_tui_models_provider.py`, `test_model_switch.py::TestModelProvider` move to the new
  app (detail views stay modal and the model provider stays until M5/M7);
  `test_tui_quit.py` is replaced by a Quit test; CLI tests updated.
- New: `tests/tui_fakes.py`, `tests/test_tui_app.py`, `tests/test_tool_rows.py`,
  `tests/test_session_location.py`.
- Unchanged: `test_tui_error_text.py`, `test_tui_input.py`,
  `test_tui_shell.py` (until M8), `test_markup.py`, `test_ws_client.py`.

### Data model

`SessionLocation` per D25; `sessions.location TEXT NULL` (JSON), migration 3.

### Data flow

1. Start: `ArchieApp` opens the feed; the snapshot builds the sidebar; the focused (or
   newest live) session gets a controller and the pane attaches to it.
2. Controller: live and not starting → GET `/events` (full on first load, `after` later),
   connect the stream, buffer frames until the replay lands, apply; ended → GET `/events`
   only. While the first load runs the pane shows "Loading…"; replay yields every 200
   events.
3. Feed `session` event: update the row; on-screen → header, D18; others → compare with the
   previous record and notice; leaving `starting` lets a waiting controller connect.
4. Switch (D10): detach the old pane's controller, remove the pane, attach a new pane.
5. Failures: feed drop → back-off, snapshot re-syncs; stream drop → D9; `/events` 409 →
   full reload of that session; HTTP errors in a session action → a client error row in
   the pane; errors in app actions (create, workspaces) → a notice.

### Error handling (server)

| Failure | Response |
|---|---|
| Workspace dir gone (`resolve_workspace` `ValueError`) | location None for that session |
| Log unreadable (`OSError`) on connect scan | keep the previous location; DEBUG log |
| `_Reader` `OSError`/`ValueError` | that candidate is skipped |
| Unexpected exception in a location job | caught and logged (as `_title_job`); the stream is still followed |
| Store write fails | WARNING; the in-memory record keeps it (082 D12a) |

### Non-functional

- Switching to a visited session mounts from memory: measured in M2 on
  `archie-01m32ky6rn.jsonl` (11.5 MB) with `/tmp/switch_timing.py` (a `run_test` script that
  feeds the log to a fake session and times attach); above 300 ms, the previous pane stays
  mounted hidden (an LRU of 2 mounted panes), logged as a deviation.
- One 8 Hz animation timer at most, plus a 30 s age timer (D16).
- Location work runs off the event loop; the sweep reads one HEAD per live session.

### Config / infra

- `archie.db` migration 3 on the next orchestrator start.

## Milestones

### [x] M1 — SessionController holds a session's state (prefactor)

- **Approach:** **prefactor**. Create `client.py` (`HttpOrchestrator` for events/stream/
  delete only so far) and `session.py`. Move the per-session state and logic listed in
  Code structure from `app.py` into `SessionController` per D6, including local entries with
  anchors and today's reconnect rule (D9's feed-driven rule is M6). Today's `ArchieApp` becomes a thin host: it composes the same
  widgets, implements `SessionListener`, and forwards actions to the controller. Rewrite
  the M1 tests (Code structure) against the controller with a recording listener.
- **Test seam:** `SessionController` with the M1 fake client in `tests/tui_fakes.py` and a
  recording listener; the host app for widget assertions.
- **Deliverable:** `archie attach` behaves as before, backed by the controller.
- **Verify:** `scripts/dev test tests/test_tui_canonical.py tests/test_tui_question.py
  tests/test_tui_streaming.py tests/test_protocol_version.py tests/test_orchestrator_bounce.py
  tests/test_kiro_translate.py tests/test_tui_detail.py tests/test_tui_window.py` pass; full
  suite.
- **Outcome:** `client.py` (`OrchestratorClient`, `HttpOrchestrator`: events/stream/delete,
  `CursorNotFound`), `session.py` (`SessionController` with all per-session state, local
  entries with anchors, `Ledger`/`TurnMetrics`, the `SessionListener` protocol), `app.py` a
  thin host (1085 → 554 lines) with temporary shims, `tests/tui_fakes.py`. Listener adds two
  methods beyond D6: `on_replay(active)` (brackets history batches so the conversation still
  mounts once; replay yields every 200 events) and `on_notice(message, timeout)` (today's
  reconnect toasts, M1 only); `on_reset` is async. Evidence: M1 Verify files 129 passed;
  full suite 5209 passed; Ruff and format clean.

### [x] M2 — The new TUI lists sessions and shows the selected one

- **Approach:** `client.py` gains the feed and PATCH; `pane.py`,
  `sidebar.py`, new `app.py` (D7, D8, D10, D15, D19, D30; sidebar line 2 shows `no repo` until
  M3), `tests/tui_fakes.py`. `TranscriptView.load(timeline)`. Wire `start`/`attach`; delete
  the old app and `quit_screen.py`; move the M2 tests (Code structure). Detail views stay
  modal `DetailScreen`s and `models_provider.py` stays, re-pointed at the on-screen
  controller. Until M7, ctrl+q quits at once (sessions keep running). Measure switch time
  (Non-functional) and record it in the Outcome.
- **Test seam:** `ArchieApp(client=FakeOrchestrator)` with `run_test`.
- **Deliverable:** `archie attach` shows the sidebar of live sessions and the selected
  session's live transcript; switching works; messages send.
- **Verify:** `scripts/dev test tests/test_tui_app.py tests/test_tui_detail.py
  tests/test_tui_subagents.py tests/test_cli_attach.py tests/test_cli_start_stop.py
  tests/test_cli_profiles.py` pass; full suite.
- **Outcome:** New `ArchieApp(client, profile, focus)` with a feed-driven `Sidebar`
  (`SessionList` of keyed `WorkspaceBand`/`SessionRow`, D30 glyphs and badges, `no repo` until
  M3), one `SessionPane` (header, loading line, conversation, throbber, question, input,
  status) attached to the on-screen `SessionController`; `TranscriptView.load` rebuilds from
  the builder with anchored local entries; controller LRU of 5 with D7's exemptions;
  switches serialised by a lock and generation number inside `batch_update()`;
  `start`/`attach` wired, `quit_screen.py` and the M1 shims deleted, `SubagentProvider`
  dropped (D22). A switch crash (a removed pane's `MessageInput` drawn after its styles were
  cleared, 7 of 8 timing runs) is fixed by doing the whole switch in `batch_update()` with a
  layout refresh, with a regression test. Window constants 200/80/40 → 40/20/20 (see
  Deviations). Evidence: M2 Verify files pass; full suite 5236 passed; Ruff clean. Switch
  back to `archie-01m32ky6rn.jsonl` (11.5 MB) through the next frame: median 376/401/379 ms
  over 3 runs of 5 (491 widgets); `switch_to` itself 10 ms; first load ~780 ms.

### [x] M3 — Sessions carry their repo and branch

- **Approach:** D25–D29. `workspaces.locate` on `_Reader`; `location.py` (prototype's
  `location.py` as the algorithm reference, via `git show
  prototype/tui-workbench:cli/src/archie_cli/tui/prototype/location.py`, plus D26); tracker
  jobs, connect scan, sweep (started in `start()`, cancelled in `close()`); migration 3;
  docs (`docs/architecture.md` record table, a location paragraph in the tracker section,
  the datastore section). Sidebar line 2 (D30) and the status bar's branch from the
  location (D31; the rest of D31 is M4). `/tmp/location_eval.py` is a throwaway script that
  runs `location.scan_log` and `workspaces.locate` over the 10 newest `archie-*` logs with
  `/workspace` as the workspace dir and prints each result.
  ⚠️ Resolve on `ToolResult`. ⚠️ All reads through `_Reader`.
- **Test seam:** `locate`/`location` functions with real repos in `tmp_path`;
  `SessionTracker._on_frame` with frames (as the existing tracker tests); store migration;
  sidebar rows in `run_test`.
- **Deliverable:** the sidebar shows `⎇ repo · branch` for sessions that touched a repo.
- **Verify:** `scripts/dev test tests/test_session_location.py tests/test_session_store.py
  tests/test_session_tracker.py tests/test_tui_app.py` pass; full suite;
  `scripts/dev run python /tmp/location_eval.py` (scans the 10 most recent `archie-*` logs
  with `/workspace` as the workspace dir) prints a location or `None` per session, and the
  current session (`$ARCHIE_SESSION_ID`) resolves to `archie-nexus` with the branch of the
  worktree it last worked in.
- **Outcome:** `SessionLocation` and `SessionDescriptor.location`; `workspaces.locate` and
  `refresh_location` on `_Reader`; new `location.py` (candidate extraction with ordered `cd`
  chains, heredoc/comment skipping, `git -C`; `LocationState` keyed by `(scope, tool_use_id)`,
  eligible on result; `best()` per D26; `scan_log`); tracker jobs on results and on every
  connect (frames during the scan buffered and replayed), a 30 s sweep, `workspace_root`
  injection; migration 3 (`location` JSON); sidebar line 2 and the status-bar branch from the
  location; docs. Carry-over from M2: the window pages in earlier rows (up to 10 pages)
  while it can't scroll, plus a `_jumping` guard so a jump to latest doesn't bounce back a
  page. Evidence: 224 targeted tests pass; full suite 5307 passed; Ruff clean. Eval over
  the 10 newest archie logs: this session resolves to `archie-nexus` /
  `build-it-worktrees/084-multi-session-tui` / `build-it/084-multi-session-tui` (correct);
  others to `archie-nexus` main or the `fix-callback-log-redaction` worktree; three empty
  logs to None; scans 13–45 ms.

### [x] M4 — Two-line tool rows, activity line and the pane's look

- **Approach:** D11, D12, D16, D33. `tool_rows.py`; `ToolRow`; `UserMessage`; `ActivityLine`
  replaces the throbber (delete `throbber.py`); header badge; status bar (D31 except the
  branch); `archie.tcss` and theme per the prototype's `prototype.tcss` values; animation
  and age timers.
- **Test seam:** `tool_rows` functions; `run_test` for the activity line and header.
- **Deliverable:** the pane renders per the spec; an SVG screenshot of a fake session taken
  with `app.save_screenshot()` shows two-line rows, the activity line and the header badge.
- **Verify:** `scripts/dev test tests/test_tool_rows.py tests/test_tui_app.py` pass; full
  suite; the screenshot is inspected.
- **Outcome:** `shared/.../tool_rows.py` (plain-parts row model from `ToolNode`, cut rules
  keeping the suffix), two-line `ToolRow`/`SubagentRow` drawn in `render()`, header-less user
  messages with a bar and replies aligned to tool rows, `ActivityLine` from the controller's
  `activity` (Thinking/Responding/Working; blank, same height between turns), one
  `AnimationClock` (8 Hz, paused when nothing is registered) plus the 30 s age timer,
  status bar per D31, theme and CSS per the prototype's values; `throbber.py` deleted;
  `plans/tui-tool-rows.md` with the D12 corrections. Evidence: 88 targeted tests; full suite
  5363 passed; Ruff clean; screenshot `/tmp/084-m4/084-m4.png` (synthetic session) inspected:
  rows, bars, activity line, badge and repo/branch lines as specified, the status bar's cost
  cut off at 150 columns (fixed in M7). Switch back to the 11.5 MB session: median 383 ms
  (the prototype's `Markdown > *:last-child` rule cost ~500 ms and was replaced).

### [ ] M5 — Tool and subagent views inside the pane

- **Approach:** D13, D23. `detail.py` widgets fed by `on_changes`; the pane's view stack;
  stop child from the subagent view; delete `DetailScreen`s; port `test_tui_detail.py` and
  `test_tui_subagents.py`.
- **Test seam:** `run_test` with the fake client.
- **Deliverable:** clicking a row opens a live-updating view; Esc returns.
- **Verify:** `scripts/dev test tests/test_tui_detail.py tests/test_tui_subagents.py` pass;
  full suite.
- **Outcome:**

### [ ] M6 — Notices, unseen and reconnect

- **Approach:** D9, D17, D18. Feed transitions → notices; PATCH unseen; the feed-driven
  stream retry rule replacing M1's 30 s give-up, the single reconnecting row, feed reconnect
  with the 40 s dead-feed rule and the missing-session fetch.
- **Test seam:** `run_test` with a fake client that drops and restores connections.
- **Deliverable:** background sessions announce replies and questions; a restart recovers.
- **Verify:** `scripts/dev test tests/test_tui_app.py -k "notice or unseen or reconnect"`
  pass; full suite.
- **Outcome:**

### [ ] M7 — Dialogs, palette, model picker and filter

- **Approach:** D3, D14, D23, D24, D34. `client.py` gains POST and workspaces; `dialogs.py`
  (`QuitDialog` replaces M2's immediate quit), `commands.py` (replacing
  `models_provider.py`; port `test_tui_models_provider.py` and
  `test_model_switch.py::TestModelProvider`), filter, web link, keys.
- **Test seam:** `run_test` with the fake client.
- **Deliverable:** sessions can be created, renamed and stopped; the palette acts on the
  on-screen session.
- **Verify:** `scripts/dev test tests/test_tui_app.py -k "dialog or palette or filter"
  tests/test_model_switch.py` pass; full suite.
- **Outcome:**

### [ ] M8 — Direct shell, editor, copy and docs

- **Approach:** D20, D21. `!` shell and the editor through the controller/pane; docs:
  `README.md`, `docs/architecture.md` (TUI sections and the `_apply_event` mentions),
  `docs/session-protocol-walkthrough.md` (`_apply_event`, throbber, `ToolDetailScreen`,
  reconnect), `docs/event-spec.md`.
- **Test seam:** ported shell and copy tests; `run_test` for the editor reconnect.
- **Deliverable:** parity with the old TUI's remaining features.
- **Verify:** full suite; Ruff; `rg -n "quit_screen|throbber|models_provider|DetailScreen"
  cli tests` finds nothing.
- **Outcome:**

## Deviations

- M4: fetch rows show `N lines` (the result has no match count); URLs are elided in the
  middle like paths; widths are measured in characters, not cells.
- M3: turns are ordered by their latest call, not the turn number (robust to a restarted
  counter). A `cd` target that can't be resolved stops the walk but keeps the directories
  reached before it.
- M2: `TranscriptView`'s window is 40 rows (page 20, overscan 20), not 078 D9's 200/80/40.
  Every switch remounts the window; at 200 the 11.5 MB session mounted ~5,100 widgets and
  took 5.2 s to settle, at 40 it mounts ~490 and takes ~0.38 s. The 300 ms target is still
  missed by ~80 ms in this headless ARM container; the plan's fallback (a second mounted,
  hidden pane) is not applied, because it doubles the DOM the 078 keypress cost depends on
  for a gain only on A↔B toggling. Recorded under Risks. Follow-on fix in M3: a window
  shorter than the viewport can't scroll, so earlier rows must page in automatically.
- M1: `SessionListener` gained `on_replay(active)` and `on_notice(message, timeout)`, and
  `on_reset` is async (popups and `Conversation.reset()` must finish before the full
  replay). `on_notice` goes away with the reconnect toasts in M6.
## Risks

- Switching back to a very large session takes ~0.4 s here (target 300 ms); not measured on
  the user's Mac.
- The TUI can't be run against the user's real orchestrator from this container; app
  behaviour is verified with the fake client and `run_test` only.
- Moving `ArchieApp` logic into a controller may change replay/reconnect details only the
  rewritten tests cover.
- Location heuristics (shell parsing) will sometimes pick the wrong repo.

## Review

- Plan review: 2 passes (archie-worker). Pass 1 fixed: the ❌s (client-only rows and pane
  state lost on remount, so D6/D7 add a controller timeline and draft; the controller↔pane
  contract, D6; ArchieApp-dependent tests between milestones, so M1 is a prefactor with a
  test schedule; missing test files; no local-profile rule, D20), plus D3's post-create
  switch, D10 switch serialisation, D12 spec questions, D16 spinners, D23 keys and Esc, D24
  compact, D30 row content, D31 status bar, server error handling, the `location = None`
  default, one constructor signature (D19), and location moved after the TUI tracer (M3).
  Pass 2 fixed: the ❌s (shims for tests the prefactor would break; `on_reset`/`on_loaded`;
  the non-existent `model_price`), the timeline rule (rebuild from builder roots plus
  anchored local entries), M1's reconnect rule vs M6, D20 using `archie shell`'s rule, Esc
  order (detail view before question; 0.5 s), ctrl+x, scoped results for subagent-only
  location, the age timer, worktree path format, `store.COLUMNS`, snapshot-missing and
  dead-feed rules, the docs list, the M3/M4 status-bar split, ctrl+q before M7,
  POST/workspaces moved to M7, the switch-time measurement moved to M2, the eval script
  described. None rejected. Stopped after two passes.

## Outcome

PR: pending
CI: pending

## Follow-ups

- Model, first message and background options in the new-session dialog (roadmap item 5).
- A session browser for ended sessions.
- Web client parity (roadmap item 6).
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

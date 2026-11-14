# 085 — Current-agent stop shortcut

Status: done
Branch: build-it/085-current-agent-stop (base: feat/default-tui-entrypoint @ 68ed85c)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/085-current-agent-stop

## Objective

Make `Ctrl+X` stop the agent whose conversation is currently shown. When a subagent view is open, it must confirm stopping that subagent; when the main session view is shown, it must confirm interrupting the main agent. The warnings must distinguish the lower-impact child stop from the more destructive main-agent stop.

## Context

The TUI currently binds `Ctrl+X` to `stop_child` and only handles it when a subagent detail view is open. It interrupts the topmost open child immediately, without confirmation; with no child view, the key falls through to the focused input and cuts text. `Esc` already interrupts the main turn through `SessionController.cancel()`, while `stop_child()` sends an `InterruptCommand` for a child. The existing palette stop flow provides the confirmation-dialog pattern and defaults destructive dialogs to Cancel.

The source checkout contains unrelated untracked `.DS_Store`, `plans/060-spec-container-aws-profiles.md`, and `plans/070-build-nexus-plugins.md`; they are excluded from this build. Focused baseline: `scripts/dev test tests/test_tui_app.py tests/test_tui_detail.py` passed with 101 tests.

## Baseline

- Check commands: `scripts/dev test`; `scripts/dev ruff check .`; `scripts/dev ruff format .`
- Setup: no setup command needed; worktree dependencies were available
- Setup leftovers: none
- Failing before this work: none in focused TUI tests
- Not included: `.DS_Store`, `plans/060-spec-container-aws-profiles.md`, `plans/070-build-nexus-plugins.md`

## Decisions

| # | Decision | Chosen | Rejected | Why | Source |
|---|---|---|---|---|---|
| D1 | What does `Ctrl+X` target? | The topmost open subagent view, otherwise the main session agent | Keep it child-only and let the input cut text | The requested behavior is current-conversation semantics, and the existing app already knows the open-view stack | user |
| D2 | Should either stop happen immediately? | Confirm both, with separate messages and labels | Reuse the child interrupt without confirmation | Main-agent interruption is destructive, and confirmation also prevents accidental child cancellation; the existing `ConfirmDialog` is the established pattern | self |
| D3 | How should the main-agent interrupt be sent? | Reuse `SessionController.cancel()` after confirmation | Add a second websocket command path | `cancel()` already sends the root interrupt and preserves the existing session behavior | self |
| D4 | What happens when no agent turn is active? | Open the relevant warning and let the controller handle the command as it currently does | Add a new active-turn state gate | the main interrupt is already a no-op when no turn is active, while the child command is retained for the selected live child; the shortcut is inert when no live session/view exists | self |

## Requirements

- MUST make `Ctrl+X` target the topmost open subagent view when one exists.
  - AC: the child confirmation opens over a subagent/tool detail view and confirming sends `InterruptCommand` for that child.
- MUST make `Ctrl+X` target the main session agent when no subagent view is open.
  - AC: the main confirmation opens from the main transcript and confirming sends the root interrupt through the controller.
- MUST require confirmation before either shortcut stop is sent.
  - AC: dismissing or accepting the default Cancel choice sends no interrupt; selecting Stop sends exactly one interrupt when the selected controller path is active.
- MUST leave the shortcut inert when there is no on-screen live session or no current agent view to act on.
  - AC: pressing `Ctrl+X` in those states opens no dialog and sends no command.
- MUST use distinct warning text that explains child stops preserve the main agent and siblings, while main-agent stops interrupt the main turn and may end its remaining work.
  - AC: child and main dialog titles, button labels, and messages differ and contain those consequences.
- MUST preserve normal input editing when the shortcut confirmation is not applicable because a dialog is already open.
  - AC: existing modal behavior and focused TUI tests continue to pass.

Out of scope: changing `Esc`, the command palette's session/container stop, backend interrupt semantics, or sibling-subagent behavior.

## Design

### Overview

`App.check_action` will allow the priority `Ctrl+X` action on the main screen regardless of whether a child view is open. `App.action_stop_current` will capture the topmost child key, or the main-session target, before opening a purpose-built `ConfirmDialog`; confirmation then invokes that captured target. It will do nothing without a live on-screen session. The existing pane view stack remains the source of truth for the current conversation.

### Code structure

- `cli/src/archie_cli/tui/app.py`: binding, action dispatch, current-agent selection, and confirmation coroutine.
- `cli/src/archie_cli/tui/dialogs.py`: separate child/main warning constructors following `stop_dialog` and `ConfirmDialog`.
- `tests/test_tui_detail.py`: child shortcut behavior through the mounted TUI.
- `tests/test_tui_app.py`: main shortcut confirmation and dialog copy.

No new dependency or wire contract is required.

### Error handling

If the user cancels, no websocket command is sent. If the websocket is unavailable, the existing controller `_fire` behavior drops the command and logs it; this change does not alter transport error handling. Modal dialog dismissal leaves the current view and input unchanged.

## Milestones

### [x] M1 — Confirm and stop the current agent

- **Approach:** Replace the child-only `Ctrl+X` action in `app.py` with a current-agent action. Add child and main confirmation constructors in `dialogs.py`, using destructive dialogs with Cancel focused. Capture `(scope, index)` before opening a child dialog; on confirmation call `SessionController.stop_child()` with that key, or call `SessionController.cancel()` for the main agent. Update the existing child test's no-view expectation and add main-agent confirmation/cancellation tests, preserving the existing `stop_dialog` tests.
- **Test seam:** Textual `App.run_test` with the real priority binding, `make_app`, `attached`, `settle`, and the fake websocket exposed as `app.controller.ws.sent`.
- **Edge cases:** nested tool view → capture and target the underlying subagent; no child view → target main agent; no live on-screen session → no dialog or command; Cancel/Escape and repeated shortcut while modal → no interrupt; the existing modal handling remains authoritative.
- **Deliverable:** `Ctrl+X` visibly warns about and confirms stopping the current child or main agent, with distinct consequences.
- **Verify:** `scripts/dev test tests/test_tui_app.py tests/test_tui_detail.py` passes, then `scripts/dev ruff check .` and `scripts/dev ruff format --check .` pass without changes.
- **Outcome:** Implemented current-agent selection, distinct confirmation dialogs, captured child targets, and TUI regressions. Focused tests (101) and full tests (5,432) passed; Ruff lint and targeted format checks passed. Repository format check remains blocked by pre-existing formatting in `persona/skills/python/assets/output.py`.

## Deviations

- None.

## Risks

- The main-agent interrupt currently has no separate return value for whether a turn is active; the implementation will retain that established behavior rather than broaden session state handling.

## Review

- Plan review: 1 pass. Fixed the inactive-turn AC, defined no-live-session behavior, documented captured child targets, specified existing test helpers, and changed format verification to `--check`.
- Code review: general and risk, 1 round; both reported no findings. The general reviewer mentioned an unrelated full-suite failure, but the recorded full run in this worktree passed all 5,432 tests; no failure was reproducible. Risk reviewer confirmed no external-input, API, security, or performance issue.
- Merge risk: Low: the change is confined to the TUI shortcut and confirmation path, with no wire or persistence changes; cancellation remains the default and existing full tests passed.

## Outcome

Delivered and verified with 5,432 passing tests, Ruff lint passing, targeted format checks passing, and `git diff --check` passing. Repository-wide format check has one unrelated existing mismatch in `persona/skills/python/assets/output.py`.

PR: https://github.com/simon-downes/archie-nexus/pull/31
CI: none (no checks configured on the repository)

## Follow-ups

- Repository-wide `scripts/dev ruff format --check .` remains red because `persona/skills/python/assets/output.py` would be reformatted; this file is unrelated and was left unchanged.

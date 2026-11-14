# 085 — Current-agent stop shortcut

Status: planning
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
| D4 | What happens when no agent turn is active? | Open the relevant warning and let the controller handle the command as it currently does | Add a new active-turn state gate | The existing interrupt methods are safe no-ops at the transport boundary, and this keeps shortcut behavior predictable without inventing state semantics | self |

## Requirements

- MUST make `Ctrl+X` target the topmost open subagent view when one exists.
  - AC: the child confirmation opens over a subagent/tool detail view and confirming sends `InterruptCommand` for that child.
- MUST make `Ctrl+X` target the main session agent when no subagent view is open.
  - AC: the main confirmation opens from the main transcript and confirming sends the root interrupt through the controller.
- MUST require confirmation before either shortcut stop is sent.
  - AC: dismissing or accepting the default Cancel choice sends no interrupt; selecting Stop sends exactly one interrupt.
- MUST use distinct warning text that explains child stops preserve the main agent and siblings, while main-agent stops interrupt the main turn and may end its remaining work.
  - AC: child and main dialog titles, button labels, and messages differ and contain those consequences.
- MUST preserve normal input editing when the shortcut confirmation is not applicable because a dialog is already open.
  - AC: existing modal behavior and focused TUI tests continue to pass.

Out of scope: changing `Esc`, the command palette's session/container stop, backend interrupt semantics, or sibling-subagent behavior.

## Design

### Overview

`App.check_action` will allow the priority `Ctrl+X` action on the main screen regardless of whether a child view is open. `App.action_stop_current` will select the topmost child key or the main controller, open a purpose-built `ConfirmDialog`, and only invoke the selected interrupt after confirmation. The existing pane view stack remains the source of truth for the current conversation.

### Code structure

- `cli/src/archie_cli/tui/app.py`: binding, action dispatch, current-agent selection, and confirmation coroutine.
- `cli/src/archie_cli/tui/dialogs.py`: separate child/main warning constructors following `stop_dialog` and `ConfirmDialog`.
- `tests/test_tui_detail.py`: child shortcut behavior through the mounted TUI.
- `tests/test_tui_app.py`: main shortcut confirmation and dialog copy.

No new dependency or wire contract is required.

### Error handling

If the user cancels, no websocket command is sent. If the websocket is unavailable, the existing controller `_fire` behavior drops the command and logs it; this change does not alter transport error handling. Modal dialog dismissal leaves the current view and input unchanged.

## Milestones

### [ ] M1 — Confirm and stop the current agent

- **Approach:** Replace the child-only `Ctrl+X` action in `app.py` with a current-agent action. Add child and main confirmation constructors in `dialogs.py`, using destructive dialogs with Cancel focused. On confirmation, call `SessionController.stop_child()` for the selected child or `SessionController.cancel()` for the main agent. Update the existing child test and add main-agent confirmation/cancellation tests, preserving the existing `stop_dialog` tests.
- **Test seam:** Textual `App.run_test` with the real priority binding, confirmation dialog, fake websocket, and `SessionController`.
- **Edge cases:** nested tool view → target the underlying subagent; no child view → target main agent; Cancel/Escape → no interrupt; dialog open → modal handling remains authoritative.
- **Deliverable:** `Ctrl+X` visibly warns about and confirms stopping the current child or main agent, with distinct consequences.
- **Verify:** `scripts/dev test tests/test_tui_app.py tests/test_tui_detail.py` passes, then `scripts/dev ruff check .` and `scripts/dev ruff format .` report no issues.
- **Outcome:** pending

## Deviations

- None.

## Risks

- The main-agent interrupt currently has no separate return value for whether a turn is active; the implementation will retain that established behavior rather than broaden session state handling.

## Review

- Plan review: pending
- Code review: pending
- Merge risk: pending

## Outcome

Pending implementation and verification.

PR: pending
CI: pending

## Follow-ups

- None.

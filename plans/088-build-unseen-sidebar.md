# 088 — Unseen sidebar indicator

Status: reviewing
Branch: build-it/088-unseen-sidebar (base: main @ 9b137e9)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/088-unseen-sidebar

## Objective

Make the TUI sidebar identify sessions needing attention without relying on bold text. An unseen ready session will use the same purple as the current-session border; seen ready sessions will remain green. The current session name will be bold, while unseen background session names will use normal weight.

## Context

The orchestrator sets `SessionDescriptor.unseen` when a root turn completes or fails. The TUI clears it for the on-screen session and currently renders unseen names in bold. `state_glyph()` is shared by the sidebar row and session header; it renders idle sessions green, regardless of `unseen`. The requested change is presentation-only and does not alter notification or unseen persistence behavior. The purple unseen idle glyph will therefore also appear in the session header when that record is still unseen; the header already bolds its title independently.

## Baseline

- Check commands: `scripts/dev test`; `scripts/dev ruff check .`; `scripts/dev ruff format .`
- Setup: none
- Setup leftovers: none
- Failing before this work: none; focused TUI tests passed (13 passed)
- Not included: `.DS_Store`, `plans/060-spec-container-aws-profiles.md`, and `plans/070-build-nexus-plugins.md` in the user's checkout

## Decisions

| # | Decision | Chosen | Rejected | Why | Source |
|---|---|---|---|---|---|
| D1 | Base branch for the build | `main` at `9b137e9` | Current checkout branch | User explicitly requested a branch from main; checkout had unrelated feature work | user |
| D2 | Unseen visual treatment | Purple idle glyph using `theme.PRIMARY`; seen idle glyph remains green | Bold unseen name or a new marker | Reuses the existing current-row border colour and keeps the two-cell row layout unchanged | user |
| D3 | Title weight | Bold only for the current row | Bold unseen names | Makes current focus explicit and leaves attention state to the glyph | user |
| D4 | Scope | Sidebar rendering and focused tests only | Notification/state semantics changes | User confirmed notifications are working; the issue is identifying sessions needing attention | self |

## Requirements

- MUST render an idle session's glyph in `theme.PRIMARY` when `unseen=True` and in `theme.SUCCESS` when `unseen=False`.
  - AC: sidebar glyph tests assert the idle unseen and idle seen styles differ and equal the corresponding theme colours.
- MUST bold the session title only when its row is current.
  - AC: row rendering tests assert a current row's title has bold style and an unseen non-current row's title does not.
- MUST preserve existing status glyphs and notification behavior for non-idle statuses.
  - AC: existing TUI status and notification tests pass unchanged.

Out of scope: changing what sets or clears `unseen`, notification text/timing, the session feed contract, or pane header badges.

## Design

### Overview

Update `cli/src/archie_cli/tui/sidebar.py`: make `state_glyph()` select the idle colour from `record.unseen`, and make `SessionRow.render()` derive title weight only from `self.current`. Extend `tests/test_tui_app.py` at the existing sidebar rendering seams.

### Code structure

No new modules or dependencies. Follow the existing `state_glyph`, `state_badge`, and `SessionRow.render()` patterns in `cli/src/archie_cli/tui/sidebar.py`.

## Milestones

### [x] M1 — Show unseen state with a purple sidebar glyph

- **Approach:** Change idle glyph colour selection in the shared `state_glyph()` helper and current-row title styling in `cli/src/archie_cli/tui/sidebar.py`. This also updates the session header's idle glyph because `pane.py` reuses the helper. Add explicit style assertions to the existing status/row tests in `tests/test_tui_app.py`.
- **Test seam:** `state_glyph()` and mounted `SessionRow.render()` output used by the TUI tests.
- **Edge cases:** unseen error/blocked sessions keep their error/warning glyphs; only idle unseen sessions use the purple attention colour.
- **Deliverable:** A non-current unseen ready session has a purple glyph and normal title text; once seen its glyph is green; the current row title is bold.
- **Verify:** `scripts/dev test tests/test_tui_app.py` passes; `scripts/dev ruff check cli/src/archie_cli/tui/sidebar.py tests/test_tui_app.py` passes.
- **Outcome:** Updated the shared idle glyph to use `theme.PRIMARY` while unseen and `theme.SUCCESS` once seen; changed row title bolding to current-only; added style tests for idle, blocked, current, and unseen rows. `scripts/dev test tests/test_tui_app.py` passed with 78 tests and the focused Ruff check passed.

## Deviations

- M1: The shared `state_glyph()` helper is also used by the session header, so the purple unseen idle glyph applies there too. The plan review identified this shared use and the behavior is documented and covered by the existing header rendering path.

## Risks

- None known.

## Review

- Plan review: 1 pass; fixed the shared `state_glyph()` scope and expanded verification to the full TUI app test file.
- Code review: general reviewer, 1 round; no findings. Risk review skipped because the change has no external input, public interface, auth, shell, SQL, or per-item external work.
- Merge risk: low: presentation-only TUI changes with full tests and no changed wire or persistence behavior.

## Outcome

Implemented the unseen-session sidebar treatment. Unseen idle sessions use the purple primary glyph, seen idle sessions use the green success glyph, and only the current session title is bold. Full tests passed (5,442); Ruff lint passed. Repository-wide format checking remains blocked by the pre-existing unrelated `persona/skills/python/assets/output.py` formatting mismatch.

PR: pending
CI: pending

## Follow-ups

- None.

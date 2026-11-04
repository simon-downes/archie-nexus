# 078 — Tool and subagent detail views in the TUI and web viewer

Status: planning
Branch: build-it/078-tool-detail-views (base: main @ 245b73f)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/078-tool-detail-views

## Objective

Make tool calls and subagents inspectable in both clients. Clicking a tool call in the
TUI or the web session viewer opens an overlay with its full input and result. Clicking a
subagent opens its transcript, whose tool calls are clickable in the same way. Escape backs
out one level, and a breadcrumb title shows where you are. Iteration blocks get a
permanent dim border so parallel tool calls are visible at a glance.

## Context

The user's request: "clicking on a tool call in the tui (or web client) should show the
tool call and the result in a pop up/over window. this will be great for debugging and
general visibility. we can remove the existing click to expand on exec call and just show
the first 10 lines. we can also do that for shell tool (which i think always shows the
full command). bonus points if we can add syntax highlighting when viewing in the popup.
clicking a subagent should show the transcript. tool calls in the subagent view should
also be clickable the same as in the main session window. esc should back out of popup
views. might be useful to have a breadcrumb or similar title bar structure to indicate
what we're viewing." Plus: "i like the idea of a dim border on iteration blocks". The web
client stays viewer-only (no session driving).

Codebase facts:

- TUI: `cli/src/archie_cli/tui/conversation.py` has `IterationBlock` (one per LLM
  response that calls tools, focusable, `IterationBlock:focus` border in `archie.tcss`)
  and `ToolEntry` (non-focusable; exec source collapsed to 10 lines with click to
  expand). `ToolEntry` keeps only the pending summary string, not the raw input or result.
- TUI subagents: `app.py::_handle_scoped_event` folds child events into
  `ChildActivityState` (`subagents.py`), keeping only the last 3 activity lines.
  `SubagentActivity` rows have no click handler. `SubagentScreen` (a `ModalScreen`) shows
  status plus those lines and is opened only from the command palette
  (`models_provider.py::SubagentProvider` → `app.open_child_detail`).
- Web viewer: `orchestrator/src/archie_orchestrator/transcript.py::TranscriptBuilder`
  reduces events into nodes rendered by `templates/viewer/_nodes.html`; live updates go
  over SSE (`web.py::session_live`, `static/viewer/live.js`, htmx swaps). Tool rows are
  `<details>` that expand inline to input and result; subagent events are nested inline
  under their `task` tool. `IterationStart` is ignored, so iterations are not grouped.
  Pygments highlighting already exists (`highlight_code`).
- `shared/src/archie_shared/tool_summaries.py` produces Rich-markup summaries used by
  both clients; `format_tool_pending("shell")` contains the full command.

## Baseline

- Check commands: `scripts/dev test`, `scripts/dev ruff check .`, `scripts/dev ruff format --check <touched files>`
- Setup: `scripts/dev sync`
- Setup leftovers: none (the environment is `.venv-archie`, gitignored)
- Failing before this work: none (2552 passed; ruff clean)
- Not included: user's uncommitted changes in `plugins/datadog/` (README.md, __init__.py,
  formatting.py, logs.py, skills/datadog/SKILL.md, tests/test_datadog_monitors.py,
  tests/test_datadog_reads.py, validation.py) and untracked
  `plans/060-spec-container-aws-profiles.md`, `plans/070-build-nexus-plugins.md`

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|

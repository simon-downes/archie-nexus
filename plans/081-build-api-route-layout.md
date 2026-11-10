# 081 — Orchestrator API route layout

Status: planning
Branch: build-it/081-api-route-layout (base: main @ a1d18e4)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/081-api-route-layout

## Objective

The orchestrator mixes JSON routes and web pages at the root, with pages under `/ui`. Move
every JSON route under `/api`, serve the web pages at the root without `/ui`, and split the
route table into an API module and a page module. When done, code clients (CLI, TUI, the
auth page's script) call `/api/...`, browsers use `/sessions`, `/insights/...` and `/auth`.

## Context

Roadmap item 1 (brain: `projects/archie/nexus-roadmap/api-route-layout.md`). The user asked
to implement it with three fixed points: `/`, `/health` and `/auth/callback/{provider}` stay
exactly as they are. Keeping the callback means no OAuth redirect URI re-registration
(`auth.py:140` builds it; `plugins/google/README.md:30` documents it) and the callback log
filter at `app.py:65` stays valid. Backward compatibility doesn't matter (single-user
system), so the old paths are removed rather than redirected.

Codebase facts:

- All routes are in one list at `orchestrator/src/archie_orchestrator/app.py:509-544`.
  JSON handlers live in `app.py`; proxy handlers in `proxy.py`; page handlers in `web.py`
  and `insights_web.py` (which imports from `web.py`).
- `/ui/sessions` already returns its `#results` fragment for `HX-Request` (`web.py:250`).
- Clients build URLs by hand: `cli/src/archie_cli/cli.py` (`_profile_url` plus literal
  paths; `ws_url`/`api_url` for the TUI), `cli/src/archie_cli/auth.py` (`_orchestrator_url`
  plus literal paths), `templates/auth_status.html` (`fetch('/auth/...')`).
- Templates and `insights_web.VIEWS` hard-code `/ui/...` links; `templates/viewer/detail.html`
  carries the live SSE URL in `data-live-url`.
- Tests patch `archie_orchestrator.app.{list_sessions,start_session,stop_session,home_dir}`
  and import `_query_metrics` and `unhandled_exception_handler` from `app`.
- Docs listing routes: `docs/architecture.md`, `docs/auth-credentials.md`,
  `docs/session-protocol-walkthrough.md`, `docs/config.md`, `docs/kiro.md`.

## Baseline

- Check commands: `env -u ARCHIE_BACKEND scripts/dev test`, `scripts/dev ruff check .`,
  `scripts/dev ruff format --check <touched files>`
- Setup: `scripts/dev sync`
- Setup leftovers: none (`git status --short` clean after sync and tests)
- Failing before this work: none with `ARCHIE_BACKEND` unset (4783 passed). This Kiro session
  exports `ARCHIE_BACKEND`, which makes 23 agent tests in `tests/test_question_tool.py` and
  `tests/test_ws_integration.py` fail; that is environmental, so checks unset it.
  `ruff format --check .` already flags `persona/skills/python/assets/output.py`.
- Not included: `plans/060-spec-container-aws-profiles.md`, `plans/070-build-nexus-plugins.md`
  (untracked in the user's checkout)

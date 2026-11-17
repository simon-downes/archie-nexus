# 092 — Brain browser in the web client

Status: planning
Branch: build-it/092-brain-browser (base: main @ fc1f141)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/092-brain-browser

## Objective

The brain (`~/.nexus/brain`, or `ARCHIE_BRAIN_DIR`) is only readable through the agent's
tools or a terminal. Add a read-only "brain browser" to the orchestrator's web pages: a
sidebar tree of directories and files, a main view that renders Markdown as HTML with
syntax-highlighted code, and a search that uses the same ranking as the `brain_search`
tool.

## Context

User's words: "a sidebar with directories and files, main view of html rendered markdown -
ideally complete with syntax highlighting. search functionality that mirrors the logic of
the brain search tool."

Discovery:

- The web client is the orchestrator's page set (`orchestrator/src/archie_orchestrator/pages.py`
  route table; handlers in `web.py` and `insights_web.py`; Jinja templates under
  `templates/viewer/` and `templates/insights/`; nimble.css + htmx vendored; strict
  `VIEWER_CSP` with no inline script or style). Pages are read-only and localhost-only.
- `transcript.render_markdown` already renders untrusted Markdown safely (markdown-it
  `js-default`: raw HTML escaped, unsafe links dropped, images turned into links) with
  Pygments highlighting; `static/viewer/pygments.css` holds light and dark styles.
- `brain_search` (`agent/src/archie_agent/exec/tools/brain.py`) is a thin wrapper over
  `archie_shared.brain.search(query, limit)`. The orchestrator already depends on
  `archie-shared`, so the browser can call the same function: identical ranking by
  construction.
- `search` shells out to `rg`. The orchestrator runs on the host, where `rg` may not be
  installed; `subprocess.run` would raise `FileNotFoundError`.
- `search` calls `ensure_fresh_index`, which may rewrite `index.yaml` (under the brain lock)
  and creates the brain root if missing. The agent tool has the same side effect.
- `brain_root()` resolves the same directory on the host as the container sees (the
  home directory is mounted; `ARCHIE_BRAIN_DIR` is forwarded).
- The live brain holds Markdown entries plus non-Markdown files (`.py`, `.tf`, `.sh`), a
  root `SOUL.md`, a root `index.yaml` (derived) and possibly `.git`.
- Pattern for an htmx search page: `session_list_page` in `web.py` with
  `templates/viewer/list.html` and `_results.html` (fragment on `HX-Request`, full page on
  history restore, `Vary` header). Tests: `tests/test_web_session_list.py`.

## Baseline

- Check commands: `scripts/dev test`, `scripts/dev ruff check .`, `scripts/dev ruff format --check <files>`
- Setup: `scripts/dev sync`
- Setup leftovers: none (`.venv-archie/` and caches are gitignored)
- Failing before this work: `tests/test_ws_integration.py` (most tests, failures and
  errors), `tests/test_host_config.py::test_the_agent_syncs_while_it_runs`,
  `tests/test_question_tool.py::test_manual_compaction_emits_working` and a few others in
  those files: all `concurrent.futures` timeouts from the container environment. Full run:
  17 failed, 9 errors, 5616 passed. Ruff check passes.
- Not included: `cli/src/archie_cli/tui/pane.py`, `tests/test_tui_app.py` (modified),
  `.DS_Store`, `plans/060-spec-container-aws-profiles.md`, `plans/070-build-nexus-plugins.md`
  (untracked) in the user's checkout; unrelated to this build.

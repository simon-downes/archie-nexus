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

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Where the browser lives | Orchestrator pages at `/brain` and `/brain/{path}`, read on the host from `brain_root()` | An agent-side route proxied per session; a separate app | The web client is the orchestrator's page set; the host resolves the same brain directory, so no running session is needed | self |
| D2 | How search mirrors `brain_search` | Call `archie_shared.brain.search(q, 50)` directly | A reimplementation or a client-side index | Same function as the tool, so ranking, excerpts and index freshness can't drift | self |
| D3 | Result limit | 50 (the function's cap) | The tool's default 10; a user-set limit | A page can show more than an agent wants in context; the order is the same, so the first 10 match the tool's default answer | self |
| D4 | `rg` missing on the host | `search` falls back to a pure-Python body scan of indexed entries when `shutil.which("rg")` is None; the agent path is unchanged when `rg` exists | Require `rg` and show an error; always use Python | The orchestrator runs on the user's machine, where `rg` is not a declared dependency; `rg` is only a prefilter, since matches are re-checked against the body in Python | self |
| D5 | Which files the tree shows | Every regular file and directory under the root except dot-names (`.git`, `_atomic_write` temp files), the root `index.yaml` and symlinks | Only indexed entries | The brain holds non-entry files (`.py`, `.tf`, `.sh`) and root guidance (`BRAIN.md`, `SOUL.md`) that the user asked to browse as "directories and files"; `index.yaml` is derived; symlinks could point outside the root | self |
| D6 | Rendering Markdown | Reuse `transcript.render_markdown` (raw HTML escaped, Pygments fences) and `static/viewer/pygments.css` | A new renderer or client-side highlighting | Brain files are written by agents and treated as untrusted; the renderer is already safe under `VIEWER_CSP` | self |
| D7 | Frontmatter display | A valid entry (`parse_entry`) shows name as title, summary, tags and updated in a header; the body is rendered without the YAML. A `.md` file outside the root guidance names that fails `parse_entry` shows a "not indexed" notice and renders the whole text | Render raw YAML; hide the problem | Makes the silent-drop defect (noted in the brain-search roadmap entry) visible while reading | self |
| D8 | Non-Markdown files | Highlight with the Pygments lexer for the filename (fallback plain text); binary (NUL byte or not UTF-8) and files over 1 MiB show a notice with the size | Refuse them; download links | The tree lists them (D5), so selecting one must show something; 1 MiB keeps a render well under a second | self |
| D9 | Navigation | Plain links with full page loads; `brain.js` only scrolls the current tree item into view. The tree uses `<details>` with the current file's ancestors open | htmx swaps of the main pane | A full load keeps the tree's current marker correct with no client state; the brain is small (78 files today) | self |
| D10 | Search UI | A search box at the top of the sidebar; results replace the main pane, via htmx on input (fragment on `HX-Request`) and a plain GET otherwise, following `session_list_page` | Results in the sidebar | The main pane has room for summary and excerpt; the pattern exists | self |
| D11 | Directory URLs | `/brain/<dir>` lists the directory's children in the main pane; breadcrumbs link to each ancestor | 404 for directories | Breadcrumbs need a target | self |
| D12 | Side effects of browsing | Tree and file views only read, and never create the root. Search may rewrite `index.yaml` and create the root, as the tool does | Read-only search via a new non-writing search function | D2: one search function; the rewrite is the same freshness behaviour the agent relies on | self |

## Requirements

- MUST serve `GET /brain` with a sidebar tree of the brain root: directories before files,
  each group sorted case-insensitively, excluding dot-names, the root `index.yaml` and
  symlinks.
  - AC: with `a/z.md`, `a/B.md`, `c.md`, `.git/x`, `index.yaml` and a symlink, the tree
    lists `a` then `c.md`, and in `a` `B.md` then `z.md`; `.git`, `index.yaml` and the
    symlink are absent.
- MUST render a Markdown file at `GET /brain/<path>` as HTML with raw HTML escaped and
  fenced code highlighted with Pygments classes.
  - AC: a body containing `<script>x</script>` appears escaped; a fenced `python` block
    produces `<pre class="highlight">` with Pygments spans; the page links
    `/static/viewer/pygments.css`.
- MUST show a valid entry's name, summary, tags and updated in a header instead of the raw
  frontmatter, and show a "not indexed" notice for a non-guidance `.md` that fails
  `parse_entry`.
  - AC: an entry's page contains its name in `<h1>`, its tags and summary, and not the line
    `summary:`; a `.md` without frontmatter shows the notice; root `SOUL.md` doesn't.
- MUST show a non-Markdown text file highlighted by its filename's lexer, and a notice with
  the size instead of content for a binary file or one over 1 MiB.
  - AC: `run.py` renders with Pygments spans; a file with a NUL byte and a 1 MiB + 1 byte
    file each show the notice and none of their content.
- MUST list a directory's children at `GET /brain/<dir>`, with breadcrumbs linking each
  ancestor.
  - AC: `/brain/a` lists links to `/brain/a/B.md` and `/brain/a/z.md`; a file page in `a/`
    has a breadcrumb link to `/brain/a`.
- MUST return 404 for a path that is missing, contains a dot-name component (including
  `..`), is the root `index.yaml`, or passes through a symlink.
  - AC: each of `/brain/missing.md`, `/brain/.git/config`, `/brain/a/%2e%2e/%2e%2e/etc`,
    `/brain/index.yaml` and `/brain/link/x.md` (link → outside dir) returns 404.
- MUST answer `GET /brain?q=<terms>` with results in the same order and with the same
  paths as `archie_shared.brain.search(q, 50)`, each showing name (linked), path, summary,
  tags and excerpt; with `HX-Request` (not a history restore) only the results fragment.
  - AC: the result paths parsed from the page equal `[h["path"] for h in search(q, 50)]`
    for a fixture where ranking depends on name, tags and body; the fragment starts with
    `<section id="brain-main"` and has no `<html`; `Vary` is
    `HX-Request, HX-History-Restore-Request`.
- MUST return the same search results without `rg` on `PATH` as with it.
  - AC: in `tests/test_brain.py`, with `shutil.which` patched to return None, `search`
    returns the same list (paths, scores rounded, excerpts) as the `rg` path on a fixture
    with body-only, name and tag matches.
- MUST render `/brain` without error when the root is missing or empty, without creating
  it.
  - AC: with `ARCHIE_BRAIN_DIR` pointing at a missing directory, `/brain` returns 200 with
    "The brain is empty" and the directory still doesn't exist.
- MUST serve every brain page with `VIEWER_CSP` and link it from the viewer's top-bar nav
  and the console header.
  - AC: the CSP header equals `VIEWER_CSP`; `/sessions` and `/` contain `href="/brain"`.

Out of scope: editing, creating or deleting brain files; a raw/download view; heading
anchors; images; authentication (the pages share the orchestrator's localhost-only,
unauthenticated model).

## Design

### Overview

A new page module `brain_web.py` reads the brain root on the host. It builds a sorted
tree, renders one file or directory in the main pane, and runs the shared `search` for
`?q=`. Templates extend `viewer/base.html`; a two-column layout puts the tree and search
box in a sticky sidebar.

### Stack

All reused: Starlette routes, Jinja templates, htmx, nimble.css, markdown-it-py and
Pygments (already orchestrator dependencies). No new dependency.

### Code structure

- `orchestrator/src/archie_orchestrator/brain_web.py` (new): `index_page` (`GET /brain`,
  landing or search), `path_page` (`GET /brain/{path:path}`, file or directory), and
  helpers `resolve_brain_path(rel) -> Path | None`, `build_tree(root, current) -> list[Node]`,
  `render_file(path) -> dict`. Renders with `web._viewer_response` / `web._render_viewer`
  (follow `insights_web.py`, which imports them).
- `orchestrator/src/archie_orchestrator/transcript.py`: add `highlight_file(text, filename)`
  using `get_lexer_for_filename` with a `TextLexer` fallback, returning the same
  `<pre class="highlight">` shape as `highlight_code`.
- `orchestrator/src/archie_orchestrator/pages.py`: two routes, and the docstring's route list.
- Templates (new) under `templates/brain/`: `base.html` (layout, sidebar, search form,
  includes `_tree.html` and the main block), `_tree.html` (recursive macro), `_main.html`
  dispatching on `view` (`landing`, `file`, `dir`, `results`), `not_found.html`.
- Static (new): `static/brain/brain.css`, `static/brain/brain.js`.
- `templates/viewer/base.html`, `templates/base.html`: add the Brain link.
- `shared/src/archie_shared/brain.py`: split the `rg` prefilter into `_body_matches(root,
  terms, valid) -> dict[str, set[str]]` with an `rg` branch and a Python branch.
- Tests: `tests/test_web_brain.py` (new), `tests/test_brain.py` (fallback test).
- Docs: `docs/architecture.md` (Pages table and a paragraph after the viewer paragraph),
  `README.md` (one sentence near the insights URL).

### Data model

Tree `Node` (a frozen dataclass in `brain_web.py`): `name: str`, `rel: str` (POSIX path from
the root), `is_dir: bool`, `children: list[Node]` (empty for files), `open: bool` (an
ancestor of, or equal to, the current path), `current: bool`. Built per request; nothing
is stored.

### Data flow

- File page: validate path → `stat` → size check → read bytes → binary check → decode →
  Markdown (`.md`, `.markdown`) via `parse_entry` + `render_markdown`, else
  `highlight_file` → template. Tree built in the same worker thread (`asyncio.to_thread`).
- Search: `q.strip()` empty → landing. Otherwise `search(q, 50)` in a worker thread; the
  tree is built too unless the request is an htmx fragment.

### Error handling

- Invalid or missing path, dot-name, symlink, root `index.yaml` → 404 page in brain chrome.
- `OSError` reading a file (permissions, race with deletion) → 404.
- `BrainError` from `search` (`rg` exits 2, empty query after trimming can't happen) →
  notice in the results pane, status 200.
- Missing root → empty tree with "The brain is empty"; tree building never calls `mkdir`.
- An `OSError` while walking a subdirectory (permission denied) skips that directory.

### Non-functional

- Security: path resolution compares `(root / rel).resolve()` with the lexical path and
  rejects any mismatch (symlink anywhere in the path) and any dot-name or empty component.
  Markdown raw HTML is escaped by `render_markdown`; `VIEWER_CSP` forbids inline script.
  The pages are unauthenticated like every other page; the orchestrator binds to loopback
  by default (README Configuration).
- Performance: tree walk and file read per request; with 78 files this is a few
  milliseconds. The 1 MiB cap bounds a single render.

## Milestones

### [ ] M1 — Browse the tree and read a Markdown entry

- **Approach:** Add `brain_web.py` with `index_page` (landing only), `path_page` (files
  only, Markdown only in this milestone), `resolve_brain_path` and `build_tree`. Templates
  `brain/base.html`, `_tree.html`, `_main.html`, `not_found.html`; `brain.css` for the
  two-column layout (sidebar `position: sticky`, own scroll; stacks under 50rem) and the
  entry header; `brain.js` scrolls `[aria-current="page"]` into view. Link
  `/static/viewer/pygments.css`. Add routes to `pages.py` and nav links to both base
  templates. Follow `web.session_detail_page` for rendering in a worker thread with the CSP
  header, and `templates/viewer/list.html` for the template shape. ⚠️ `{path:path}` gets
  the URL-decoded path, so `%2e%2e` arrives as `..` and must be rejected by the component
  check.
- **Test seam:** HTTP through `httpx.AsyncClient(ASGITransport(app))` with `ARCHIE_BRAIN_DIR`
  set to a `tmp_path` (as in `tests/test_web_session_list.py`).
- **Edge cases:** missing root → "The brain is empty", no mkdir; symlink in tree → not
  listed; symlink in URL → 404; invalid entry `.md` → notice; root `SOUL.md` → no notice.
- **Deliverable:** `/brain` shows the tree; clicking a `.md` shows rendered, highlighted
  Markdown with its metadata header.
- **Verify:** `scripts/dev test tests/test_web_brain.py` passes; `scripts/dev ruff check` and
  `ruff format --check` on the touched files pass.
- **Outcome:**

### [ ] M2 — Other files and directories

- **Approach:** Extend `path_page`: directories render a child list (`view="dir"`);
  non-Markdown files go through `transcript.highlight_file`; binary and >1 MiB files show
  the size notice. Breadcrumbs on file and directory views.
- **Test seam:** as M1.
- **Edge cases:** unknown extension → plain text; UTF-8 decode error → binary notice;
  empty file → "Empty file".
- **Deliverable:** every item in the tree opens to something readable.
- **Verify:** `scripts/dev test tests/test_web_brain.py` passes; ruff as M1.
- **Outcome:**

### [ ] M3 — Search

- **Approach:** In `shared/brain.py`, extract `_body_matches` with the Python branch when
  `shutil.which("rg")` is None (match each indexed entry's parsed body against the terms,
  lower-cased, as the existing re-check does). In `index_page`, run `search(q, 50)` and
  render `view="results"`: name link, path, summary, tags, excerpt with matched terms in
  `<mark>` (escape first, then wrap; case-insensitive), score to 1 decimal, updated date.
  Sidebar search form follows `templates/viewer/list.html` (`hx-get="/brain"`,
  `hx-target="#brain-main"`, `hx-swap="outerHTML"`, `hx-push-url="true"`, delay 250 ms);
  fragment and `Vary` logic follow `session_list_page`. Update `docs/architecture.md` and
  `README.md`.
- **Test seam:** HTTP as M1; `archie_shared.brain.search` for the fallback.
- **Edge cases:** whitespace-only `q` → landing; no hits → "No entries match"; `BrainError`
  → notice; terms containing HTML characters are escaped in the excerpt.
- **Deliverable:** typing in the sidebar box shows ranked results identical to
  `brain_search`.
- **Verify:** `scripts/dev test tests/test_web_brain.py tests/test_brain.py` passes; ruff as M1.
- **Outcome:**

## Deviations

## Risks

- The Python fallback lower-cases with `str.lower`, while `rg -i` uses Unicode simple case
  folding; for rare characters the prefilter could differ. Results are re-checked with
  `str.lower` either way, so the fallback can only find more, never fewer, of the matches
  the final check accepts.
- A search from the page can rewrite `index.yaml` while an agent writes the brain; both go
  through the existing brain lock.

## Review

## Outcome

PR: pending
CI: pending

## Follow-ups


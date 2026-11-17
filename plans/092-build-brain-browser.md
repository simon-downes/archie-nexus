# 092 — Brain browser in the web client

Status: reviewing
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
  those files: all `concurrent.futures` timeouts from the container environment; and
  `tests/test_local_api.py::test_the_agents_tcp_app_has_no_local_routes` (no Kiro key). Full run:
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
| D4 | `rg` missing on the host | `search` falls back to a pure-Python body scan of indexed entries when `shutil.which("rg")` is None, skipping entries with a dot-name path component or a symlink in their path (as an `rg` directory walk skips hidden files and doesn't follow symlinks); the agent path is unchanged when `rg` exists. Accepted differences: the fallback ignores `.gitignore`/`.ignore` rules and uses `str.lower` instead of `rg`'s case folding | Require `rg` and show an error; always use Python | The orchestrator runs on the user's machine, where `rg` is not a declared dependency; `rg` is only a prefilter, since matches are re-checked against the body in Python | self |
| D5 | Which files the tree shows | Every regular file and directory under the root except dot-names (`.git`, `_atomic_write` temp files), the root `index.yaml` and symlinks | Only indexed entries | The brain holds non-entry files (`.py`, `.tf`, `.sh`) and root guidance (`BRAIN.md`, `SOUL.md`) that the user asked to browse as "directories and files"; `index.yaml` is derived; symlinks could point outside the root | self |
| D6 | Rendering Markdown | Reuse `transcript.render_markdown` (raw HTML escaped, Pygments fences) and `static/viewer/pygments.css` | A new renderer or client-side highlighting | Brain files are written by agents and treated as untrusted; the renderer is already safe under `VIEWER_CSP` | self |
| D7 | Frontmatter display | A valid entry (`parse_entry`) shows name as title, summary, tags and updated in a header; the body is rendered without the YAML. A `.md` file outside the root guidance names that fails `parse_entry` shows a "not indexed" notice and renders the whole text | Render raw YAML; hide the problem | `_records` silently skips a `.md` that fails `parse_entry`, so an agent's malformed write is invisible to search; the notice makes it visible while reading | self |
| D8 | Non-Markdown files | Highlight with the Pygments lexer for the filename (fallback plain text); binary (NUL byte or not UTF-8) and files over 1 MiB show a notice with the size | Refuse them; download links | The tree lists them (D5), so selecting one must show something; 1 MiB keeps a render well under a second | self |
| D9 | Navigation | Plain links with full page loads; `brain.js` only scrolls the current tree item into view. The tree uses `<details>` with the current file's ancestors open | htmx swaps of the main pane | A full load keeps the tree's current marker correct with no client state; the brain is small (78 files today) | self |
| D10 | Search UI | A search box at the top of the sidebar; results replace the main pane, via htmx on input (fragment on `HX-Request`) and a plain GET otherwise, following `session_list_page` | Results in the sidebar | The main pane has room for summary and excerpt; the pattern exists | self |
| D11 | Directory URLs | `/brain/<dir>` lists the directory's children in the main pane with D5's exclusions; breadcrumbs link to each ancestor. `/brain/` (empty path) is the landing page | 404 for directories | Breadcrumbs need a target | self |
| D12 | Side effects of browsing | Nothing writes: tree and file views only read, and search calls `search(q, 50, refresh_index=False)`, which rebuilds a stale index in memory (`current_records`) without the lock, `index.yaml` or `mkdir` | Letting search refresh `index.yaml` as the tool does | The host's `flock` may not exclude the container's across a Docker Desktop bind mount, and the host resolves container-absolute symlinks differently, so a host rewrite could drop entries from the agent's index; one ranking function is kept (D2) | self (changed in code review) |
| D13 | Search hits the browser won't show (an indexed entry under a dot-directory, a dot-file or a symlink, which `_candidate` accepts but D5 excludes) | Listed in rank order like any hit, with the name unlinked and a "not browsable" label | Filtering them out; linking to a 404 | Keeps the result list identical to `search` (D2) without dead links |

## Requirements

- MUST serve `GET /brain` with a sidebar tree of the brain root: directories before files,
  each group sorted case-insensitively, excluding dot-names, the root `index.yaml` and
  symlinks. [M1]
  - AC: with `a/z.md`, `a/B.md`, `c.md`, `.git/x`, `index.yaml` and a symlink, the tree
    lists `a` then `c.md`, and in `a` `B.md` then `z.md`; `.git`, `index.yaml` and the
    symlink are absent.
- MUST render a Markdown file at `GET /brain/<path>` as HTML with raw HTML escaped and
  fenced code highlighted with Pygments classes. [M1]
  - AC: a body containing `<script>x</script>` appears escaped; a fenced `python` block
    produces `<pre class="highlight">` with Pygments spans; the page links
    `/static/viewer/pygments.css`.
- MUST show a valid entry's name, summary, tags and updated in a header instead of the raw
  frontmatter, and show a "not indexed" notice for a non-guidance `.md` that fails
  `parse_entry`. [M1]
  - AC: an entry's page contains its name in `<h1>`, its tags and summary, and not the line
    `summary:`; a `.md` without frontmatter shows the notice; root `SOUL.md` doesn't.
- MUST show a non-Markdown text file highlighted by its filename's lexer, and a notice with
  the size instead of content for a binary file or one over 1 MiB. [M2]
  - AC: `run.py` renders with Pygments spans; a file with a NUL byte and a 1 MiB + 1 byte
    file each show the notice and none of their content.
- MUST list a directory's children at `GET /brain/<dir>`, with breadcrumbs linking each
  ancestor, applying the tree's exclusions. [M2]
  - AC: `/brain/a` lists links to `/brain/a/B.md` and `/brain/a/z.md` and not `a/.hidden`; a file page in `a/`
    has a breadcrumb link to `/brain/a`.
- MUST return 404 for a path that is missing, contains a dot-name component (including
  `..`), is the root `index.yaml`, or passes through a symlink. [M1]
  - AC: each of `/brain/missing.md`, `/brain/.git/config`, `/brain/a/%2e%2e/%2e%2e/etc`,
    `/brain/index.yaml` and `/brain/link/x.md` (link → outside dir) returns 404.
- MUST answer `GET /brain?q=<terms>` with results in the same order and with the same
  paths as `archie_shared.brain.search(q, 50)`, each showing name (linked), path, summary,
  tags and excerpt; with `HX-Request` (not a history restore) only the results fragment.
  [M3]
  - AC: the result paths parsed from the page equal `[h["path"] for h in search(q, 50)]`
    for a fixture where ranking depends on name, tags and body; a hit for a symlinked
    entry is listed with no link (D13); an excerpt containing `&` with the query `amp`
    renders `&amp;` intact; the fragment starts with
    `<section id="brain-main"` and has no `<html`; `Vary` is
    `HX-Request, HX-History-Restore-Request`.
- MUST return the same search results without `rg` on `PATH` as with it, for entries with
  no dot-name path component and no ignore-file rules (D4). [M3]
  - AC: in `tests/test_brain.py`, with `shutil.which` patched to return None, `search`
    returns the same list (paths, scores rounded, excerpts) as the `rg` path on a fixture
    with body-only, name and tag matches and an entry under `.hidden/` (absent from both).
- MUST render `/brain` without error when the root is missing or empty, without creating
  it. [M1]
  - AC: with `ARCHIE_BRAIN_DIR` pointing at a missing directory, `/brain` returns 200 with
    "The brain is empty" and the directory still doesn't exist.
- MUST serve every brain page with `VIEWER_CSP` and link it from the viewer's top-bar nav
  and the console header. [M1]
  - AC: the CSP header equals `VIEWER_CSP`; `/sessions` and `/` contain `href="/brain"`.

Out of scope: editing, creating or deleting brain files; a raw/download view; heading
anchors; images; authentication (the pages share the orchestrator's localhost-only,
unauthenticated model).

## Design

### Overview

A new page module `brain_web.py` reads the brain root on the host. It builds a sorted
tree, renders one file or directory in the main pane, and runs the shared `search` for
`?q=`. Templates extend `viewer/base.html`; a two-column layout puts the tree and search
box in a sticky sidebar. `brain/base.html` extends `viewer/base.html`, which gains a
`brand` block (default "session logs") so brain pages read "Archie brain" and set their
own title.

### Stack

All reused: Starlette routes, Jinja templates, htmx, nimble.css, markdown-it-py and
Pygments (already orchestrator dependencies). No new dependency.

### Code structure

- `orchestrator/src/archie_orchestrator/brain_web.py` (new): `index_page` (`GET /brain`,
  landing or search), `path_page` (`GET /brain/{path:path}`, file or directory), and
  helpers `resolve_brain_path(rel) -> Path | None`, `build_tree(root, current) -> list[Node]`,
  `render_file(path) -> dict`. Renders in a worker thread with `web._render_viewer` and
  returns `HTMLResponse` with the `VIEWER_CSP` header (follow `web.session_detail_page`);
  the 404 page uses `web._viewer_response` (as `insights_web._not_found` does).
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
- Search: `q.strip()` empty → landing (so clearing the box swaps the main pane to the
  landing view and pushes `/brain`; intended). Otherwise `search(q, 50)` in a worker thread; the
  tree is built too unless the request is an htmx fragment.

### Error handling

- Invalid or missing path, dot-name, symlink, root `index.yaml` → 404 page in brain chrome.
- `OSError` reading a file (permissions, race with deletion) → 404.
- `BrainError` or `OSError` from `search` (`rg` exits 2; the host user can't create the
  root, the lock file or `index.yaml`) → "Search failed: <message>" notice in the results
  pane, status 200, logged at warning.
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

### [x] M1 — Browse the tree and read a Markdown entry

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
- **Verify:** `scripts/dev test tests/test_web_brain.py` passes; `scripts/dev ruff check
  orchestrator shared tests` and `scripts/dev ruff format --check orchestrator shared tests`
  pass.
- **Outcome:** `brain_web.py` (tree, path guard, Markdown view), `brain/page.html`,
  `_tree.html`, `_main.html` (the 404 is a `missing` view of `_main.html` inside the brain
  layout instead of a separate `not_found.html`), `brain.css`, `brain.js`, routes, nav
  links and a `brand`/`brand_href` block in `viewer/base.html`. `highlight_file` was added
  to `transcript.py` now so non-Markdown files open as plain highlighted text rather than
  erroring. `scripts/dev test tests/test_web_brain.py`: 20 passed; `tests/test_web_*.py`:
  220 passed; ruff check and format --check clean.

### [x] M2 — Other files and directories

- **Approach:** Extend `path_page`: directories render a child list (`view="dir"`);
  non-Markdown files go through `transcript.highlight_file`; binary and >1 MiB files show
  the size notice. Breadcrumbs on file and directory views.
- **Test seam:** as M1.
- **Edge cases:** unknown extension → plain text; UTF-8 decode error → binary notice;
  empty file → "Empty file".
- **Deliverable:** every item in the tree opens to something readable.
- **Verify:** `scripts/dev test tests/test_web_brain.py` passes; ruff as M1.
- **Outcome:** `_dir_view` (non-recursive, same `_shown` filter as the tree), `_crumbs`,
  `_text` (NUL byte or invalid UTF-8 → binary), `MAX_FILE_BYTES` = 1 MiB (a file of exactly
  1 MiB still shows), "Empty file." notice; directory and file views share the breadcrumb
  line. `scripts/dev test tests/test_web_brain.py`: 24 passed; ruff check and format
  --check clean.

### [x] M3 — Search

- **Approach:** In `shared/brain.py`, extract `_body_matches` with the Python branch when
  `shutil.which("rg")` is None (match each indexed entry's parsed body against the terms,
  lower-cased, as the existing re-check does). In `index_page`, run `search(q, 50)` and
  render `view="results"`: name link (or unlinked with "not browsable", D13), path,
  summary, tags, excerpt with matched terms in `<mark>` (find case-insensitive match spans
  on the raw excerpt, then escape each segment and wrap the matches, so `amp` never
  matches inside an entity), score to 1 decimal, updated date.
  Sidebar search form follows `templates/viewer/list.html` (`hx-get="/brain"`,
  `hx-target="#brain-main"`, `hx-swap="outerHTML"`, `hx-push-url="true"`, delay 250 ms);
  fragment and `Vary` logic follow `session_list_page`. Update `docs/architecture.md` and
  `README.md`.
- **Test seam:** HTTP as M1; `archie_shared.brain.search` for the fallback.
- **Edge cases:** whitespace-only `q` → landing; no hits → "No entries match"; `BrainError`
  → notice; terms containing HTML characters are escaped in the excerpt.
- **Deliverable:** typing in the sidebar box shows ranked results identical to
  `brain_search`.
- **Verify:** `scripts/dev test tests/test_web_brain.py tests/test_brain.py tests/test_exec_tools.py`
  passes, then the full `scripts/dev test` shows no failures beyond the Baseline's (shared
  change, CONTRIBUTING.md); ruff commands as M1.
- **Outcome:** `shared/brain.py`: `_body_matches` (the old `rg` block, unchanged) and
  `_python_body_matches` behind `shutil.which("rg")`. `brain_web.py`: `_results_view`,
  `_hit`, `_mark`, `_index_body`; the sidebar search box and `results` view. Docs:
  architecture Pages table and viewer paragraph, README. Focused run
  (`test_web_brain.py`, `test_brain.py`, `test_exec_tools.py`): 98 passed. Full
  `scripts/dev test`: 17 failed, 9 errors, 5650 passed, the same failures as the Baseline.
  Ruff check and format clean.

## Deviations

- Code review: browser search no longer refreshes `index.yaml` (D12 changed). `search`
  gained a keyword `refresh_index` (default True, so the agent's tool is unchanged) and
  `shared/brain.py` a public `current_records()`; `search` returns `[]` early when there
  are no records. Prompted by the risk reviewer's questions on cross-VM locking and
  host-side symlink resolution.

- M3: the Python fallback also skips symlinked entries. `rg` doesn't follow symlinks
  in a directory walk, so a symlinked entry never gets a body match there; without the
  skip the fallback would rank it higher. D4 updated; the fallback test covers it.

## Risks

- Accepted fallback differences (D4): with `rg` absent, an entry that `rg` would skip
  through `.gitignore`/`.ignore`, or whose match depends on Unicode case folding beyond
  `str.lower`, can gain a body match. The common host case (no `rg`, no ignore files) is
  identical.

## Review

- Plan review: 1 pass, no ❌. Fixed: M3 Verify now runs `tests/test_exec_tools.py` and the
  full suite (shared change); exact ruff commands; milestone tags on each MUST; the
  fallback MUST narrowed to entries without dot-name components or ignore files, with the
  fallback skipping dot-name paths (D4) and the gitignore/case-folding differences
  accepted under Risks; D13 added for hits the browser can't show; D7's defect stated
  inline; D11 applies D5's exclusions and makes `/brain/` the landing page; Code structure
  names `web.session_detail_page` as the render pattern; `OSError` from `search` handled;
  `<mark>` spans found on raw text before escaping; clearing the box pushing `/brain`
  stated as intended; `brain/base.html` extends `viewer/base.html` with a new `brand`
  block. No findings rejected.
- Code review: `general` and `risk` (risk: URL paths become filesystem paths; the shared
  search function changed), one round, no ❌.
  - Fixed: ⚠️ untested `OSError` paths, now covered by tests with a mode-000 file and
    directory; ℹ️ the fallback test now asserts `rg` is on PATH; ℹ️ the search-failure
    test no longer patches the module's own `search` (a real unreadable file makes `rg`
    exit 2; `subprocess.run` is patched only at the process boundary); ℹ️ (both
    reviewers) the root `index.yaml` check now uses `samefile`, so `INDEX.yaml` on a
    case-insensitive filesystem is a 404 (a hard-link test stands in for that); ℹ️ a
    comment in `web.py` points to the `brain_href` global.
  - ❓ risk (host/container `flock` and symlink divergence): answered by changing D12, so
    the host never writes the index; tested by `test_search_never_writes_the_index` and
    `test_search_without_refreshing_ranks_the_same_and_writes_nothing`.
  - ❓ general (a valid root `SOUL.md` shows an entry header; a non-Markdown file with
    frontmatter shows as source): intended. The header only describes the file's own
    frontmatter, and non-Markdown files are shown as source by D8. No change.
  - Rejected: the risk reviewer's side note that `_GUIDANCE_NAMES` is case-sensitive, so a
    hand-typed `/brain/soul.md` on macOS would show the "not indexed" notice. Tree links
    use on-disk names, and the brain's own `_ROOT_EXCLUDED_NAMES` compares on-disk
    names the same way, so the notice is right for every link the browser produces.

## Outcome

PR: pending
CI: pending

## Follow-ups


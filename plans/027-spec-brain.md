# 027 — SPEC: Brain (curated memory / knowledge store)

## Objective

Give archie-nexus a **brain**: a durable, git-versioned store of curated knowledge
(people, projects, domain knowledge) that the agent can search and read during a session
and deliberately write to. It is the long-term memory layer that persists across sessions
and containers. v1 is **curated-only** — the agent writes entries when it judges something
worth remembering; there is no automatic memory extraction yet.

## Context

nexus has *zero* brain implementation today. All references are aspirational (`VISION.md`)
or explicitly deferred (`plans/done/007-tools.md`). The brain is the last major capability
from prior archie generations not yet ported.

This is a **first-principles** design, not a lift-and-shift. Three prior implementations
exist (`archie-nextgen`, `archie-og`, `agent-kit`) and were studied as *inspiration only*:

- Across all three, **SQLite (`brain.db`) is never on the retrieval hot path** — it is a
  write-only access log for observability. Ranking runs on an in-memory `index.yaml` +
  ripgrep. Scale is *hundreds* of human-curated entries.
- Prior impls spawn a ripgrep subprocess **per search term** (og/agent-kit also per file for
  excerpts). We collapse body search to a **single** `rg` call.
- Priors worth keeping: git-versioned markdown + YAML frontmatter as the source of truth;
  lexical retrieval (rg + index + recency); tool-driven pull (agent decides when to
  read/write); **host owns git**; a rebuildable root `index.yaml` derived from frontmatter.
- Prior write APIs diverged: nextgen had a buggy full-replace `brain` tool; og/agent-kit had
  *no* write API (the LLM edited markdown directly then reindexed). We take a middle path
  (see Locked decisions D5/D10).

`VISION.md` is partly outdated and is **not** authoritative here: it places the brain at
`~/.archie/brain` mounted separately and routes everything through exec tools. Superseded —
nexus uses `ARCHIE_HOME_DIR` (default `~/.nexus`) and native/discrete tools.

**Locked decisions:**

- **D1 — Location & mount.** Brain is a git repo at `$ARCHIE_HOME_DIR/brain` (default
  `~/.nexus/brain`). Reuse the existing home mount — **no new Docker mount**.
- **D2 — Retrieval.** Lexical (`index.yaml` + ripgrep + recency decay). No embeddings /
  vector DB.
- **D3 — Scope.** Curated-only v1. Automatic memory-extraction is deferred; its future home
  `_auto/` stays reserved (unwritable in v1). One system dir **is** writable now: `_inbox/`
  (a staging area, e.g. research artifacts). Rule: `_`-prefixed dirs are reserved *except*
  those on an explicit `SYSTEM_DIRS` allow-list (v1: `_inbox`).
- **D4 — Tool style.** Brain tools are native/discrete tools (registered in the tool
  registry, not exec `@tool`s).
- **D5 — Dumb store + smart writer.** Three dedicated brain tools (`brain_write`,
  `brain_search`, `brain_read`). Surgical *updates* to existing entries reuse the existing
  `fs.edit` tool. To let `fs` reach the brain, relax the fs path restriction to multi-root
  (workspace + brain) via a shared IO engine — no duplicated file I/O.
- **D6 — Indexing.** Happens automatically in-container on any brain write/edit (pure file
  I/O, no git). A reindex hook lives in the shared write/edit path, keyed on "path is under
  the brain root." No model-visible index tool; no filesystem watcher; no checkpoint lag.
- **D7 — No SQLite in v1.** Search = in-memory index scan + a single `rg` call.
- **D8 — `updated` field.** Frontmatter carries `updated` **only**, and it is system-set by
  tooling — never model-set. The write/edit hook stamps `updated=now()` and reindexes. The
  index caches `updated`.
- **D9 — Taxonomy.** Categorized folders from v1: `people/`, `projects/`, `knowledge/`.
  `item_type` = top-level dir. System dir `_inbox/` (item_type `_inbox`) is a writable staging
  area outside the taxonomy. Root `index.yaml`. Frontmatter is the source of truth.
- **D10 — Write API.** `brain_write(path, name, summary, tags, content)` — model supplies an
  explicit `path` (nextgen-style). Taxonomy is guided by `BRAIN.md` conventions + tool
  guidance, **not** hard-enforced.
- **D-Git — Host-only git.** Commit on session-end in `stop_session`; optional periodic
  background commit. Failures are best-effort and logged, never fatal to a session.

**Depends on:** existing home mount (`lifecycle.py:96,114,124`), `config.home_dir()`
(`config.py:20`), `fs` tool resolve guard (`fs.py:75-81`), discrete-tool pattern
(`skills.py:135`).

**Constraints:**
- **Container never runs git.** `shared/brain/git.py` is physically present in-container via
  the read-only mount, but is never imported or invoked there — enforced by lazy import + an
  import-graph test (per AGENTS.md: never git from `/opt/archie`).
- **Multi-root fs stays a closed allow-list.** Workspace + brain only; everything else
  rejected. Under the brain root, the top-level segment must be a category or an allowed
  system dir (`_inbox`); other `_`-prefixed dirs stay reserved.
- **Ripgrep runs in-container.** Body search is one `rg` invocation per query.

## Requirements

### Store & layout

- MUST place the brain at `$ARCHIE_HOME_DIR/brain` (`home_dir() / "brain"`) and seed its
  file scaffold on first use, in-container, with no git operation.
  - AC: On a fresh env (no `~/.nexus/brain`), first `brain_write` seeds `BRAIN.md`, an empty
    `index.yaml`, and category dirs (files only, no git), then writes the entry.
  - Why: The git repo itself (`git init` + initial commit) is established host-side by
    `ensure_repo()` before the first commit; seeding files before the repo exists is fine —
    the first host commit picks them up.

- MUST store entries as markdown files with YAML frontmatter under top-level category dirs
  `people/`, `projects/`, `knowledge/`, where the top-level dir is the entry's `item_type`.
  - AC: An entry written to `people/x.md` has `item_type == "people"` in the index.

- MUST include frontmatter fields `name`, `summary`, `tags` (list), and `updated` (ISO-8601),
  with `updated` system-set.
  - AC: A model-supplied `updated` is ignored/overwritten with the system timestamp.

- MUST maintain a root `index.yaml` as a derived cache of each entry's path + ranking fields
  (`name`, `summary`, `tags`, `updated`, `item_type`), fully rebuildable from frontmatter.
  - AC: Deleting `index.yaml` and running the rebuild path reconstructs an equivalent index
    from frontmatter (frontmatter is the source of truth).

- MUST reserve `_`-prefixed dirs **except** those on the `SYSTEM_DIRS` allow-list (v1:
  `_inbox`); v1 tooling MUST NOT create or write non-allowed `_`-prefixed dirs.
  - AC: A `brain_write` to `_auto/x.md` is rejected; a `brain_write` to `_inbox/x.md` succeeds.

### Write

- MUST provide a `brain_write(path, name, summary, tags, content)` tool that creates or fully
  replaces an entry at the model-supplied brain-relative `path`, writing frontmatter
  (`name`, `summary`, `tags`, system-set `updated`) + `content` body.
  - AC: Handler on a tmp root produces the entry file and an index entry with a system
    `updated`.

- MUST reject `brain_write` paths that escape the brain root, or whose top-level segment is
  neither an allowed category nor an allowed system dir (`_inbox`) — with a clear error
  and no file created.
  - AC: `../escape.md`, `secrets/x.md`, and `_auto/x.md` are each rejected and no file is
    written; `_inbox/x.md` is accepted.
  - Why: Path resolution uses the resolve-then-`relative_to(root)` guard exactly as
    `fs._resolve_path` (`fs.py:75-81`), so symlink/`..` traversal is caught after resolution.

- MUST perform surgical edits to existing entries via the existing `fs.edit` tool (no
  separate brain-edit tool); `fs.edit`/`fs.write` MUST be able to target the brain root in
  addition to `/workspace`.
  - AC: `fs.edit` on an entry under the brain root succeeds and re-stamps `updated`.

- MUST enforce the same top-level-segment rules on `fs.write`/`fs.edit` under the brain
  root as `brain_write` enforces.
  - AC: A `fs.write`/`fs.edit` to a non-category, non-system-dir path (e.g. `_auto/`) under
    the brain root is rejected; `_inbox/` is accepted.
  - Why: The multi-root relaxation is scoped to *editing existing* entries, not authoring new
    taxonomy; creating entries is `brain_write`'s job.

- MUST re-stamp `updated` and update `index.yaml` for an entry on any write/edit under the
  brain root, before the tool returns.
  - AC: After `fs.edit` on a brain entry, the new `updated` is reflected in `brain_search`
    output immediately in the same session.

- MUST NOT perform any git operation or require network during indexing.
  - AC: The index code path contains no git/network calls (verified by inspection + test).

### Search & read

- MUST provide `brain_search(query, item_type=None, limit=None)` returning ranked matches
  drawn from (a) the in-memory index (name/summary/tags) and (b) a single ripgrep pass over
  entry bodies.
  - AC: A query matching two entries returns both, ranked.

- MUST rank by field-weighted lexical score plus a recency term derived from `updated`
  (weights `name ×3`, `tag ×2`, `summary ×1`, `body ×1`; recency a bounded additive decay;
  ties broken by `updated` desc).
  - AC: An entry whose *name* matches outranks one where only the *summary* matches; among
    equal lexical scores the more-recently-`updated` entry ranks first.

- MUST include per hit: `path`, `name`, `item_type`, `summary`, `updated`, and a short body
  excerpt when the body matched.
  - AC: A body-only term returns the entry with an excerpt.

- MUST use exactly one ripgrep subprocess per query (all terms in one call).
  - AC: The search runner reports exactly one `rg` invocation, asserted via a counting/patched
    subprocess runner in a unit test (not strace — rg runs in-container).

- MUST provide `brain_read(path)` returning the full entry (frontmatter + body) for an exact
  brain-relative path, erroring clearly if absent.
  - AC: `brain_read` on a missing path returns `Error: no brain entry at '<path>'`.

### Git (host-only)

- MUST commit brain changes on session end (`stop_session`) with a session-scoped message,
  running `ensure_repo()` (host `git init` if absent) first, and treating "nothing to commit"
  as success.
  - AC: Writing via a brain tool then stopping the session produces a commit; stopping a
    session with no brain changes produces no commit and no error.
  - Why: The commit MUST happen after `stop_container` so no in-container index write can
    race a half-written `index.yaml` into the commit.

- MAY run an optional periodic background commit on the orchestrator (mirroring the metrics
  writer task); when enabled it MUST be best-effort. Disabled by default in v1.
  - AC: With the flag off (default), no periodic commit task runs.

- MUST catch and log all git failures without failing the session or any tool call, and MUST
  NOT import or invoke git on the brain in-container.
  - AC: An import-graph test proves the container tool import graph never pulls in
    `brain/git.py`.
  - AC: A missing git binary / corrupt repo on session end is logged and the session still
    stops.

### Discoverability

- MUST register and advertise the brain tools to the model with guidance, and seed a
  `BRAIN.md` at the brain root documenting the taxonomy/conventions the agent should follow
  (soft guidance, per D10).
  - AC: The advertised tool set includes `brain_write`, `brain_search`, `brain_read`, each
    with a non-empty description.
  - AC: `BRAIN.md` is present after seeding.

## Technical Design

### Overview

Three homes, mirroring existing nexus conventions: a shared `brain/` subpackage (pure I/O +
host-only git), discrete brain tools in the agent, and a minimal multi-root refactor of the
existing `fs` tools so surgical edits reach the brain and trigger reindexing. Search is a
single ripgrep pass merged with an in-memory index scan; git is host-only.

### Architecture

- **Container (agent):** discrete `brain_*` tools + multi-root `fs` tools. All brain I/O
  (store/index/search/seed) is pure files — safe in-container.
- **Host (orchestrator):** git only. Commits the brain on session end; optional periodic
  commit task.
- **Shared state:** the brain repo files + `index.yaml` under `brain_root()`, on the home
  mount visible to both container (writes) and host (git).

### Components

**`shared/src/archie_shared/brain/`** (new subpackage, mirroring `credentials/` and
`session/` — a small package of `models.py` + logic modules):

- `models.py` — msgspec `Struct`s: `Frontmatter`, `IndexEntry`, `SearchHit`;
  `ITEM_TYPES = ("people", "projects", "knowledge")`; `RESERVED_PREFIX = "_"`;
  `SYSTEM_DIRS = ("_inbox",)` (writable `_`-dirs exempt from the reserved rule).
- `paths.py` — `brain_root()` (= `home_dir() / "brain"`), `resolve_brain_path()` (validate a
  brain-relative path: no traversal; top-level segment in `ITEM_TYPES` or `SYSTEM_DIRS`;
  reject any other `_`-prefixed dir), and the
  predicate `is_under_brain(abs_path)` used by the fs reindex hook.
- `store.py` — pure file I/O: `read_entry`, `write_entry` (frontmatter + body, system-stamps
  `updated`), `parse_frontmatter`, `split_frontmatter_body`.
- `index.py` — `load_index`, `rebuild_index` (walk categories → frontmatter → write
  `index.yaml`), `upsert_index_entry(path)` (single-entry incremental update),
  `remove_index_entry(path)`.
- `search.py` — `search(query, item_type, limit)`: scans the in-memory index for
  name/summary/tags hits, runs one `rg --json -e <t1> -e <t2> …` over category dirs for body
  hits, merges, scores, returns `list[SearchHit]`. The runner exposes an invocation count so
  tests can assert exactly one `rg` call.
- `git.py` — **host-only** helpers: `ensure_repo()` (host `git init` + initial commit if
  absent), `commit_all(message)`. Not imported at module load by any in-container tool —
  imported lazily only at orchestrator call sites.
- `seed.py` — `ensure_seed()`: creates `BRAIN.md` + empty `index.yaml` + category dirs (with
  `.gitkeep`) on first use. File-only — performs no git. Safe in-container.

Rationale: `shared/` is importable by both the agent (in-container tools) and the
orchestrator (host git). Search/index/store/seed are pure I/O → safe in-container. `git.py`
is the only host-only module.

**`agent/src/archie_agent/brain_tools.py`** (new): `create_brain_tools()` returning three
`ToolSpec`s (`brain_write`, `brain_search`, `brain_read`), following the `create_skill_tool`
pattern (`skills.py:135`). Handlers call into `archie_shared.brain.*`. Registered in
`harness.py` near the existing tool registration (~`harness.py:130`).

**`agent/src/archie_agent/exec/tools/fs.py`** (modified — multi-root refactor, D5): see Code
Structure below.

### Data Model

**Entry file** (markdown + YAML frontmatter), under `people/` | `projects/` | `knowledge/`:
```
---
name: "..."
summary: "..."
tags: [a, b]
updated: 2026-08-03T12:34:56+00:00   # system-set only
---
<body>
```
- `item_type` is derived from the top-level dir (not stored in frontmatter).
- Frontmatter is the source of truth.

**Root `index.yaml`** — derived cache, one record per entry: `path`, `name`, `summary`,
`tags`, `updated`, `item_type`. Fully rebuildable from frontmatter via `rebuild_index`.

**System dir (writable):** `_inbox/` — staging area outside the taxonomy (e.g. research
artifacts), on the `SYSTEM_DIRS` allow-list.

**Reserved:** all other `_`-prefixed dirs (e.g. `_auto/`) — never created/written by v1 tooling.

### Data Flow

**Write (create/replace) via `brain_write`:**
1. `resolve_brain_path(path)` → validate (traversal / category or system dir / reserved).
2. `ensure_seed()` if the scaffold is absent (file-only, no git).
3. `store.write_entry(...)` → writes frontmatter (system-stamped `updated`) + body.
4. `index.upsert_index_entry(path)` → incremental index update, before the tool returns.

**Edit (surgical) via `fs.edit`/`fs.write`:**
1. `_resolve_path` validates against the root list `[WORKSPACE, brain_root()]`; if under the
   brain root, also enforce category/system-dir/reserved rules.
2. On success, if `is_under_brain(resolved)`: shared hook re-stamps `updated` +
   `upsert_index_entry`. Single reindex seam (D6). Non-brain writes fire no hook.

**Search via `brain_search`:**
1. Tokenise query into terms `T`.
2. Scan in-memory index for name/summary/tags hits.
3. One `rg --json -e t1 -e t2 …` over category dirs for body hits. Multiple `-e` flags are an
   OR; parse each `match` event's `submatches` and attribute back to the query term matched,
   so the body component of the score is per-term (not a single lumped count).
4. Merge + score:
   ```
   field_score   = 3·(name hits) + 2·(tag hits) + 1·(summary hits) + 1·(body hits)
   recency_bonus = R_MAX · exp(-age_days / HALF_LIFE_DAYS)     # bounded additive
   score         = field_score + recency_bonus
   ```
   Constants (tunable, documented in code): `R_MAX = 2.0`, `HALF_LIFE_DAYS = 30`. Recency is
   a *bonus* that decides ties, never surfaces a zero-lexical entry. Sort by `score` desc,
   then `updated` desc. Entries with zero lexical hits are excluded.

**Session-end git (host):** after `stop_container`, `commit_all(f"session {session_id}")`
runs `ensure_repo()` then `git add -A` + `git commit`; "nothing to commit" = success; all in
try/except → log-and-continue.

### Code Structure

- **New:** `shared/src/archie_shared/brain/` (`__init__.py`, `models.py`, `paths.py`,
  `store.py`, `index.py`, `search.py`, `git.py`, `seed.py`) — see Components.
- **New:** `agent/src/archie_agent/brain_tools.py` — `create_brain_tools()`.
- **Modified:** `agent/src/archie_agent/exec/tools/fs.py`:
  - `read`/`write`/`edit` are `@tool`-decorated and exposed on **two** surfaces — native
    `ToolSpec`s *and* the exec Python sandbox. The reindex hook must live **inside the
    `@tool` function body** (not a registry wrapper) so it fires for both surfaces.
  - Replace the single `WORKSPACE` scoping in `_resolve_path` (`fs.py:46`) with a root list
    `[WORKSPACE, brain_root()]`. A path is valid if it resolves under *any* allowed root,
    using the existing resolve-then-`relative_to` guard (`fs.py:75-81`) per root. When
     resolved under the brain root, additionally enforce category/system-dir/reserved rules by
     reusing `resolve_brain_path`'s validator.
   - After a successful `write`/`edit` (`fs.py:379`, `fs.py:405`), if `is_under_brain(...)`,
    call the shared hook (re-stamp `updated` + `upsert_index_entry`).
- **Modified:** `agent/src/archie_agent/harness.py` — register the three brain tools
  (~`harness.py:130`).
- **Modified (host):** `orchestrator/src/archie_orchestrator/lifecycle.py` (`stop_session`,
  `:156`) and optionally `app.py` (`lifespan`, `:55`) for the periodic task.

### Non-Functional Concerns

- **Security/scoping:** multi-root fs stays a *closed* allow-list; brain path validation
  rejects traversal, top-levels that are neither a category nor an allowed system dir, and
  reserved `_`-prefixed dirs.
- **Performance:** one `rg` pass per search; incremental single-entry index upsert on write
  (not full rebuild). Full `rebuild_index` is reserved for the recovery path.
- **Concurrency:** two processes touch the brain — the container (file writes + index upsert)
  and the host (git commit). Git is read/commit only and runs **after** `stop_container`, so
  it never races an in-flight container write. Index upsert reads + rewrites `index.yaml`
  atomically (temp-file + rename, as `credentials/store.py:61` does), so an interleaved read
  sees a whole file. Otherwise single-writer per brain.
- **Observability:** git commits + reindex log at INFO/DEBUG. No SQLite (D7).

### Key Decisions

- **Dumb store + smart writer (D5):** three curated tools for create/search/read; surgical
  edits reuse `fs.edit` rather than a bespoke brain-edit tool — avoids a duplicate edit
  engine at the cost of one multi-root change to `fs`.
- **In-container indexing, host-only git (D6/D-Git):** indexing is pure I/O and must be
  synchronous with writes (no lag); git is a host concern to honour the container/host split
  and never block a session.
- **Reindex hook inside the `@tool` body:** the only correct seam given `fs` is dual-surfaced
  (native spec + exec sandbox).
- **Single `rg` pass, per-term counts via `submatches`:** honours "one subprocess per query"
  while still yielding per-term body weights.
- **No SQLite v1 (D7):** prior generations only used it as an access log off the hot path.

### Deviations from existing patterns

- First **host-side git** in nexus (host previously ran none). Contained entirely in
  `brain/git.py` + two call sites.
- `fs` tools go from single-root to multi-root — the one structural change to an existing
  module; kept minimal (root list + hook).

### Risks and Open Questions

- Multi-root fs could bypass brain taxonomy rules → mitigated by enforcing
  category/system-dir/reserved rules on `fs` writes under the brain root (same as `brain_write`).
- Container git isolation is a guarantee about *imports*, not file presence → enforced via
  lazy import + import-graph test.
- Deferred (out of scope for v1): automatic memory extraction into the reserved `_auto/` (D3);
  SQLite
  observability log (D7); cross-session merge/conflict handling beyond single-writer;
  richer config surface for periodic-commit interval (v1 is a simple on/off flag).

## Milestones

> Each milestone is a vertical slice unless labelled **prefactor**. Test seams named where
> behavioural. `⚠️` marks gotchas.

### 1. Shared brain models + paths + seed (prefactor)

**Approach:**
- New pkg `shared/src/archie_shared/brain/` mirroring `credentials/`. Add `models.py`
  (`Frontmatter`, `IndexEntry`, `SearchHit`, `ITEM_TYPES`, `RESERVED_PREFIX`, `SYSTEM_DIRS`),
  `paths.py`
  (`brain_root`, `resolve_brain_path`, `is_under_brain`), `seed.py`.
- `brain_root()` = `home_dir() / "brain"` (reuse `config.home_dir()`, `config.py:20`).
- Atomic writes via temp-file + rename per `credentials/store.py:61`.
- Test seam: public functions in `paths.py` / `seed.py` (unit tests).
- ⚠️ `resolve_brain_path` must reject `..` traversal AND top-level not in `ITEM_TYPES ∪
  SYSTEM_DIRS` AND `_`-prefixed dirs not on `SYSTEM_DIRS`.
- ⚠️ Seed is **file-only, no git** — `git init` belongs to host `ensure_repo()` (M6).

**Tasks:**
- Create subpackage skeleton + `__init__.py` exports.
- Implement models, path validation, and seed (`BRAIN.md` + `index.yaml` + category
  `.gitkeep`).

**Deliverable:** `resolve_brain_path` + `ensure_seed` behave per the store/layout
requirements, and `ensure_seed` performs no git operation.

**Verify:** unit tests — valid category paths and `_inbox/` resolve; traversal/`secrets/`/
`_auto/` rejected; `ensure_seed` on a tmp dir yields the expected tree with no `.git`.

---

### 2. Store + incremental index + rebuild (prefactor)

**Approach:**
- `store.py` (`read_entry`, `write_entry` with system-stamped `updated`, frontmatter
  split/parse) and `index.py` (`load_index`, `upsert_index_entry`, `remove_index_entry`,
  `rebuild_index`).
- `updated` stamped to `datetime.now(UTC).isoformat()`; any incoming `updated` ignored.
- Atomic index writes (temp-file + rename).
- Test seam: `store` / `index` public funcs.
- ⚠️ `index.yaml` is a *derived cache* — `rebuild_index` must reconstruct it purely from
  frontmatter. Incremental upsert must not full-rebuild.

**Tasks:**
- Implement store read/write with frontmatter stamping.
- Implement incremental upsert/remove + full rebuild.

**Deliverable:** writing an entry updates `index.yaml` for that entry; deleting `index.yaml`
+ rebuild reproduces an equivalent index.

**Verify:** unit tests — write two entries → index has both; mutate one → only its `updated`
changes; delete `index.yaml` + `rebuild_index` → equivalent mapping.

---

### 3. `brain_write` tool (create/replace) end-to-end

**Approach:**
- `agent/src/archie_agent/brain_tools.py`: `create_brain_tools()` → `ToolSpec` for
  `brain_write(path, name, summary, tags, content)`, following `create_skill_tool`
  (`skills.py:135`). Handler: `resolve_brain_path` → `ensure_seed` if needed →
  `store.write_entry` → `index.upsert_index_entry`.
- Register in `harness.py` near existing tool registration (~`harness.py:130`).
- Test seam: the `ToolSpec` handler (call it directly with a tmp brain root).

**Wiring:**
- State: brain repo files + `index.yaml` under `brain_root()`.
- Producers: `brain_write` handler.
- Consumers: `brain_search` / `brain_read` (M4); host git (M6).
- Call site: model tool call `brain_write(...)` in the harness loop.

**Edge cases:**
- Escaping/invalid `path` (`../`, non-category, reserved `_`-prefixed): return error, write
  nothing. `_inbox/` is valid.
- Fresh env: seed then write.
- Model supplies `updated`: ignored.

**Tasks:**
- Implement handler + schema + model guidance (in `description`).
- Register in harness.

**Deliverable:** `brain_write` creates/replaces an entry, stamps `updated`, updates the index.

**Verify:** handler test on tmp root — new entry file + index entry present with system
`updated`; invalid path rejected with no file created.

---

### 4. `brain_search` (ranked, single rg pass) + `brain_read`

**Approach:**
- `search.py`: `search()` scans the in-memory index for name/summary/tags hits + one
  `rg --json -e t1 -e t2 …` over category dirs (reuse the `exec/tools/_subprocess.run_exec`
  pattern or a shared runner) for body hits; merge + score (formula in Design); return
  `list[SearchHit]`. `brain_read` = `store.read_entry`.
- Two more `ToolSpec`s in `brain_tools.py`, registered with `brain_write`.
- Test seam: `search.search()` (pure) + the two handlers.
- ⚠️ Exactly one rg subprocess per query. ⚠️ Recency is a tie-breaker bonus only — never
  surfaces zero-lexical entries.

**Edge cases:**
- No matches → empty-result message.
- `item_type` filter → restrict index scan + rg scope to that category dir.
- Body-only match → excerpt included.

**Tasks:**
- Implement scoring + single-rg body search (parse `submatches` for per-term counts) + merge.
- Implement `brain_read`; register both tools with model guidance in each `description`.

**Deliverable:** ranked search per the search requirements and exact-path read.

**Verify:** unit test — name-match outranks summary-match; equal-lexical ties break by
`updated` desc; body-only term yields excerpt via a single rg call (asserted via counting
runner); `brain_read` returns full entry / clear error on missing path.

---

### 5. Multi-root fs + reindex hook

**Approach:**
- Refactor `fs._resolve_path` (`fs.py:46`) to validate against a root list
  `[WORKSPACE, brain_root()]` using the existing resolve-then-`relative_to` guard
  (`fs.py:75-81`) per root; keep the closed allow-list (reject anything under neither). When
  resolved under the brain root, also enforce category/system-dir/reserved rules by reusing
  `resolve_brain_path`'s validator.
- After a successful `write`/`edit` (`fs.py:379`/`:405`), if `is_under_brain(resolved)`, call
  the shared hook → re-stamp `updated` + `upsert_index_entry`. Single seam.
- Test seam: `_resolve_path` + `write`/`edit` behaviour with a tmp brain root.
- ⚠️ `write`/`edit` are dual-surfaced (native `ToolSpec` **and** exec sandbox) — put the
  reindex hook **inside the `@tool` function body** so it fires on both surfaces.
- ⚠️ Non-workspace/non-brain paths must still be rejected; under the brain root, top-levels
  that are neither a category nor an allowed system dir (`_inbox`) must also be rejected.
  Home mount is confirmed (Design) — not open.

**Wiring:**
- State: `index.yaml` under `brain_root()` (shared with M3).
- Producers: `fs.write`/`fs.edit` hook (new), alongside `brain_write` (M3).
- Consumers: `brain_search`/`brain_read`.
- Call site: existing `write(...)`/`edit(...)` tool calls, now brain-aware.

**Edge cases:**
- Edit of a brain entry → `updated` re-stamped + index reflects it same-session.
- Edit of a workspace file → no brain hook fires.
- Path outside both roots → `PathValidationError`.
- Write to `<brain>/_auto/x.md` or `<brain>/misc/x.md` → rejected. `<brain>/_inbox/x.md` → ok.

**Tasks:**
- Root-list resolution + brain category enforcement.
- Brain post-write hook inside the `@tool` bodies; ensure the fs module never imports
  `git.py`.

**Deliverable:** `fs.edit`/`fs.write` reach the brain (category-valid paths only), reindex on
brain writes, and still reject out-of-root and non-category paths.

**Verify:** unit tests for the edit re-stamp and the rejection cases; edit a brain entry via
both the native and exec surface → `brain_search` shows the new `updated`.

---

### 6. Host-side git: session-end commit (+ optional periodic)

**Approach:**
- `brain/git.py`: `ensure_repo()` (host `git init` + initial commit if absent),
  `commit_all(message)` (`add -A`; commit; "nothing to commit" = success). Shell out via
  `asyncio.to_thread`. Imported **lazily at the call site only**.
- Call `commit_all` in `stop_session` (`lifecycle.py:156`) **after** `stop_container`, wrapped
  in try/except → log-and-continue.
- Optional periodic task in `lifespan` (`app.py:55`) mirroring `MetricsWriter.run`
  (`metrics.py:111`) + done-callback logging (`app.py:71`); disabled by default.
- Test seam: `commit_all` on a tmp repo; `stop_session` git call is best-effort.
- ⚠️ Host-only — `git.py` must never be imported on the container tool path; enforce via lazy
  import + import-graph test. ⚠️ "nothing to commit" must not error. ⚠️ First `git init`
  happens here (host), NOT in the in-container seed.

**Edge cases:**
- No brain changes on session end → no commit, no error.
- git binary missing / repo corrupt → logged, session still stops.

**Tasks:**
- Implement `git.py` (incl. host `ensure_repo`/`git init`).
- Wire `stop_session` (commit after `stop_container`); add the default-off periodic task +
  config flag; add the import-graph test proving container tools don't import `git.py`.

**Deliverable:** session-end commits brain changes best-effort; clean sessions no-op.

**Verify:** integration — write via a brain tool then `stop_session` → commit exists; stop
with no changes → no commit and no raised error.

---

### 7. Discoverability: BRAIN.md conventions + tool descriptions

**Approach:**
- Seeded `BRAIN.md` documents the taxonomy (people/projects/knowledge), frontmatter shape,
  and *soft* naming conventions.
- Ensure the three brain tools carry model-facing guidance in their `description` (discrete
  `ToolSpec`s have no `guidelines=` tuple — that's the native `@tool` path). Optionally add
  brief brain guidance to the relevant memory/skill doc.
- Test seam: tool registry advertises brain tools with non-empty descriptions.

**Tasks:**
- Author `BRAIN.md` seed content; finalise tool `description` text.

**Deliverable:** the agent is told how/when to use the brain and the taxonomy to follow.

**Verify:** inspect the advertised tool set — includes the three brain tools with
descriptions; `BRAIN.md` present after seed.

---

### Not yet specified (deferred)

- Automatic memory extraction into the reserved `_auto/` (D3) — future spec.
- SQLite observability log (D7) — only if an observability need materialises.
- Cross-session merge/conflict handling beyond the single-writer assumption.
- Config surface for periodic-commit interval beyond a simple on/off flag.

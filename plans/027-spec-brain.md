# 027 — SPEC: Brain (curated memory / knowledge store)

## Objective

Add a durable, filesystem-backed brain to archie-nexus. The agent can search curated
knowledge with one dedicated native tool and can inspect or change exact entries through the
regular filesystem tools. Users control the brain's directory layout; the only convention
for an indexed entry is valid YAML frontmatter with the required search and recency fields.

The brain is curated-only in v1. Automatic extraction, embeddings, and other retrieval
systems are outside this spec. Filesystem visibility and Docker mount configuration remain
the agent's access boundary.

## Context

Nexus has no brain implementation today. Earlier designs combined a derived YAML index with
fixed categories and additional access-tracking concepts. This spec intentionally removes
those constraints: an entry may live at any depth and the system does not assign semantic
meaning to directories or filenames.

Nexus exposes native tools as well as the exec surface. The brain therefore has one dedicated
native capability, `brain_search`, because ranked retrieval is domain-specific. Exact reads
and mutations use the existing native `read`, `write`, and `edit` tools. Those filesystem
tools are also available through the exec surface and must retain one implementation path.

The system prompt has two guidance layers:

1. a repository-owned `persona/prompts/brain.md` fragment that explains the brain's purpose,
   frontmatter, and normal workflow; and
2. an optional user-owned `BRAIN.md` at the brain root, injected after the persona fragment
   so users can add brain-specific conventions.

The brain root is configurable with `ARCHIE_BRAIN_DIR`. When unset, it defaults to
`$ARCHIE_HOME_DIR/brain`, using the existing `ARCHIE_HOME_DIR` default when that variable is
also unset.

## Requirements

### Brain root and user-defined layout

- MUST resolve the brain root from `ARCHIE_BRAIN_DIR` when set, otherwise from
  `home_dir() / "brain"`
  - AC: with `ARCHIE_BRAIN_DIR=/tmp/custom-brain`, all brain operations use that directory.
  - AC: with no `ARCHIE_BRAIN_DIR`, the resolved root is `$ARCHIE_HOME_DIR/brain`.
  - AC: with neither environment variable, the default root is `~/.nexus/brain`.

- MUST expand the configured path and use the resolved path consistently for indexing,
  searching, prompt guidance, and filesystem write/edit hooks
  - AC: equivalent `~` and absolute forms identify the same brain root.
  - AC: a relative `ARCHIE_BRAIN_DIR` is resolved relative to the resolved `ARCHIE_HOME_DIR`.
  - AC: changing `ARCHIE_BRAIN_DIR` between isolated test runs does not read the previous root.

- MUST propagate an explicitly configured `ARCHIE_BRAIN_DIR` through the standard host-to-container
  session environment without rewriting its value
  - AC: a session started with the variable set exposes the same variable inside the agent
    container.
  - AC: the configured container path must already be visible through the session mounts; the
    lifecycle does not silently fall back to the default when it is not visible.

- MUST create the configured brain root on first brain use without creating categories,
  reserved directories, filenames, or a documentation scaffold
  - AC: a fresh root contains no system-created category directories or guidance file.
  - AC: writing a valid entry at any nested path succeeds when its parent directories can be
    created.

- MUST accept arbitrary nested directory layouts and filenames below the brain root
  - AC: entries at unrelated paths such as `people/alice.md`, `work/acme/context.md`, and
    `notes.md` are treated identically by indexing and search.
  - AC: no path is rejected because of its depth, directory name, filename, or underscore
    prefix.

### Entry format and source of truth

- MUST recognise an indexed entry as a readable UTF-8 text file with YAML frontmatter at the
  start of the file and a body after the closing frontmatter delimiter
  - AC: valid frontmatter and body produce one index record.
  - AC: a binary file, missing delimiter, malformed YAML, or non-mapping frontmatter produces
    no index record and does not prevent other entries from being indexed.

- MUST require `name`, `summary`, `tags`, and `updated` in indexed frontmatter, with `tags`
  represented as a list
  - AC: each indexed record contains those four fields with the expected types.
  - AC: an entry missing any required field is excluded from search and indexing.
  - AC: additional frontmatter fields are preserved and do not invalidate the entry.

- MUST treat frontmatter as the source of truth for indexed metadata and recency
  - AC: deleting the derived index and rebuilding it reproduces equivalent records from entry
    files alone.
  - AC: newest/oldest ordering uses `updated`, not filesystem mtime or access history.

- MUST set `updated` in the regular brain-aware write/edit path and overwrite any model-supplied
  value
  - AC: a write containing an old or future `updated` value stores the current system timestamp.
  - AC: editing an existing valid entry changes its `updated` value before the tool returns.
  - AC: the index record contains the same timestamp written into the entry frontmatter.

- MUST store and parse `updated` as a UTC ISO-8601 string with `+00:00` offset and
  microsecond precision
  - AC: system-generated timestamps use `datetime.now(UTC).isoformat()`.
  - AC: frontmatter timestamps with another valid offset normalize to UTC during indexing.
  - AC: an unparseable or non-string `updated` value excludes the file from indexing.

- MUST maintain a rebuildable `index.yaml` at the brain root as derived implementation data
  - AC: each valid entry contributes its brain-relative path, `name`, `summary`, `tags`, and
    `updated`.
  - AC: `index.yaml` itself is never indexed as an entry.
  - AC: the index is written atomically so readers see either the old or new complete file.

- MUST treat a missing, malformed, or structurally invalid `index.yaml` as disposable and
  rebuild it from entry files under the per-brain lock
  - AC: invalid YAML, non-mapping root data, duplicate paths, invalid record fields, or
    out-of-root paths trigger a rebuild rather than partial search results.
  - AC: a manually edited but structurally valid index is used until the normal freshness probe
    detects a newer entry or deletion; frontmatter remains authoritative on rebuild.

### Regular filesystem operations

- MUST remove Archie-specific path containment restrictions from `read`, `write`, and `edit`
  - AC: an absolute path anywhere in the container's visible filesystem works when ordinary
    filesystem permissions allow it.
  - AC: leaving the workspace is not itself a validation error.
  - AC: a path outside the container's mounts or on a read-only mount fails through the normal
    filesystem error path.

- MUST preserve the existing relative-path convention for filesystem tools
  - AC: a relative path continues to resolve from the workspace working directory.
  - AC: an absolute path is resolved independently of the workspace.

- MUST apply the same unrestricted-visible-path behavior to `grep` and `glob`
  - AC: their absolute path arguments are not restricted to the workspace.
  - AC: results under the workspace retain the existing relative display form, while results
    outside the workspace are displayed as normalized absolute paths.
  - AC: outside-workspace results are not silently dropped during path formatting.

- MUST use the regular filesystem implementation for exact brain reads and mutations
  - AC: the advertised dedicated brain tool set contains `brain_search` only.
  - AC: `read`, `write`, and `edit` work for brain paths without a second brain-specific read
    or write API.

- MUST run a shared brain hook before returning from a successful `write` or `edit` whose
  resolved path is under the configured brain root
  - AC: a valid brain entry written or edited through either native or exec filesystem access
    receives a system timestamp and an index update before the tool returns.
  - AC: a workspace or unrelated visible file does not trigger brain indexing.

- MUST serialize the complete brain mutation, including the file read/validation, entry
  replacement, and index update, under the per-brain lock
  - AC: concurrent writes to the same brain cannot produce an index record for content that is
    not the final content of the corresponding entry.
  - AC: non-brain filesystem operations do not wait on the brain lock.

- MUST preserve existing valid-entry invariants and roll back the complete mutation when its
  index update fails
  - AC: an edit that would remove required frontmatter is rejected and leaves the original file
    and index unchanged.
  - AC: an index serialization or replacement failure restores the prior entry and prior index
    before returning an error; a rollback failure returns a compound error naming both failures.
  - AC: a new ordinary file without valid frontmatter remains writable and is ignored by brain
    indexing.
  - AC: a new valid entry is timestamped and indexed.

### Index refresh policy

- MUST refresh a missing index before serving search results
  - AC: a search with no `index.yaml` rebuilds the index before ranking results.
  - AC: concurrent searches do not each perform an independent rebuild after one has completed.

- MUST use a fixed one-day freshness interval for cheap change detection
  - AC: an index whose mtime is less than 24 hours old is used without recursively parsing
    frontmatter, unless an indexed path is missing.
  - AC: the freshness interval is represented by one named implementation constant, not
    duplicated across callers.

- MUST, when the index is at least one day old, compare filesystem modification times rather
  than parsing frontmatter to decide whether a rebuild is needed
  - AC: the probe checks for any candidate file newer than `index.yaml` using file mtime.
  - AC: the probe checks whether any indexed path no longer exists, so deletions are detected.
  - AC: if no candidate is newer and no indexed path is missing, the old index is reused.
  - Why: filesystem metadata is cheaper than parsing every entry and `updated` remains reserved
    for ranking rather than freshness detection.

- MUST serialize rebuild and incremental index updates with a per-brain inter-process lock
  - AC: concurrent callers use the same lock path for the same configured brain root.
  - AC: a caller rechecks index state after acquiring the lock and skips a rebuild already
    completed by another caller.
  - AC: waiting callers eventually use the atomically replaced index or return a clear error
    if the lock cannot be acquired.

- MUST make the freshness probe and rebuild safe for arbitrary user layouts
  - AC: the probe excludes `index.yaml`, the optional root guidance file, lock artifacts, and
    VCS metadata from candidate entry changes.
  - AC: nested directories are traversed without category assumptions.

### Search

- MUST provide one native `brain_search` tool accepting `query: string` and optional
  `limit: integer`
  - AC: the tool schema contains no category or item-type filter.
  - AC: the tool is present in the root and child-agent tool configurations.
  - AC: the tool description tells the model to use regular `read`, `write`, and `edit` for
    exact entry access and changes.

- MUST refresh the index according to the freshness policy before searching
  - AC: a search after a regular filesystem write sees the entry immediately through the
    incremental hook.
  - AC: a search after an out-of-band file change rebuilds when the mtime probe detects it.

- MUST combine indexed frontmatter matches with body matches from exactly one literal ripgrep
  invocation per query
  - AC: a query with multiple terms produces one body-search ripgrep process, not one per term.
  - AC: query punctuation is searched literally and cannot change the ripgrep expression.

- MUST tokenize a query as distinct non-empty whitespace-separated terms, match terms with
  case-insensitive OR semantics, and count each matching term at most once per field
  - AC: a query containing `alpha beta` matches an entry containing either term.
  - AC: repeated occurrences of one term in a field do not multiply that field's score.
  - AC: matching is case-insensitive for both indexed fields and body text.
  - AC: phrase matching is not required.

- MUST rank matches using name ×3, tags ×2, summary ×1, and body ×1, plus a bounded additive
  recency term derived from `updated`
  - AC: the lexical score equals the sum of the weights for distinct query terms present in
    each field.
  - AC: recency is `2.0 * exp(-age_days / 30.0)` where age is clamped to zero for future
    timestamps.
  - AC: a name-only match outranks an otherwise equivalent summary-only match.
  - AC: a newer entry wins an equal lexical-score tie.
  - AC: recency never surfaces an entry with zero lexical matches.

- MUST apply the result-limit contract `limit` default 10, maximum 50, zero returns no hits,
  and negative values are invalid
  - AC: omitting `limit` returns at most 10 hits.
  - AC: values above 50 are clamped to 50.
  - AC: `limit=0` returns an empty list without invoking body search.
  - AC: a negative limit returns a clear validation error.

- MUST return each hit with its brain-relative `path`, `name`, `summary`, `tags`, `updated`,
  and a short body excerpt when the body matched
  - AC: a body-only match includes a non-empty excerpt.
  - AC: no-match queries return an empty result without an exception.
  - AC: an empty query returns a clear validation error without invoking ripgrep.

### Prompt guidance

- MUST add `persona/prompts/brain.md` containing stable guidance about the brain's purpose,
  arbitrary layout, required frontmatter, search workflow, and regular filesystem operations
  - AC: the file is loaded through the existing persona prompt-fragment convention.
  - AC: the static system prompt includes the persona brain guidance in every root and child
    prompt that includes standard tool guidance.

- MUST load an optional `BRAIN.md` from the root of the configured brain after the persona
  `brain.md` fragment
  - AC: when present and readable, its content appears after the persona guidance in the
    assembled prompt.
  - AC: when absent, the prompt still builds successfully without an empty placeholder.
  - AC: unreadable or invalid UTF-8 `BRAIN.md` is omitted with a warning rather than failing
    the turn.
  - AC: the root `BRAIN.md` is guidance, not an indexed entry, even if it contains frontmatter.

- MUST rebuild prompt guidance at the existing prompt-build boundary so a changed root
  `BRAIN.md` is reflected on the next prompt snapshot
  - AC: changing `BRAIN.md` between turns changes the next assembled prompt.
  - AC: the persona fragment remains stable unless the persona source changes.

### Scope and exclusions

- MUST keep v1 curated-only
  - AC: no automatic extraction runs from conversation content or session logs.
  - AC: no embeddings or vector retrieval dependency is added.

- MUST leave version-control operations and user-specific persistence policy outside this spec
  - AC: no lifecycle hook attempts to create, stage, or record changes in a repository.
  - AC: nested repositories and user-managed version-control layouts under the brain are left
    untouched by the brain implementation.

## Technical Design

### Overview

Build the brain as a shared filesystem/index/search service with one native adapter. The
regular filesystem tools remain the only exact file access API. A post-write hook detects
paths under the configured brain root and applies frontmatter timestamping plus an incremental
index update. Search uses a cheap mtime freshness probe and a lock-guarded rebuild only when
necessary, then performs one literal body-search ripgrep pass.

Prompt guidance is assembled from the repository-owned persona fragment and an optional
root-level user guidance file. The brain implementation never performs repository or
version-control operations.

### Technical Stack

- Reuse the existing Python standard library (`pathlib`, `os`, `datetime`, and Linux
  `fcntl.flock`) for path resolution, timestamps, mtime probes, and the inter-process lock; no
  new dependency is needed.
- Reuse the existing YAML library for frontmatter and `index.yaml` parsing/serialization; no
  second YAML implementation is introduced.
- Reuse the installed `ripgrep` executable for body matching; pass query terms as fixed-string
  expressions rather than regexes.
- Reuse the existing native `ToolSpec`/`ToolRegistry` pattern for `brain_search`.
- Reuse the existing structured prompt builder and persona fragment loader for `brain.md`.
- Reuse the repository's existing pytest and async test conventions; no new test framework is
  needed.

### Architecture

**Shared brain service**

Owns root resolution, entry parsing, timestamping, index records, freshness probing, locking,
rebuild, incremental updates, and search. It performs filesystem I/O only and does not own
prompt assembly or filesystem tool dispatch.

**Filesystem tool layer**

Owns arbitrary visible-path resolution and the existing read/write/edit behavior. After a
successful write/edit, it calls the shared brain hook with the resolved path and resulting
content. The hook decides whether the path is a valid brain entry and updates metadata/index
without duplicating file-tool behavior.

**Native search adapter**

Owns the model-facing `brain_search` schema and description. It delegates all search behavior
to the shared service and is registered in both root and child-agent registries.

**Prompt builder**

Loads the static persona fragment and the optional root guidance file in order. It adds the
combined brain guidance to the standard static prompt section.

### Components

**Brain root/config resolver**

- Interface: shared `brain_root()` resolver.
- Input: `ARCHIE_BRAIN_DIR`, `ARCHIE_HOME_DIR`.
- State: no mutable state; returns a resolved `Path`.
- Default: `home_dir() / "brain"` when the override is absent.

**Entry parser/store**

- Interface: parse, validate, stamp, read, and atomically write entry content.
- State: frontmatter and body in the entry file; arbitrary extra frontmatter is preserved.
- Rule: existing valid entries cannot be replaced by invalid content through regular edit.

**Index service**

- Interface: load, rebuild, incremental upsert/remove, freshness check.
- State: root `index.yaml`; lock file adjacent to the brain root.
- Rule: frontmatter is authoritative; the index is disposable and rebuildable.

**Search service**

- Interface: `search(query, limit)` returning ranked hits.
- State: refreshed in-memory index plus per-query body-match data.
- Rule: one literal ripgrep invocation per non-empty query.

**Filesystem integration**

- Interface: existing `read`, `write`, `edit` functions and their native/exec surfaces.
- State: none beyond files; brain hook runs only after successful mutations.
- Rule: all visible paths are permitted subject to normal filesystem behavior.

**Prompt guidance**

- Interface: existing structured prompt builder.
- State: repository persona fragment plus optional root `BRAIN.md` content for each prompt
  snapshot.
- Rule: persona guidance appears before user-owned brain guidance.

### Data Model

**Frontmatter** — stored in each indexed entry

- `name: string` — required.
- `summary: string` — required.
- `tags: list[string]` — required; empty list allowed.
- `updated: string` — required and system-managed; stored as a UTC ISO-8601 string with
  `+00:00` offset and microsecond precision.
- Additional fields — optional; preserved verbatim where YAML representation permits.

**Index record** — stored in `index.yaml`

- `path: string` — required; brain-relative POSIX path.
- `name: string` — required; copied from frontmatter.
- `summary: string` — required; copied from frontmatter.
- `tags: list[string]` — required; copied from frontmatter.
- `updated: string` — required; copied from frontmatter and parsed as the normalized UTC
  timestamp for ranking.

**Search hit** — transient tool result

- `path`, `name`, `summary`, `tags`, `updated` — copied from the index record.
- `excerpt: string | null` — present when body matching contributed to the hit.
- `score: number` — optional diagnostic field; not required for model behavior.

**Refresh state** — transient decision data

- `index_mtime: float` — required when the index exists.
- `index_age: duration` — derived from current time and index mtime.
- `has_newer_candidate: bool` — result of the mtime probe.
- `has_missing_index_path: bool` — result of checking indexed paths.

### Data Flow

**Regular brain write/edit**

1. The model calls regular `write` or `edit` with a visible path.
2. The filesystem tool resolves the path without workspace containment validation.
3. If the path is under the configured brain root, acquire the per-brain lock before reading
   the current file for mutation.
4. Parse the candidate content, preserve the original bytes, and decide whether the result is a
   valid entry. Stamp `updated` for a valid entry.
5. Atomically replace the entry and upsert a candidate index while holding the lock. If either
   replacement fails, restore the original entry and index before returning an error.
6. For an edit that would invalidate an existing valid entry, reject the mutation and preserve
   the original file and index.
7. Release the lock; paths outside the brain complete through the ordinary filesystem path.

**Search freshness check**

1. Ensure the brain root exists.
2. If `index.yaml` is absent, acquire the per-brain lock, recheck, and rebuild.
3. If `index.yaml` is younger than 24 hours, check indexed paths for deletion and otherwise
   use the index without parsing entry frontmatter.
4. If it is at least 24 hours old, run a cheap mtime probe over candidate files and check
   indexed paths for deletion.
5. If neither newer candidates nor missing indexed paths exist, use the old index.
6. If refresh is needed, acquire the lock, repeat steps 2–5, and rebuild only if still needed.
7. Release the lock before the body-search process.

**Search query**

1. Validate that `query` is non-empty and split it into distinct non-empty whitespace-separated
   terms, normalized for case-insensitive matching.
2. Scan refreshed index fields; each distinct term present in a field contributes that field's
   weight once.
3. Run exactly one fixed-string, case-insensitive ripgrep invocation over the brain root for
   body matches, excluding derived/prompt/VCS metadata and filtering results to valid indexed
   paths.
4. Count each distinct body term once per entry, merge field and body scores, and apply the
   bounded `updated` recency term.
5. Sort by score descending, then `updated` descending, and return up to the limit contract.

**Prompt assembly**

1. Load the existing static identity, environment, and tools sections.
2. Load `persona/prompts/brain.md` using the existing persona prompt convention.
3. Resolve the configured brain root.
4. If root `BRAIN.md` exists, read and normalize it; otherwise omit it.
5. Append persona brain guidance followed by root guidance to the static prompt.
6. Preserve the existing dynamic section behavior for skills and other dynamic content.

### Error Handling & Edge Cases

- Missing brain root: create it when the operation requires it; return a normal permission error
  if creation is not allowed.
- Invalid `ARCHIE_BRAIN_DIR`: fail brain operations with a clear configuration/path error;
  do not silently fall back to the default.
- Missing index: rebuild under lock.
- Stale index with newer candidate files: acquire the lock, recheck, rebuild once.
- Stale index with no newer files: reuse it; do not parse every entry.
- Deleted indexed file: rebuild under lock even when the index is younger than 24 hours.
- Lock contention: wait for the existing lock; after acquisition recheck before rebuilding.
- Lock failure: return a clear search/index error and do not replace the index unsafely.
- Invalid new entry: write as an ordinary unindexed file unless it is replacing an existing
  valid entry; in the latter case reject and restore the original.
- Invalid existing-entry edit: preserve the original file and index, and return a validation
  error to the tool caller.
- Malformed/unsafe index: discard it, rebuild from entry files under the lock, and never use
  unvalidated index paths to read outside the brain.
- Brain mutation/index failure: restore both the original entry and prior index before returning
  an error; if rollback fails, return a compound error naming the mutation and rollback failures.
- Missing/unreadable root `BRAIN.md`: omit only that optional guidance and continue prompt
  construction with persona guidance.
- Empty search query: return a validation error without spawning ripgrep.
- Ripgrep failure: return a clear search error; do not return partial ranking results.
- Unrestricted filesystem path: defer access decisions to visibility, mount mode, and normal
  OS permissions.

### Code Structure

The plan is intentionally agnostic about repository package directories. Locate implementation
seams by the existing symbols and responsibilities:

- Add the shared brain service alongside other shared filesystem/data services.
- Add the native search adapter alongside existing custom `ToolSpec` factories.
- Extend the existing filesystem tool module and its path/read/write/edit functions.
- Add the required repository-owned prompt file at `persona/prompts/brain.md`.
- Extend the existing structured prompt builder to load that fragment and root `BRAIN.md`.
- Keep tests at the repository's established test boundary and test public behavior rather than
  source-directory layout.

The explicit prompt path is a content contract. Other implementation files may follow the
current package layout or a future equivalent layout.

### Patterns and Conventions

- Configuration: follow `home_dir()` environment-variable resolution and use
  `ARCHIE_BRAIN_DIR` as the higher-priority override.
- File writes: use temporary-file plus same-directory atomic replacement for entry/index files.
- Locking: use an adjacent lock file and `fcntl.flock`; for brain mutations acquire before
  reading current content, validate and mutate while held, replace entry and index atomically,
  then release. For freshness acquire only after the cheap probe and recheck before rebuilding.
  Never hold the lock while running ripgrep.
- Timestamps: generate UTC strings with `datetime.now(UTC).isoformat()` and normalize valid
  offset-bearing input to UTC before indexing.
- Errors: use the existing typed filesystem-tool errors at the tool boundary and clear shared
  service exceptions internally; do not convert validation failures into successful results.
- Search: use fixed-string matching and escape/quote terms before invoking ripgrep.
- Prompt content: preserve structured static/dynamic prompt sections and append root guidance
  after persona guidance.
- Testing: patch the brain-root resolver and command runner at public seams; assert behavior,
  not internal module paths.

### Infrastructure and Deployment

- New environment variable: `ARCHIE_BRAIN_DIR`, optional. It overrides the default
  `$ARCHIE_HOME_DIR/brain`; relative values resolve relative to the resolved home directory,
  and absolute values are used as supplied after expansion.
- Existing environment variable: `ARCHIE_HOME_DIR`, unchanged. It supplies the default parent
  for the brain root.
- Session environment: the standard host lifecycle forwards an explicitly set
  `ARCHIE_BRAIN_DIR` into the container unchanged. A custom path must be visible through the
  caller's existing mounts; this plan does not infer host-to-container path translations or
  create a new mount.
- Mounts: no new mount is required for the default brain root because the existing home mount
  already exposes it. A custom path outside existing mounts must be made visible by the caller's
  container configuration.
- System packages: no new package is required; the container already provides Python, YAML
  dependencies, and ripgrep.
- Persona content: add the tracked `persona/prompts/brain.md` file.
- Version-control or repository lifecycle changes: none in this spec.

### Non-Functional Concerns

- Access boundary: the agent may operate on any visible path; Docker mounts and normal OS
  permissions are the controlling boundary. No Archie-specific path sandbox is added.
- Performance: a search normally reads the existing index; only an index older than 24 hours,
  a missing indexed path, or a missing index triggers the freshness/rebuild path. The freshness
  probe uses file mtimes, not frontmatter parsing.
- Concurrency: the adjacent per-brain lock prevents duplicate rebuilds and lost index updates
  among cooperating brain operations. Atomic replacement protects readers from partial files.
- Corpus assumption: the brain is expected to contain hundreds to low thousands of curated
  entries; a full rebuild under the lock is acceptable at that scale.
- Reliability: a disposable index can always be reconstructed from frontmatter; a failed
  rebuild leaves the previous index intact and returns an explicit error.

### Key Decisions

- `ARCHIE_BRAIN_DIR` override over a default derived from `ARCHIE_HOME_DIR`: permits separate
  brains without changing the established home configuration or mount contract.
- One dedicated `brain_search` tool over dedicated read/write tools: search has domain-specific
  ranking, while regular filesystem tools already provide exact access and mutation.
- Arbitrary user-defined layout over taxonomy: users can organise knowledge according to their
  own needs, and frontmatter supplies the stable machine-readable contract.
- One-day mtime freshness threshold plus newer-file probe over rebuilding every search: keeps
  normal searches cheap while detecting ordinary out-of-band changes without parsing every file.
- Adjacent inter-process lock with double-check over unconditional locking or last-writer-wins:
  avoids duplicate rebuild work and prevents cooperating writers from overwriting index updates.
- Persona guidance followed by optional root guidance: repository defaults provide the workflow,
  while each brain can add its own conventions without changing the agent package.
- No automatic version-control lifecycle: brains may contain unrelated or nested repositories,
  so the brain implementation must not mutate repository state.

### Risks and Open Questions

- **Mtime-preserving copies:** a copied/new file whose mtime is not newer than `index.yaml` is
  not detected until a later freshness window or an explicit rebuild. This is acceptable for v1;
  regular filesystem writes update the index immediately.
- **Out-of-band shell writes:** shell changes do not receive a system `updated` timestamp. The
  prompt directs model-authored brain changes through `write`/`edit`; search can rebuild state
  but cannot infer the intended semantic timestamp.
- **Lock file lifecycle:** a stale lock after process termination must be releasable by the OS
  file-lock mechanism; the lock file itself may remain and is not brain content.
- **Prompt size:** a large root `BRAIN.md` increases every prompt. The user controls this file;
  future prompt-size policy can be added independently.

## Milestones

### 1. Configurable brain root and frontmatter/index service (prefactor)

**Approach:**
- Implement the shared root resolver with `ARCHIE_BRAIN_DIR` precedence over the existing
  `home_dir()` default.
- Use the existing YAML dependency and frontmatter parsing conventions; do not add a new
  serialization library.
- Treat every valid UTF-8 frontmatter-bearing text file at any depth as an entry, excluding
  root `BRAIN.md`, `index.yaml`, lock artifacts, and VCS metadata.
- Use public service functions as the test seam: root resolution, parse/validate, write/stamp,
  rebuild, and index serialization.

**Wiring:**
- State: configured brain root and derived `index.yaml`.
- Producers: frontmatter-bearing files written by regular filesystem operations and existing
  user files already present under the root.
- Consumers: index rebuild/upsert and later search.
- Call site: shared services and filesystem hooks call `brain_root()` rather than embedding a
  path or reading the environment independently.

**Edge Cases:**
- Override set: use the expanded override.
- Override absent: use `home_dir() / "brain"`.
- Invalid YAML or missing fields: skip the file from the index.
- Arbitrary nested layout: accept it without category validation.
- Missing root: create it on first brain operation.

**Tasks:**
- Add the configurable root resolver and frontmatter data structures.
- Implement entry parsing/validation with preservation of extra fields and UTC timestamp
  normalization.
- Implement atomic index rebuild and incremental upsert/remove operations, including
  structural index validation and discard/rebuild behavior.
- Forward an explicitly configured `ARCHIE_BRAIN_DIR` through the standard session lifecycle.
- Add tests for environment precedence, relative override resolution, arbitrary layouts,
  invalid entries, corrupt indexes, and rebuild.

**Deliverable:** A configured brain root can be indexed and rebuilt solely from valid
frontmatter files at arbitrary paths.

**Verify:** Run focused shared-service tests and assert equivalent indexes after deleting and
rebuilding `index.yaml`.

### 2. Freshness probe and concurrent index coordination (prefactor)

**Approach:**
- Use a named 24-hour index freshness constant.
- Use one `find` subprocess with `-newer <index>` and `-print -quit` for the stale-index
  candidate probe; pass paths as argv without a shell and exclude infrastructure paths in the
  expression. This avoids parsing frontmatter during the cheap check.
- Use an adjacent lock file with `fcntl.flock`. Recheck freshness after acquiring the lock so
  only the first caller rebuilds.
- Keep the lock around rebuild/upsert and atomic index replacement, but release it before body
  search.
- Test seam: public `ensure_fresh_index()` with injectable clock, file probe, and lock/runner
  boundaries.

**Wiring:**
- State: index mtime, freshness decision, lock file, and in-memory index records.
- Producers: search freshness checks and regular brain writes/edits.
- Consumers: search field scan and filesystem-tool results.
- Call site: `brain_search` calls `ensure_fresh_index()` before ranking; write/edit hooks call
  the locked incremental update path.

**Edge Cases:**
- Missing index: one caller rebuilds; waiters reuse it after double-checking.
- Fresh index: no recursive frontmatter parse unless an indexed path is missing.
- Stale index with no newer files: reuse the index.
- Stale index with newer file or deletion: rebuild once under lock.
- Lock contention: wait, recheck, then reuse or rebuild.
- Lock failure: return an explicit error and preserve the previous index.

**Tasks:**
- Implement the `find`-based newer-file probe and indexed-path deletion checks.
- Implement adjacent inter-process lock acquisition and double-check logic.
- Wire freshness checks into search and locking into incremental updates.
- Add threshold-boundary tests and a subprocess-based two-process test with a shared temporary
  brain to prove only one stale-index rebuild occurs.

**Deliverable:** Concurrent processes coordinate through one lock and avoid unnecessary index
rebuilds.

**Verify:** Spawn two separate Python processes against the same stale temporary brain, release
both at the same barrier, and assert one rebuild plus two successful search/index results.

### 3. Unrestricted filesystem tools with brain-aware mutation hooks

**Approach:**
- Remove workspace containment validation from the existing path resolver while preserving
  relative workspace resolution and ordinary filesystem error handling.
- Apply it to read/write/edit and grep/glob. For grep/glob, retain workspace-relative display
  paths for workspace results and emit normalized absolute paths for results outside it.
- Keep one filesystem implementation for native and exec surfaces.
- For brain paths, acquire the per-brain lock before reading the current content for mutation;
  validate the candidate, stamp `updated`, atomically replace the entry, and update the index
  as one rollback-safe operation. Reject an edit that would invalidate an existing entry.
- Test seam: public/native filesystem handlers with temporary visible roots and a patched brain
  root, not private path-helper implementation details.

**Wiring:**
- State: resolved file content and shared brain index.
- Producers: regular `write` and `edit` handlers on native and exec surfaces.
- Consumers: `read`, `brain_search`, and later writes/edits.
- Call site: successful brain mutation calls the shared hook before returning its confirmation.

**Edge Cases:**
- Absolute visible path: operate normally.
- Absolute inaccessible path: return ordinary filesystem error.
- Existing valid brain entry made invalid: reject and preserve original.
- New non-entry file: allow and exclude from index.
- Workspace file: do not invoke brain hook.

**Tasks:**
- Refactor path resolution and update path descriptions/documentation.
- Add post-write brain detection, timestamping, and index update.
- Add rollback-safe handling for invalid existing-entry edits.
- Test native and exec filesystem access outside the workspace and brain index updates.

**Deliverable:** Regular filesystem tools can operate on any visible path and maintain valid
brain entries without a separate brain write/read API.

**Verify:** Run focused filesystem tests that write/edit an arbitrary absolute path and then
search a nested brain entry updated through both filesystem surfaces.

### 4. Native ranked brain search

**Approach:**
- Implement search over the refreshed index plus one fixed-string ripgrep body pass.
- Filter body matches to valid paths from the current index so arbitrary non-entry files do
  not become search results.
- Use distinct whitespace terms with case-insensitive OR matching; count each term once per
  field and use the explicit limit contract (default 10, maximum 50, zero allowed).
- Use field weights name ×3, tags ×2, summary ×1, body ×1, plus bounded recency from
  frontmatter `updated`; use file mtime only for freshness detection.
- Expose the service through one native `brain_search` ToolSpec with query/limit schema and
  workflow guidance.
- Register the same read-only search capability in root and child-agent registries.
- Test seam: search service and ToolSpec handler; inject the ripgrep runner to count calls.

**Wiring:**
- State: refreshed index records and transient per-query body matches.
- Producers: `ensure_fresh_index()` and the one ripgrep invocation.
- Consumers: native tool result and model workflow.
- Call site: model calls `brain_search(query, limit)`; returned paths are passed to regular
  `read`, `write`, or `edit` calls.

**Edge Cases:**
- Empty query: validation error and no ripgrep call.
- Literal punctuation: fixed-string matching.
- Body-only match: return excerpt.
- Equal lexical score: newer `updated` first.
- No matches: empty result.
- Ripgrep failure: clear tool error without partial results.

**Tasks:**
- Implement query tokenisation, field scoring, body parsing, excerpts, recency, sorting, and
  limit handling.
- Implement the single-call fixed-string ripgrep runner.
- Add and register the native `brain_search` ToolSpec.
- Add ranking, nested-layout, freshness, excerpt, and invocation-count tests.

**Deliverable:** The model can retrieve ranked brain entries from any user-defined layout.

**Verify:** Run search tests and assert ranking behavior, body excerpts, stale-index refresh,
and exactly one ripgrep process per non-empty query.

### 5. Persona and per-brain prompt guidance

**Approach:**
- Add the repository-owned `persona/prompts/brain.md` following the existing static prompt
  fragment convention.
- Extend the structured prompt builder to append persona brain guidance after the standard
  tools guidance and then append optional root `BRAIN.md` content.
- Read root `BRAIN.md` at each existing prompt snapshot so changes appear on the next turn.
- Omit missing/unreadable optional guidance with a warning; do not make prompt construction
  fail.
- Test seam: structured prompt builder output with patched persona and brain roots.

**Wiring:**
- State: static persona guidance and per-snapshot root guidance text.
- Producers: tracked `persona/prompts/brain.md` and user-owned `<brain_root>/BRAIN.md`.
- Consumers: root and child `SystemPrompt` static sections.
- Call site: existing root/child prompt builders load the two fragments in that order.

**Edge Cases:**
- No root `BRAIN.md`: persona guidance remains present.
- Root `BRAIN.md` changes: next prompt snapshot reflects it.
- Root guidance is unreadable: warn and continue.
- Root guidance contains frontmatter: treat it as prompt guidance, never as an index entry.

**Tasks:**
- Create `persona/prompts/brain.md` with the decided brain workflow guidance.
- Add root guidance loading and ordered prompt composition.
- Add tests for presence, ordering, absence, change detection, and read failures.
- Update the `brain_search` description with matching read/write/edit signposting.

**Deliverable:** Every standard root/child prompt explains the brain workflow and can include
user-specific root guidance after the persona guidance.

**Verify:** Build prompts with and without `BRAIN.md` and assert exact section ordering and
next-snapshot refresh behavior.

### 6. Contract verification and cleanup

**Approach:**
- Verify the implementation exposes exactly one dedicated native brain tool.
- Verify all requirements use the configured root and arbitrary layout rather than category
  assumptions.
- Verify no lifecycle code performs repository operations for the brain.
- Run focused tests, the relevant full test suite, formatter, and linter.
- Search implementation and plan-adjacent documentation for stale fixed-layout or access
  tracking assumptions.

**Tasks:**
- Add end-to-end tests covering write/edit → index → search → read workflow.
- Add configuration tests for environment override and default resolution.
- Add prompt/tool registry tests for root and child agents.
- Remove obsolete fixed-layout guidance from touched code and documentation.
- Run validation commands and fix failures.

**Deliverable:** The implemented brain contract is consistent across configuration, filesystem
operations, search, prompt guidance, and child-agent tool registration.

**Verify:** Run targeted brain tests, the full relevant pytest suite, `ruff check` on touched
Python files, `ruff format --check` on touched Python files, and `git diff --check` for the
change set.

## Not yet specified

- Automatic memory extraction.
- Embedding/vector retrieval.
- Searchable ranking for optional frontmatter fields.
- A future permissions or approval layer above Docker mounts and OS permissions.
- A future explicit rebuild command or user-facing index diagnostics.

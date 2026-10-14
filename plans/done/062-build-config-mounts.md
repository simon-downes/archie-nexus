# 062 — Configurable host mounts

Status: done
Branch: build-it/062-config-mounts (base: build-it/061-compaction-events @ 1819917)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/062-config-mounts
Updated: 2026-03-09

## Objective

Allow `archie-nexus` users to configure additional host-to-container bind mounts for agent sessions. Support the current use case of mounting host Git configuration and SSH key files, including wildcard source patterns such as `~/.gitconfig*` and `~/.ssh/id_ed25519*`, while retaining read-only mounts and explicit container targets.

Document the complete configuration format in `docs/config.md` and link it from the README and architecture documentation.

## Context

Nexus currently hard-codes its Docker bind mounts in `orchestrator/src/archie_orchestrator/lifecycle.py`; `NexusConfig` rejects an unknown top-level `mounts` key. `archie-og` accepts string or `[source, destination]` entries with Docker mode suffixes, and `archie-nextgen` uses additional `host:container:mode` mounts plus standard dotfiles. Nexus should adopt the compatible top-level `mounts` list while adding wildcard source expansion.

The host orchestrator resolves mounts before constructing `docker run`. Missing literal paths and wildcard patterns with no matches are skipped, matching the existing sibling behavior and avoiding startup failure when optional dotfiles are absent. Mounts are host-controlled configuration and may expose sensitive files, so the feature is explicitly documented as a trust boundary.

## Baseline

- Check commands: `uv run pytest`; `uv run ruff check .`; `uv run ruff format --check .`
- Setup: `uv sync`
- Setup leftovers: none
- Failing before this work: `uv run ruff format --check .` reports 19 pre-existing files needing formatting; `uv run pytest` (1129 passed) and `uv run ruff check .` pass
- Not included: `persona/skills/aws-agent-access/`, `plans/060-spec-container-aws-profiles.md`

## Decisions

| # | Decision | Chosen | Rejected | Why | Source |
|---|---|---|---|---|---|
| D1 | Where should configurable mounts live? | Top-level `mounts` list | Nested `orchestrator.mounts` or a new profile-specific section | Matches the existing `archie-og` and `archie-nextgen` user-facing format and keeps mounts applicable to every session | self |
| D2 | What entry forms should be supported? | String source with same-path destination, or two-item `[source, destination]` pair | Only a colon-delimited string format; mapping objects | Preserves `archie-og` compatibility while YAML lists avoid ambiguous colon parsing and support explicit targets | self |
| D3 | How should wildcard matches map to explicit targets? | A wildcard source with a pair destination always treats the destination path as a directory and appends each match basename; literal sources use the destination exactly | Require a destination wildcard or infer directory-ness from a trailing slash or host existence | The requested SSH example has `~/.ssh` without a trailing slash, and deterministic basename mapping avoids exposing unrelated files | self |
| D4 | What happens for missing paths and malformed entries? | Skip missing literal/glob matches with a warning; reject malformed schema entries during config loading | Fail every session for absent optional files; silently accept malformed data | Optional Git/SSH files commonly do not exist, while `msgspec` validation already gives Nexus’s standard config error path for malformed types | self |
| D5 | Which Docker mount modes are accepted? | Only a trailing `:ro` or `:rw` on the destination is accepted; no suffix defaults to `:rw`; source strings may use the same suffix | Accept arbitrary Docker option strings or add a separate typed `readonly` field | Limits config injection while matching the sibling syntax and existing Docker command construction | self |
| D6 | How should wildcard expansion be ordered and represented? | Split an absolute expanded glob into its parent directory and filename pattern, use non-recursive `Path.glob`, sort matches lexically, and emit one `-v` per match | Recursive globbing or one directory mount | Python `Path.glob` rejects absolute patterns; the split preserves absolute home-directory examples while separate mounts limit exposure | self |

## Requirements

- MUST accept a top-level `mounts` YAML list containing source strings and `[source, destination]` pairs, represented as `tuple[str | tuple[str, str], ...]` after decoding.
  - AC: `load_nexus_config` parses both forms into the typed config, and unknown/malformed mount entries raise `ConfigError`.
- MUST expand wildcard source patterns against the host filesystem before starting the container.
  - AC: `~/.gitconfig*` and `~/.ssh/id_ed25519*` produce one Docker volume argument per matching host file, sorted lexically.
- MUST mount a source string at the corresponding container path under the container user home, and MUST map explicit destinations under `~` to the container user home.
  - AC: a configured `~/.gitconfig` produces `/home/archie/.gitconfig:/home/archie/.gitconfig:rw`.
- MUST support explicit target directories for wildcard matches by appending each matched basename to the target directory.
  - AC: `["~/.ssh/id_ed25519*", "~/.ssh:ro"]` produces separate `:ro` mounts for `id_ed25519` and `id_ed25519.pub` under `/home/archie/.ssh`.
- MUST preserve explicit mount modes and default unspecified modes to `rw`.
  - AC: `:ro` appears in the generated Docker command, while an omitted mode generates `:rw`.
- MUST skip missing optional sources and wildcard patterns with no matches without preventing session startup.
  - AC: the Docker command contains no volume for the missing source and `start_session` still returns a descriptor.
- MUST retain all existing fixed mounts and append configured mounts to the Docker command.
  - AC: lifecycle tests still find workspace, agent, shared, persona, Nexus home, and agent-directory mounts alongside configured entries.
- MUST document the schema, wildcard behavior, modes, destination rules, examples, and sensitive-file warning in `docs/config.md`, and link to it from `README.md` and `docs/architecture.md`.
  - AC: `docs/config.md` contains the Git and SSH examples, and both `README.md` and `docs/architecture.md` contain a link to `config.md`.

Out of scope: automatic SSH agent forwarding, copying files into the image, environment-variable interpolation in mount paths, per-orchestrator-profile mount sets, CLI commands for editing mounts, and changing the existing fixed mounts.

## Design

### Overview

Add `mounts: tuple[str | tuple[str, str], ...]` to `shared/src/archie_shared/schemas.py`. Add the resolver as `orchestrator/src/archie_orchestrator/mounts.py`; it expands `~` paths against `Path.home()`, splits absolute glob patterns into parent and pattern before calling `Path.glob()`, translates `~` destinations to `/home/archie`, validates only `:ro` and `:rw`, applies `rw` by default, and returns Docker `-v` specifications. `start_session` appends those specifications after the existing fixed mounts.

### Data flow

`~/.nexus/config.yaml` → `load_nexus_config()` → `NexusConfig.mounts` → `start_session()` → `resolve_mounts()` in `orchestrator/mounts.py` → separate Docker `-v host:container:mode` arguments.

A literal missing source or unmatched glob yields a warning and no mount. A wildcard source paired with a destination maps each match to `destination / match.name`; a literal source paired with a destination maps to the destination exactly. A source match that is not a regular file or directory remains eligible for Docker mounting if it exists; Docker reports any runtime-specific rejection.

### Error handling

Config type/shape errors surface through the existing `ConfigError` loader path before session creation. A mount destination must be a non-empty path optionally ending in exactly `:ro` or `:rw`; any other suffix, empty mode, or missing destination raises `ValueError` before `docker run`. Missing optional paths are skipped with a warning. No mount is created from a wildcard string itself, so a glob never becomes a literal path inside the container.

### Config / infra

No new dependency or environment variable. The new `mounts` config key is host-side and is not copied into the agent container beyond the resulting bind mounts.

## Milestones

### [x] M1 — Resolve configured mounts into Docker volume arguments

- **Approach:** Add `mounts: tuple[str | tuple[str, str], ...]` and implement `resolve_mounts` in the new host-only `orchestrator/src/archie_orchestrator/mounts.py`. Split absolute expanded glob patterns into parent and filename pattern before `Path.glob()`, sort matches, append basenames for wildcard pair destinations, and validate only `:ro`/`:rw`. Keep the container home fixed at `/home/archie` from `CONTAINER_USER`.
- **Test seam:** call `resolve_mounts` directly for expansion/parsing behavior, then assert the public `start_session` captured Docker command and `load_nexus_config` decoding.
- **Wiring:** `NexusConfig.mounts` flows through `start_session`; configured `-v` pairs append after fixed lifecycle mounts.
- **Edge cases:** missing literal or unmatched glob → warning and skip; wildcard pair to target directory → one basename-preserving mount per match; `:ro`/`:rw` → preserved; malformed schema → `ConfigError`.
- **Tasks:**
  - Add mount config typing and loader coverage.
  - Add resolver tests for literals, globs, explicit targets, modes, sorting, and absent paths.
  - Wire resolved mounts into the Docker command and update lifecycle command assertions.
- **Deliverable:** Sessions started with configured mounts include the expected separate Docker bind mounts.
- **Verify:** `uv run pytest tests/test_nexus_config.py tests/test_orchestrator_mounts.py tests/test_orchestrator_lifecycle.py` passes; the result includes config decoding, resolver error/warning, wildcard ordering, and lifecycle command assertions.
- **Outcome:** Added typed `mounts` config, absolute wildcard expansion, basename-preserving target directories, `:ro`/`:rw` validation, missing-source skipping, and lifecycle wiring. Focused verification passed: `43 passed`; touched-file Ruff passed.

### [x] M2 — Publish configuration documentation and lifecycle contract

- **Approach:** Create `docs/config.md` as the canonical config guide, add Git/SSH wildcard examples, explain target and mode syntax plus the sensitive-file trust boundary, and link it from README and architecture docs. Update the architecture fixed-mount section to distinguish fixed mounts from configured mounts.
- **Test seam:** exact repository inspection: `docs/config.md` includes `~/.gitconfig*`, `~/.ssh/id_ed25519*`, `:ro`, and the trust warning; `README.md` and `docs/architecture.md` each link to `docs/config.md`.
- **Tasks:**
  - Write the dedicated configuration reference.
  - Link it from README and architecture documentation.
  - Run touched-file formatting and the full checks.
- **Deliverable:** Users can configure and understand Git/SSH mounts without reading source code.
- **Verify:** `uv run pytest` and `uv run ruff check .` pass; `uv run ruff format --check .` reports only the 19 baseline files; grep confirms `~/.gitconfig*`, `~/.ssh/id_ed25519*`, `:ro`, and `docs/config.md` links in the named files.
- **Outcome:** Added `docs/config.md`, linked it from README and architecture docs, and documented schema, wildcard expansion, modes, target behavior, skipped optional files, and sensitive-file risks. Full verification passed: `1137 passed`, Ruff lint passed, and formatting remained at the 19-file baseline.

## Change log

- C1 (planning): Resolved plan-review gaps by fixing the mount grammar, wildcard target-directory rule, absolute glob expansion approach, resolver module path, and exact verification seams. The implementation now has no material design choices left open.
- C2 (review): Rejected recursive `**` glob patterns after correctness review found that `Path.glob` would otherwise recurse despite the documented non-recursive contract; centralized glob splitting and clarified fixed versus configured architecture mounts.
- C3 (follow-up): Incorporated field feedback by rejecting directory-component wildcards, empty or relative targets, and targets that equal or nest under fixed mounts. Added clear pre-Docker errors, missing-source lifecycle coverage, and documentation for absolute paths and the full config surface.
- C4 (review): Removed duplicated container target constants by sharing lifecycle paths from `docker.py`, simplified mount error labels, and removed the impossible wildcard assertion. Final review found no further issues.

## Risks

- Bind mounts can expose private keys or other host files to the agent container; the docs must make this explicit, and users should use `:ro` for credentials.
- Docker permits more mount syntax than this feature intends to document; the implementation should validate only the supported trailing mode forms rather than accept arbitrary option injection.

## Review

- Plan review: one pass; fixed all agreed gaps around grammar, directory targets, absolute globs, module placement, and verification seams.
- Code review: four rounds total. The original correctness/conventions/risk reviews found and fixed recursive-glob handling; follow-up correctness found and fixed validation-after-missing-source behavior; the final correctness review approved. Convention suggestions were applied, and risk found no issues.
- Merge risk: Medium: configured mounts can expose or modify host files in agent containers, but modes are constrained, sensitive-file warnings are documented, and missing/recursive patterns are covered by tests.

## Outcome

Implemented top-level configurable host mounts for Nexus sessions. Source strings and source/destination pairs support `:ro`/`:rw`, wildcard expansion into separate mounts, explicit target directories, optional missing files, and recursive-glob rejection. Added `docs/config.md` with Git/SSH examples and linked it from the README and architecture guide.

Final verification passed: `1146 passed`, Ruff lint passed, touched-file formatting passed, and `git diff --check` passed. The final review pass approved the shared-constant and error-message cleanup. The repository-wide format check retains only the 19 baseline failures.

PR: error — `pr.py open` could not fetch origin: GitHub SSH returned `Permission denied (publickey)`
CI: none

## Follow-ups

- Per-profile mount sets and SSH agent forwarding remain out of scope.

# 093 — Scratch workspace for chat sessions

Status: building
Branch: build-it/093-scratch-workspace (base: main @ fc1f141)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/093-scratch-workspace

## Objective

Give general-question ("chat") sessions a clean `/workspace` every time. One configured
workspace, `global.scratch_workspace`, mounts a fresh per-session directory instead of the
shared workspace directory, so files from earlier chats are never read or searched by later
ones. Unused per-session directories are removed when the session stops.

## Context

The user previously used a `tmp` workspace for ad-hoc questions; it filled up over time and
agents searched and read irrelevant files. They chose a light structured approach: keep
`chat` as an ordinary workspace name (so the session store, insights, search, titles and
the TUI picker keep working, since all key on `record.workspace`) and change only what is
mounted at `/workspace`. Config is a single key, `global.scratch_workspace` (e.g. `"chat"`).
There is no session resume, so a per-session directory need not outlive its session beyond
what cleanup keeps.

Codebase facts:

- `orchestrator/src/archie_orchestrator/lifecycle.py`: `prepare_start` resolves the
  workspace with `workspaces.resolve_workspace` and builds the `docker run` argv, mounting
  the resolved directory at `/workspace:rw`; `run_start` runs it and waits. Only a
  `RuntimeError` (and a Kiro `DockerError`) from `wait_for_ready` calls
  `_cleanup_failed_start`; a native readiness timeout raises `DockerError` and leaves the
  container running, and `_reconcile` can later adopt it. `stop_session(session_id)` stops
  the container and removes a prompt-less log.
- The tracker (`tracker.py`) calls these through `DockerRuntime` (`prepare`, `run`, `stop`).
  `app.py` constructs `DockerRuntime()` and passes the tracker
  `workspace_root=lambda: expand_workspace_root(config_source.latest())` (084 D29).
- Session locations: `tracker.locate_session(root, workspace, state)` and
  `refresh_sessions(root, sessions)` both map container paths through
  `resolve_workspace(root, workspace)`. With a per-session mount this would map
  `/workspace/x` to `chat/x` instead of `chat/<id>/x`.
- The agent reads `/workspace/AGENTS.md` into its prompt (`agent/.../prompt.py`
  `read_agents_context`); Kiro does the same.
- Session IDs are `<workspace>-<ulid>` (`shared/.../session/identity.py`, `split_id`).
- If a bind-mount source is missing, Docker creates it as root on Linux, so the directory
  must be created before `docker run`.
- Workspace content is session-writable, so `workspaces.py` reads it without following
  symlinks (`_open` with `O_NOFOLLOW`). `chat/AGENTS.md` may have been written by an earlier
  non-scratch session in `chat`.

## Baseline

- Check commands: `scripts/dev test`, `scripts/dev ruff check .`, `scripts/dev ruff format --check <files>`
- Setup: `scripts/dev sync`
- Setup leftovers: none (`git status --short` clean after sync)
- Failing before this work (32 entries, all `concurrent.futures.CancelledError` timeouts in
  agent-app tests that start the agent in-process): `tests/test_ws_integration.py` (4 failed,
  9 errors), `tests/test_question_tool.py` (10), `tests/test_host_config.py` (2),
  `tests/test_local_api.py` (1), plus 6 teardown errors attributed to `archie_agent.app` /
  `asyncio`. List saved at `/tmp/baseline-fail.txt` during the build.
- Not included: the user's uncommitted `cli/src/archie_cli/tui/pane.py`,
  `tests/test_tui_app.py`, `.DS_Store`, `plans/060-spec-container-aws-profiles.md`,
  `plans/070-build-nexus-plugins.md` (unrelated).

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Shape of the feature | Per-session directory mounted at `/workspace` for one named workspace | plain `chat/` dir; AGENTS.md telling the agent to use `/tmp`; tmpfs with no host mount | Keeps every `record.workspace` consumer unchanged; isolation doesn't depend on the agent obeying a prompt; artefacts a chat produces stay on the host | user (discussion) |
| D2 | Config | `global.scratch_workspace: str \| None`, one name | a list of scratch workspaces; a marker file in the directory | User asked for a single key | user (discussion) |
| D3 | Default | `None` (off) | `"chat"` | Turning it on changes what an existing `chat` workspace mounts; off by default keeps current behaviour for every existing config | self |
| D4 | Where per-session dirs live | `<workspace_root>/<scratch>/<session_id>/` | `~/.nexus/scratch/<id>`; a hidden `.sessions/` subdir | Next to the workspace's `AGENTS.md`, browsable on the host under the workspace the user picked; session IDs are unique and already start with the workspace name | self |
| D5 | Workspace instructions | Copy `<scratch>/AGENTS.md` into the new dir before `docker run`, only when `lstat` shows a regular file (a symlink is skipped with a warning) | a nested read-only file bind mount; no instructions | A nested file mount leaves an empty placeholder file on the host and is a second mount to reason about; a copy works for native and Kiro alike | self |
| D6 | When to create the dir | In `run_start`, before `run_container` | in `prepare_start` | `prepare_start` stays free of workspace side effects (082 D15: it only validates and builds argv); the path is computed in `prepare_start` and carried on `PreparedStart` | self |
| D7 | Cleanup rule | Remove the dir when it is empty, or holds only an `AGENTS.md` that is a regular file (by `lstat`) byte-identical to the workspace's current regular-file `AGENTS.md`; otherwise keep it. Removal is `unlink` + `rmdir`, never recursive | always delete; never delete; age-based prune | Removes the common no-artefact case without ever deleting something the session wrote or changed | self |
| D8 | When to clean up | After a successful `stop_session`; and when `run_start` fails, only if `docker.container_running` then reports the container not running (a failed or raising check keeps the dir) | also on crash/exit detection and at orchestrator start; always on a failed start | A native readiness timeout leaves the container running and adoptable, so a failed start doesn't prove the dir is unused; detecting other ends safely needs the live set, and deleting a dir under a running container loses its writes | self |
| D9 | How stop learns the config | `DockerRuntime(config=...)` callable; `DockerRuntime.stop` passes `config` to `lifecycle.stop_session(session_id, config=...)`, which derives the workspace with `split_id` | change the `Runtime.stop` protocol to take a path; have the tracker do the cleanup | Keeps the `Runtime` protocol and fakes unchanged; file cleanup stays in `lifecycle` next to the log removal | self |
| D10 | Location tracking for scratch sessions | Map through the per-session dir | skip location for scratch sessions; leave the wrong mapping (location always None) | User flagged cloning repos in chats as plausible; one helper (`workspaces.session_directory`) serves stop-time cleanup and location, so the cost is a parameter on two functions (`prepare_start` builds `resolved / session_id` itself, as the dir doesn't exist yet) | self (user asked for an opinion) |
| D11 | How the tracker learns the scratch name | New `scratch_workspace: Callable[[], str \| None]` tracker argument, read on each use, wired in `app.py` from `config_source.latest()` | pass the whole config | Same injection pattern as `workspace_root` (084 D29) | self |

## Requirements

- MUST accept `global.scratch_workspace` as an optional string (default `null`), rejecting a
  value that isn't a single directory name (contains `/` or `\`, or is `""`, `.`, `..`).
  - AC: loading `global: {scratch_workspace: chat}` gives `"chat"`; the default is `None`;
    `"a/b"`, `"a\\b"`, `"."`, `".."` and `""` raise `ConfigError` (a direct
    `GlobalConfig(...)` raises `ValueError`).
- MUST, when starting a session in the scratch workspace, mount
  `<workspace_root>/<scratch>/<session_id>` at `/workspace:rw` instead of the workspace
  directory, creating it (and copying the workspace's `AGENTS.md` into it, if that is a
  regular file) before `docker run`.
  - AC: the argv has `<ws>/<session_id>:/workspace:rw` and not `<ws>:/workspace:rw`; at
    `run_container` time the directory exists and its `AGENTS.md` matches the source.
  - AC: a workspace without `AGENTS.md`, or whose `AGENTS.md` is a symlink, gets an empty
    directory.
- MUST leave sessions in any other workspace, and all sessions when the key is unset,
  mounted exactly as today.
  - AC: existing `test_start_session_correct_docker_command` passes unchanged; with the key
    set to another name, a `myproject` start mounts `myproject` itself and creates nothing.
- MUST remove the per-session directory after the session is stopped through
  `stop_session`, or its start fails in `run_start`, when it is unused (empty, or only an
  `AGENTS.md` identical to the workspace's current one), and keep it otherwise.
  - AC: stop removes an empty dir and an AGENTS.md-only dir; keeps a dir with another file,
    and one whose `AGENTS.md` differs or is a symlink; a failed `run_start` removes the dir
    it created when the container isn't running and re-raises the original error; when
    `container_running` returns True or raises, the dir is kept.
  - AC: a cleanup `OSError` is logged as a warning and doesn't change `stop_session`'s
    result or raise.
- MUST report a scratch session's location relative to its per-session directory.
  - AC: with a repo at `<ws>/<session_id>/app`, an edit of `app/a.py` gives location repo
    `app`; `refresh_sessions` re-reads it from the same directory; a missing per-session dir
    gives `None`.
- MUST document the key in `docs/config.md` and the mount in `docs/architecture.md`.
  - AC: both files mention `scratch_workspace`.

Out of scope: cleaning up after sessions that end other than by `stop_session` (container
crash, Docker restart); age-based pruning; creating the `chat` workspace or its `AGENTS.md`
(host-side, user-owned); TUI changes (the workspace appears in the picker as any other).

## Design

### Overview

`prepare_start` points the `/workspace` mount of a scratch session at
`<workspace>/<session_id>`, `run_start` creates it, and one helper
(`workspaces.session_directory`) finds it again for `stop_session`'s cleanup and the
tracker's location mapping.

### Code structure

- `shared/src/archie_shared/schemas.py`: `GlobalConfig.scratch_workspace: str | None = None`
  with `__post_init__` validation (pattern: `KiroConfig.__post_init__`).
- `orchestrator/src/archie_orchestrator/workspaces.py` (new functions):
  - `session_directory(root, workspace, session_id, scratch_workspace) -> Path`: the
    resolved workspace dir, or `<that>/<session_id>` for the scratch workspace; raises
    `ValueError` if either doesn't exist (used by `stop_session` and the tracker).
- `orchestrator/src/archie_orchestrator/lifecycle.py`:
  - `PreparedStart.scratch_dir: Path | None = None` and `agents_md: Path | None = None`.
  - `prepare_start`: when `workspace == config.global_.scratch_workspace`, mount source is
    `resolved / session_id`.
  - `run_start`: `_create_scratch_dir(prepared)` before `run_container`; any exception →
    if `container_running(cname)` is False, `_remove_unused_scratch_dir(...)`; then re-raise
    the original exception.
  - `_remove_unused_scratch_dir(directory, agents_md) -> bool`: D7 rule; logs `OSError`.
  - A regular-file check helper using `os.lstat` + `stat.S_ISREG`, used for the copy source
    and both sides of the D7 comparison.
  - `stop_session(session_id, *, config: NexusConfig | None = None)`: after the log removal,
    if the session's workspace (`split_id`) is the scratch workspace, remove its unused dir.
- `orchestrator/src/archie_orchestrator/tracker.py`: `DockerRuntime.__init__(config=None)`;
  `stop` passes `config=self._config()` when set. `SessionTracker(scratch_workspace=...)`;
  `locate_session(root, workspace, state, *, session_id="", scratch_workspace=None)` and
  `refresh_sessions(root, sessions, scratch_workspace=None)` resolve through
  `session_directory`.
- `orchestrator/src/archie_orchestrator/app.py`: `DockerRuntime(config=config_source.latest)`
  and `scratch_workspace=lambda: config_source.latest().global_.scratch_workspace`.

### Error handling

- Creating the dir or copying `AGENTS.md` fails → `run_start` raises (`OSError`), the
  tracker records the start as `failed` with the message (existing `_start` path).
- Cleanup fails → warning logged, nothing raised.
- Config unreadable at stop (`config_source.latest()` raising) → can't happen after a valid
  startup; `DockerRuntime.stop` reads the config first, inside a `try` that falls back to
  `config=None` on `ConfigError`, so the stop never depends on config.
- `docker stop` fails but the container has gone → the tracker marks the session `stopped`
  but `stop_session` raised before cleanup, so the dir stays (see Risks).

## Milestones

### [x] M1 — Scratch sessions mount a fresh per-session directory

- **Approach:** config key + validation in `schemas.py`; `scratch_dir`/`agents_md` on
  `PreparedStart`; mount source in `prepare_start`; `_create_scratch_dir` in `run_start`
  (`mkdir(parents=False, exist_ok=False)` so a clash fails loudly; `shutil.copyfile` for
  `AGENTS.md`). Docs: `docs/config.md` application settings + a short "Scratch workspace"
  section; `docs/architecture.md` mounts table row.
- **Test seam:** `load_nexus_config` (tests/test_nexus_config.py); `prepare_start` /
  `run_start` with `run_container` patched (tests/test_orchestrator_lifecycle.py, pattern
  `_start_capturing`).
- **Deliverable:** `archie start chat` with `scratch_workspace: chat` runs in an empty
  `/workspace` holding only the copied `AGENTS.md`.
- **Verify:** `scripts/dev test tests/test_nexus_config.py tests/test_orchestrator_lifecycle.py`
  passes.
- **Outcome:** `GlobalConfig.scratch_workspace` with validation; `prepare_start` mounts
  `<ws>/<session_id>` and carries `scratch_dir`/`agents_md`; `run_start` creates the dir
  (`mkdir` without `exist_ok`) and copies a regular-file `AGENTS.md` (symlink skipped with a
  warning) before `_run`. `docs/config.md` gained a "Scratch workspace" section (it also
  describes M2's cleanup) and the architecture mounts table a row. Verify: 89 passed in
  `tests/test_nexus_config.py tests/test_orchestrator_lifecycle.py`; ruff check and format
  clean on touched files.

### [x] M2 — Unused per-session directories are removed

- **Approach:** `_remove_unused_scratch_dir` in `lifecycle.py`; called from `run_start`'s
  failure path and `stop_session(config=...)`. `DockerRuntime(config=...)` in `tracker.py`;
  wire `config_source.latest` in `app.py`.
- **Test seam:** `run_start`, `stop_session` (tests/test_orchestrator_lifecycle.py);
  `DockerRuntime.stop` with `lifecycle.stop_session` patched (tests/test_session_tracker.py).
- **Edge cases:** dir already gone → no error; `AGENTS.md` changed → kept; extra file → kept;
  `rmtree`/`unlink` `OSError` → warning.
- **Deliverable:** stopping a chat session that wrote nothing leaves no directory behind.
- **Verify:** `scripts/dev test tests/test_orchestrator_lifecycle.py tests/test_session_tracker.py`
  passes.
- **Outcome:** `_remove_unused_scratch_dir` (listdir, `lstat` regular-file checks, `unlink` +
  `rmdir`, `OSError` logged); `run_start` releases the dir after a failure only when
  `container_running` returns False; `stop_session(config=...)` finds the dir with the new
  `workspaces.session_directory` (added here rather than M3, since stop needs it);
  `DockerRuntime(config)` reads config before stopping and falls back to no cleanup on
  `ConfigError`; `app.py` passes `config_source.latest`. Verify: 244 passed across
  `test_orchestrator_lifecycle.py test_session_tracker.py test_workspaces.py
  test_sessions_api.py`; ruff clean.

### [ ] M3 — Scratch session locations map through the per-session directory

- **Approach:** `workspaces.session_directory`; `locate_session` / `refresh_sessions`
  parameters; tracker `scratch_workspace` callable read in `_location_once` and
  `_sweep_once`; wire in `app.py`. Note it in the architecture doc's location paragraph.
- **Test seam:** `tracker.locate_session`, `tracker.refresh_sessions`, and a tracker with
  `scratch_workspace` set as an attribute after `make_tracker` (as `_located_tracker` sets
  `workspace_root`), fed tool frames (tests/test_session_location.py).
- **Deliverable:** a chat session that clones a repo shows that repo as its location.
- **Verify:** `scripts/dev test tests/test_session_location.py tests/test_workspaces.py`
  passes.
- **Outcome:**

## Deviations

## Risks

- Changing `scratch_workspace` or `workspace_root` while a scratch session runs changes how
  its location and cleanup resolve (they read the current config). Cleanup only removes
  unused dirs, so the worst case is a missed cleanup or a `None` location.
- Sessions that end without a successful `stop_session` (crash, Docker restart, a failed
  `docker stop` on a container that has gone, a failed start whose container still runs)
  leave their dir.
- With the key on, `/api/workspaces` lists a per-session dir that holds a `.git` (a chat that
  ran `git init` in `/workspace`) as a repo of `chat`, and kept dirs count toward
  `MAX_REPOS` scanning. Accepted: it describes what is on disk.
- An agent still running pre-change code rejects the new key on config sync
  (`forbid_unknown_fields`) and keeps its last valid config with a warning, until restarted.
- The deliverable (a real `archie start chat`) needs Docker, which isn't available in the
  build container; it is covered by unit tests on the argv and filesystem effects only.

## Review

- Plan review: 1 pass. Fixed: D8 failed-start cleanup now checks `container_running` (native
  readiness timeout leaves the container running); D10/Overview reconciled with where the
  helper is used; symlink-safe `AGENTS.md` copy and comparison (`lstat`); `ConfigError`
  read order in `DockerRuntime.stop`; AC covers `.` and `\`; test seam for the tracker
  attribute; Risks extended (`workspace_root` changes, failed `docker stop`, `scan_repos`
  listing, old agents and the new key, no Docker for an end-to-end check). Context bullet on
  `_cleanup_failed_start` corrected. No findings rejected. No ❌, so no second pass.

## Outcome

PR: pending
CI: pending

## Follow-ups

- Prune unused scratch dirs of ended sessions (crash/Docker restart), using the tracker's
  live set.

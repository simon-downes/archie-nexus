# 094 — Kiro OOM resilience

Status: planning
Branch: build-it/094-kiro-oom-resilience (base: (detached) @ ec4b472)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/094-kiro-oom-resilience

## Objective

When the Colima VM runs out of memory, the kernel's OOM killer kills Kiro's Node process, and
the Archie session is dead for good. After this change, tool processes are killed before
Kiro, a Kiro that dies anyway is restarted with its conversation reloaded, and the session,
the model and the logs say plainly that the OOM killer was involved.

## Context

The user investigated two crashed Kiro sessions (`tillo-platform-01m7hze8jh` and
`tillo-platform-01m7qk7xy0`) in session `tillo-platform-01m7qypreh`. `dmesg` in the VM showed
VM-wide OOM kills (`global_oom`): tofu and terraform-provider processes were killed first,
which didn't free enough memory, and then Kiro's `node` process (about 420 MB) was. At 14:00
the kernel killed `node` in four session containers within about 30 seconds. The `kiro-cli`
launcher reports its child's death as exit code 1, so the session only said "Kiro backend
stopped (exit code 1)".

The user has already given the VM more memory and swap and added a host monitor. They asked for
application-level resilience and chose four of the suggestions for one PR: 2 (make tool
processes the OOM killer's first choice), 3 (restart Kiro and reload its session), 5 (name the
OOM cause; persist Kiro's stderr) and 6 (show a dead backend as dead). They declined
per-container memory limits because some sessions need more memory.

Codebase facts found during discovery:

- Tool processes come from `run_shell` and `run_exec` in
  `agent/src/archie_agent/exec/tools/_subprocess.py`, and from the exec runner in
  `agent/src/archie_agent/exec/tool.py`. Both backends use them (Kiro through
  `kiro/mcp_server.py`, native through `harness.py` and `agents.py`).
- The exec runner is spawned without `start_new_session=True`, but interrupt and cleanup call
  `kill_process_group` on it. That signals the agent's own process group (pgid 1, where
  uvicorn is PID 1), so an interrupted `exec` probably also signals the agent itself and any
  other exec runner.
- `KiroHarness._on_exit` (`kiro/harness.py`) marks the backend dead for the container's life;
  `status_event` still reports `idle` when dead. Plan 077 D20 rejected auto-restart because
  "restart loses Kiro's conversation state anyway".
- A spike against real Kiro (0.66.91, `--agent-engine=v3`) in this container showed:
  SIGKILL of the Node child makes `kiro-cli` exit 1 within 0.2 s; a new `kiro-cli` reloads
  the session with ACP `session/load` in 0.7 s and the model recalls the earlier turn;
  during the load Kiro replays the history as `session/update` frames
  (`user_message_chunk`, `agent_message_chunk`, `context_usage`, `turn_completion`, …) before
  the response. With `_meta.kiro.modeId: "archie"` the mode is `archie` after load and the
  model and effort are kept, but `contentCollection` is back to `enabled`.
- `/sys/fs/cgroup/memory.events` exists in session containers and has an `oom_kill` counter
  for the container's cgroup. An unprivileged process can raise its own
  `/proc/self/oom_score_adj` (checked: `echo 1000 > /proc/self/oom_score_adj` works and
  children inherit it).
- The orchestrator tracker (`orchestrator/src/archie_orchestrator/tracker.py`) stores a
  `session_status` state of `error` as the record status, which the TUI shows as `✕` with a
  red `TURN FAILED` badge.

## Baseline

- Check commands: `env -u ARCHIE_BACKEND scripts/dev test`, `scripts/dev ruff check .`,
  `scripts/dev ruff format --check .`
- Setup: `scripts/dev sync`
- Setup leftovers: `.venv-archie/`
- Failing before this work: `ruff format --check` wants to reformat
  `persona/skills/python/assets/output.py`. Plain `scripts/dev test` fails 17 tests and errors
  9 inside a Kiro session container, because they inherit `ARCHIE_BACKEND=kiro`; with
  `env -u ARCHIE_BACKEND` all tests pass (5682).
- Not included: the user's uncommitted 093 changes in their checkout (`docs/config.md`,
  `orchestrator/src/archie_orchestrator/lifecycle.py`, `plans/done/093-build-scratch-workspace.md`,
  `shared/src/archie_shared/schemas.py`, `tests/test_nexus_config.py`,
  `tests/test_orchestrator_lifecycle.py`); this build is based on `origin/main`, not on the
  checkout's `build-it/093-scratch-workspace` branch.

# 094 — Kiro OOM resilience

Status: building
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

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Base of the build | `origin/main` (ec4b472), through a temporary detached checkout | stacking on `build-it/093-scratch-workspace` | the change doesn't touch 093's files; an independent PR merges in any order | user Q1 |
| D2 | How to settle whether Kiro can reload a killed session | spike against real Kiro, with the key read once from the running `kiro-cli`'s environment | designing for both outcomes untested | removes the largest unknown before the restart design is fixed | user Q2 |
| D3 | How tool processes get a high OOM score | the child shell writes `/proc/self/oom_score_adj` before running the command: a prefix for `run_shell`, an `sh -c '…; exec "$@"'` wrapper for argv spawns | `preexec_fn` (not safe with the agent's threads); `choom` (extra dependency on util-linux); writing `/proc/<pid>/oom_score_adj` after spawn (races the child's first fork); orchestrator `--oom-score-adj` below 0 for the container | raising one's own score needs no privilege, and descendants inherit it; a negative container score would make the kernel prefer VM services such as sshd over Kiro | self |
| D4 | Score for tool processes | `1000` | `500`; per-tool values | `1000` makes every tool process preferred over every non-tool process in the VM, whatever their sizes; Kiro's `node` scores about 50 at 420 MB of 7.7 GB | self |
| D5 | Which spawns get the score | `run_shell`, `run_exec` and the exec runner (so every descendant too), for both backends | `shell` only; Kiro sessions only | the 6.4 GB Python process killed on 14 Nov was most likely an exec script; native sessions lose their agent process the same way | self |
| D6 | How the agent detects that the OOM killer ran | compare the `oom_kill` counter in `/sys/fs/cgroup/memory.events` before and after (Kiro's lifetime, a tool call) | exit codes (`kiro-cli` reports 1 for a SIGKILLed child); `docker inspect` from the orchestrator (host-side, after the fact) | the counter is readable in the container and counts this container's kills, including VM-wide ones | self |
| D7 | How Kiro recovers | start `kiro-cli` again and `session/load` the same Kiro session; a failed restart leaves the backend dead | `session/new` as a fallback (the model would lose the conversation without noticing); no restart | the spike showed load works after SIGKILL and keeps the conversation | self |
| D8 | Restart limit | at most 3 restarts in any 30 minutes, then dead | unlimited; 1 per session | a VM that keeps running out of memory shouldn't restart Kiro in a loop; 3 covers a busy afternoon like the one investigated | self |
| D9 | The turn that was running when Kiro died | ends with `turn_error`; isn't resumed; the next prompt sent to Kiro starts with a note about the cut-off turn | resending the turn automatically | an automatic resend would repeat the memory-heavy work that caused the kill | self |
| D10 | How a dead backend shows in the TUI | `session_status.state: error` (no schema change) | a new `SessionState` value or a new status field | `error` already exists in the contract, the tracker and the TUI; a new value is a cross-package contract change that an older orchestrator would drop as malformed | self |
| D11 | Messages, model switches and `/compact` that arrive during a restart | wait for the restart to finish, then run or get the dead message | reject at once with a notice | a restart takes about a second; making the user resend is worse | self |
| D12 | Tool processes still running when Kiro dies | kill them (`ArchieMcpServer.cancel_all`) before restarting | leave them to finish | nobody can receive their results, and they are what used the memory | self |
| D13 | Where Kiro's stderr and exit are recorded | the frame log, as `dir: "err"` and `dir: "exit"` entries | a separate stderr file | the frame log is already the persisted Kiro debugging file under `~/.nexus` | self |
| D14 | What `session_status` says while a restart runs | the usual rules: `idle`, or `working` while a waiting message holds the turn | a separate restarting state | the restart takes about a second and the notice says what happened; a new state is a contract change (D10) | self |
| D15 | An argv spawn whose executable doesn't exist | spawn it unwrapped when `shutil.which(args[0])` finds nothing, so the `OSError` is raised as before | always wrap | the wrapper would turn the `OSError` into exit 127, changing `RunnerCrash` "Failed to spawn runner" (`tests/test_exec_tool.py::test_run_exec_bad_python`), and a missing `rg` in `fs.py` and `code.py` would show up as `ripgrep error (exit 127)` instead of an `OSError` | self |
| D17 | An interrupt while a message waits for a restart | drop the waiting message: no `user_message`, no `session/cancel`, and an `error_notice` kind `turn_error` "Interrupted before Kiro finished restarting; the message wasn't sent" | let the message run after the restart | otherwise the user's ESC is lost, and a `session/cancel` could reach Kiro in the middle of `session/load` | self |
| D16 | The window for counting OOM kills against a Kiro exit | since the later of Kiro's start and the start or end of the last turn | since Kiro started | after M1, tool kills hours earlier would otherwise blame the OOM killer for an unrelated crash | self |

## Requirements

- MUST give every process started through `run_shell`, `run_exec` or the exec runner, and its
  descendants, an `oom_score_adj` of 1000, and leave the agent's and Kiro's score unchanged.
  - AC: the `shell` tool running `cat /proc/self/oom_score_adj` returns `[exit: 0]\n1000`;
    `run_exec("cat", "/proc/self/oom_score_adj")` returns `1000`; an exec script reading the
    file returns `1000`; the test process's own value is unchanged afterwards.
  - AC: the wrapper doesn't change results: `shell("echo hi; exit 3")` returns exactly
    `[exit: 3]\nhi`.
- MUST start the exec runner in its own process group.
  - AC: the process passed to `on_start` has `os.getpgid(pid) == pid` (fails before the fix).
- MUST add a warning line to a `shell` result, and to an exec `RunnerCrash` error, when the
  container's `oom_kill` count rose while the command ran. The line is `[archie warning: the
  out-of-memory killer killed K process(es) in this container while this ran; the output may
  be incomplete. Run memory-heavy commands one at a time.]`.
  - AC: with `MEMORY_EVENTS` pointed at a temp file that the command itself bumps, the `shell`
    result contains the line with `killed 1 process(es)`; without a bump, the output has no
    warning.
  - AC: an exec script that bumps the temp file and then kills its own runner gets a
    `RunnerCrash` message containing the line.
- MUST name the OOM killer when Kiro exits unexpectedly and the `oom_kill` count rose since the
  later of that `kiro-cli`'s start and the start or end of the last turn (D16): in the
  `turn_error` of a cut-off turn, the live notice, and the agent's error log. The reason is
  `Kiro backend stopped (exit code N); the out-of-memory killer killed K process(es) in this
  container since HH:MM:SS UTC`. Otherwise it stays `Kiro backend stopped (exit code N)`. The
  live notice is `backend_restarted` (`<reason>. Kiro was restarted and the conversation
  reloaded.`) or `backend_stopped` (`<reason>. <why it can't run>. Start a new session.`).
  - AC: fake Kiro is SIGKILLed while idle after the test bumps the counter: the
    `backend_restarted` notice contains `out-of-memory killer`.
  - AC: fake Kiro exits mid-turn after the test bumps the temp counter: the persisted
    `turn_error` contains `exit code 3` and `out-of-memory killer`; without a bump it contains
    `exit code 3` and not `out-of-memory`.
- MUST write each `kiro-cli` stderr line that isn't ignored (redacted) to the frame log as
  `{"dir": "err", "msg": "<line>"}`, and an unexpected exit as
  `{"dir": "exit", "msg": {"code": N, "oom_kills": K}}`.
  - AC: after a fake start the frame log has an `err` entry `fake kiro: started` and none for
    the ignored SQLite warning; after a fake exit it has an `exit` entry with code 3.
- MUST, after Kiro exits unexpectedly once startup has finished: kill the Archie tool
  processes still running, start `kiro-cli` again, send `initialize` and `session/load` for
  the same Kiro session with `_meta.kiro.modeId: "archie"`, verify mode `archie`, set and
  verify `contentCollection: disabled` and `autopilot: on`, set the session's current model
  and effort, wait for the archie MCP server and tools, and send a live `error_notice` with
  kind `backend_restarted`. The steps share one 60 s deadline.
  - AC: after an `exit` turn, the next message runs a normal turn (`turn_complete` with
    `KIRO-OK`); the fake's record shows a second `initialize`, a `session/load` for
    `sess_fake`, and `contentCollection`=`disabled` and `model`=<current model> set after it.
  - AC: an `exit_tool` turn (fake starts `shell sleep 30`, then exits) leaves no `sleep`
    process running once the restart has finished.
  - AC: frames replayed during `session/load` produce no session-log events and don't change
    `context_pct`.
- MUST start the first prompt sent to Kiro after a restart that cut off a turn with a note
  saying so, and keep the canonical `user_message` as the user typed it. The prompt stays one
  text block: the note, a blank line, then the message. The note is `[Archie note: the Kiro
  backend stopped during your previous turn (<detail>) and was restarted with this
  conversation reloaded. That turn was cut off: its running tool calls and subagents were
  stopped and their results lost. Check the current state before you continue that work.]`,
  where `<detail>` is the text inside the reason's parentheses (`exit code N; the
  out-of-memory killer …`). A cut-off compaction sets no note.
  - AC: the fake's recorded prompt text starts with the note and ends with the message; the
    session log's `user_message.content` equals the message; the prompt after that has no note.
- MUST make a message, model switch or `/compact` that arrives during a restart wait for the
  restart, then go ahead, or get the dead response if the restart failed.
  - AC: a message sent while the restart runs completes with `turn_complete`.
  - AC: `switch_model` to another allowlisted model while the restart runs returns None and
    the fake records that model set after the `session/load`.
  - AC: an interrupt while a message waits drops it as D17 says; the fake records no
    `session/prompt` and no `session/cancel` for it.
- MUST restart at most 3 times in any 30 minutes; the next unexpected exit leaves the backend
  dead, with an `error_notice` kind `backend_stopped` that gives the reason.
  - AC: with the limit patched to 1, a second `exit` turn leaves the harness dead and sends
    `backend_stopped`.
- MUST leave the backend dead, with `error_notice` kind `backend_stopped` and the reason, when a
  restart fails: `session/load` errors, Kiro exits during the restart, a setting isn't
  confirmed, or the deadline passes.
  - AC: with `FAKE_KIRO_LOAD=error`, the harness is dead after the restart attempt, the notice
    contains the load error, and the next message gets the `turn_error` notice with
    `start a new session`.
- MUST NOT restart after a fail-closed stop (agent config error, a disallowed built-in tool,
  content collection turned back on).
  - AC: the `late_builtin` scenario records exactly one `initialize`.
- MUST report `session_status.state: error` while the backend is dead, and include the reason
  in the `turn_error` notice later messages get.
  - AC: after a failed restart, `status_event().state == "error"` and the next message's notice
    contains the reason and `start a new session`.
- MUST update `docs/kiro.md` (session control, debugging, limits) and `docs/architecture.md`
  (tracker states) to describe the new behaviour.
  - AC: `docs/kiro.md` mentions `session/load`, `backend_restarted`, `backend_stopped`, the
    `err` and `exit` frame-log entries and `oom_score_adj`; `docs/architecture.md`'s tracker
    lines say a Kiro backend that can't run reports `error`.

Out of scope: per-container memory limits (declined by the user), any orchestrator or TUI code
change, memory-pressure warnings before a tool starts, `GOMEMLIMIT` defaults, stopping idle
sessions, keeping `~/.kiro` on the host so a session survives its container, and a new
`SessionState` value or badge label.

## Design

### Overview

Three container-side changes. Tool spawns raise their own OOM score to 1000 through a shell
prefix or wrapper, so the kernel kills tools first in any OOM. A small `oom` module reads the
container's `oom_kill` counter, so tool results and Kiro exits can say the OOM killer ran. The
Kiro harness treats an unexpected exit after startup as recoverable: it kills orphaned tools,
relaunches `kiro-cli`, reloads the Kiro session, re-applies the settings, and tells the user;
when that isn't possible, the backend is dead and reports `state: error`.

### Code structure

- `agent/src/archie_agent/oom.py` (new): `TOOL_OOM_SCORE_ADJ = 1000`; `OOM_SCORE_ADJ_PATH`
  (`/proc/self/oom_score_adj`, patched in tests); `MEMORY_EVENTS`
  (`Path("/sys/fs/cgroup/memory.events")`, patched in tests); `oom_kill_count() -> int | None`
  (None when the file is missing or unreadable); `shell_prefix(command) -> str`;
  `argv_wrapper(args) -> list[str]` (D15); `kills_since(before) -> int`;
  `tool_warning(kills) -> str`.
- `agent/src/archie_agent/exec/tools/_subprocess.py`: `run_shell` and `run_exec` use the
  prefix and wrapper, and `run_shell` adds `tool_warning(k)` to stderr when the count rose,
  next to the existing `[archie warning: …]` line.
- `agent/src/archie_agent/exec/tool.py` `run_exec`: wraps the runner argv, adds
  `start_new_session=True`, adds `tool_warning(k)` to the `RunnerCrash` message when the count rose.
- `agent/src/archie_agent/kiro/acp.py`: `_read_stderr` also writes `err` entries; a public
  `record(direction, msg)` writes one frame-log entry (used by the harness for `exit`).
- `agent/src/archie_agent/kiro/harness.py`: `start` keeps the one-time setup (key, settings,
  ledger, MCP server, agent files) and calls a new `_launch(load_session_id, model_id,
  remaining)` that spawns `kiro-cli`, sends `initialize` and `session/new` or `session/load`,
  and applies and verifies the settings (the existing steps from `start`). `_on_exit`
  computes the reason, records the `exit` entry, and either schedules `_restart` or marks the
  backend dead. New state: `_oom_baseline`, `_exit_reason`, `_restarts` (a deque of
  monotonic times), `_restart_task`, `_restoring`, `_resume_note`, `_dead_reason` (also set
  by `_fail(reason)`, with `Kiro backend stopped` as the fallback), `_interrupted_wait`.
  `_oom_baseline` is a `(count, datetime)` pair, so the reason can say since when.
- `tests/fake_kiro.py`: `session/load` (sessions kept in `FAKE_KIRO_HOME/fake-sessions.json`,
  replays three frames, comes back with `contentCollection: enabled` like real Kiro),
  `FAKE_KIRO_LOAD` (`ok`, `error`, `exit`), and an `exit_tool` prompt.
- Tests: `tests/test_oom_tools.py` (new) for the tool side; `tests/test_kiro_harness.py` for
  the harness; `tests/test_exec_tool.py` for the runner's process group.

### Data flow

Unexpected exit after startup (`_started` and not `_closing` and not `_dead`):

1. `AcpClient._finalize` fails the outstanding requests and calls `_on_exit(code)`
   synchronously, before the failed turn resumes.
2. `_on_exit` reads the counter against `_oom_baseline` (set when `kiro-cli` starts and at
   each turn's start and end), builds `_exit_reason`, records the `exit` frame, logs at
   ERROR, cancels the pending question, and calls `self._mcp.cancel_all()`. If fewer than 3
   restarts happened in the last 30 minutes, it records one, sets `_resume_note` when a turn
   was active, and spawns `_restart()` as `_restart_task`; otherwise it marks
   the backend dead with `_dead_reason` and spawns a `backend_stopped` notice and a status.
   An `_on_exit` while `_started` is false (during startup or a restart) only marks the
   backend dead: the startup or restart path owns the error report, so each failure sends
   exactly one `backend_stopped`.
3. The failed turn's `_run_turn` takes `_exit_reason` (plus "; restarting Kiro" when a restart
   is scheduled) as its error, so the `turn_error` names the cause.
4. `_restart()` closes the old `AcpClient`, clears `config_options`, `mcp_status`,
   `tool_tags`, `config_errors` and `_started`, sets `_restoring`, and runs `_launch` with the
   Kiro session ID and the current model under a 60 s deadline. It checks `_closing` after
   each await, and closes the client it created on `CancelledError`. `_on_notification` ignores `session/update` frames
   other than `config_option_update` while `_restoring`.
5. Success: `_started` true, `_restoring` false, a `backend_restarted` notice and a status.
   Failure (`KiroStartupError`, `AcpError`, timeout): close the new client, mark dead with
   the reason, send `backend_stopped` and a status.
6. `handle_message`, `switch_model` and `run_manual_compaction` await `_restart_task`
   (shielded) before any other check; in `switch_model` that is before the model-list check.
   `_run_turn` prefixes and clears `_resume_note`. During shutdown the awaiter gets
   `CancelledError`, which is intended. `interrupt()` while `_restart_task` runs sends no
   `session/cancel`; it sets `_interrupted_wait`, and the waiting `handle_message` drops its
   message as D17 says.
7. `close()` sets `_closing`, then cancels and awaits `_restart_task` before closing
   `self._acp`, so a restart can't spawn `kiro-cli` after `close()` has closed the client.

### Error handling

- Counter missing or unreadable: `oom_kill_count()` returns None, and no OOM wording is added.
- `/proc/self/oom_score_adj` not writable: the prefix is `{ echo 1000 >PATH; } 2>/dev/null; `
  (a group, because a failed redirection is reported before a later `2>/dev/null` applies),
  so nothing is printed and the command runs as before.
- Kiro exits during a restart: its pending requests fail, so `_launch` raises and the restart
  fails (step 5); this second `_on_exit` sees `_started` false and only marks the backend dead.
- `_fail` (fail-closed) sets `_dead` before it closes Kiro, so `_on_exit` never restarts.

## Milestones

### [x] M1 — Tool processes are the OOM killer's first choice

- **Approach:** add `agent/src/archie_agent/oom.py` with `TOOL_OOM_SCORE_ADJ`,
  `OOM_SCORE_ADJ_PATH`, `shell_prefix` (`{ echo 1000 >/proc/self/oom_score_adj; } 2>/dev/null`, a
  newline, then `<command>`)
  and `argv_wrapper` (`["/bin/sh", "-c", '{ echo 1000 >/proc/self/oom_score_adj; } 2>/dev/null; exec "$@"', "sh", *args]`,
  or `args` unchanged when `shutil.which(args[0])` is None, D15). Test the unwritable case
  by patching `OOM_SCORE_ADJ_PATH` to a path in a missing directory: no stray stderr.
  Use them in `run_shell`, `run_exec` and the exec runner spawn; add `start_new_session=True`
  to the runner spawn (write its failing pgid test first). ⚠️ The prefix must not change exit
  codes or output, and a multi-line command must still work as one script.
- **Test seam:** the `shell` tool, `_subprocess.run_exec`, and `exec.tool.run_exec` with
  `on_start`.
- **Deliverable:** tool processes and their children run with `oom_score_adj` 1000; the exec
  runner has its own process group.
- **Verify:** `env -u ARCHIE_BACKEND scripts/dev test tests/test_oom_tools.py tests/test_exec_tool.py tests/test_exec_tools.py tests/test_runner.py tests/test_kiro_mcp.py`
  passes.
- **Outcome:** `archie_agent/oom.py` holds the prefix and wrapper; `run_shell`, `run_exec`
  and the exec runner use them, and the runner now starts in its own session. The new
  pgid test failed with the old `exec/tool.py` (stashed) and passes with the fix. Verify
  plus `tests/test_code_tool.py`: 162 passed; ruff check and format clean.

### [ ] M2 — OOM kills are named in tool results, Kiro exits and the frame log

- **Approach:** add `MEMORY_EVENTS`, `oom_kill_count()`, `kills_since()` and `tool_warning()` to
  `oom.py`.
  `run_shell` and the exec runner compare counts around the process. In the harness, read the
  baseline right after spawning `kiro-cli` and again at each turn's start and end (D16); `_on_exit` builds `_exit_reason` and records the
  `exit` entry through the new `AcpClient.record`; `_run_turn` uses `_exit_reason` for the
  `turn_error` when its client died. `AcpClient._read_stderr` writes `err` entries (after the
  ignore filter and redaction, both `[INFO]` and other lines). M2's tests assert with "contains",
  so they stay valid when M3 appends "; restarting Kiro".
- **Test seam:** the `shell` tool and `exec.tool.run_exec` with a patched `MEMORY_EVENTS`;
  `KiroHarness` against `tests/fake_kiro.py`, reading the session log and the frame log.
- **Deliverable:** a tool or Kiro exit during an OOM kill says the OOM killer ran; the frame log
  keeps Kiro's stderr and exit.
- **Verify:** `env -u ARCHIE_BACKEND scripts/dev test tests/test_oom_tools.py tests/test_kiro_harness.py`
  passes.
- **Outcome:**

### [ ] M3 — Kiro is restarted and its session reloaded after an unexpected exit

- **Approach:** split `KiroHarness.start` into one-time setup and `_launch`, following the
  existing step order and `remaining(step)` deadline pattern in `start`. Add the restart flow
  in Data flow, the restart limit (`RESTART_LIMIT = 3`, `RESTART_WINDOW_S = 1800`,
  `RESTART_TIMEOUT_S = 60`), the waits in `handle_message`, `switch_model` and
  `run_manual_compaction`, and the resume note. Extend `tests/fake_kiro.py` with
  `session/load`, `FAKE_KIRO_LOAD` and `exit_tool`, and update its docstring; the fake picks
  its scenario from the prompt text's last paragraph, so a prompt with the resume note still
  selects it. ⚠️
  `_agent_problem`'s content-collection check runs only when `_started`, so `_started` must be
  false until `_launch` has set `contentCollection` again. ⚠️ Replace
  `test_kiro_exit_mid_turn_then_dead_notice`, which asserts the old behaviour.
- **Test seam:** `KiroHarness` against `tests/fake_kiro.py`, reading the fake's record, the
  session log and captured `event_bus.emit` calls.
- **Edge cases:** load error → dead with `backend_stopped`; fake exits during load → dead;
  limit reached → dead; `late_builtin` → no restart; `close()` during a restart → no Kiro
  process left (`os.kill(pid, 0)` raises).
- **Deliverable:** a Kiro session survives `kiro-cli` dying and carries on with its
  conversation.
- **Verify:** `env -u ARCHIE_BACKEND scripts/dev test tests/test_kiro_harness.py` passes.
- **Outcome:**

### [ ] M4 — A dead backend reports `error`, and the docs describe the new behaviour

- **Approach:** `status_event` returns `state="error"` when `_dead`; the dead `turn_error`
  notice becomes `<_dead_reason>; start a new session`. Update `docs/kiro.md` (Session
  control's "Kiro exits mid-session", Debugging, Limits, the tool-process note) and
  `docs/architecture.md` (the tracker line listing `session_status` states).
- **Test seam:** `KiroHarness.status_event()` and captured notices.
- **Deliverable:** the TUI shows a dead Kiro session as failed, and the docs match the code.
- **Verify:** `env -u ARCHIE_BACKEND scripts/dev test tests/test_kiro_harness.py` passes;
  `rg -n 'session/load|backend_restarted|backend_stopped|oom_score_adj|"err"|"exit"' docs/kiro.md`
  finds each term; `rg -n 'error' docs/architecture.md` shows the tracker lines mention a
  Kiro backend that can't run.
- **Outcome:**

## Deviations

## Risks

- A tool process that is small but has score 1000 is still killed before a large non-tool
  process; the OOM killer may need several kills to free enough memory. That is the intended
  trade: losing tool calls instead of the session.
- Kiro's `session/load` behaviour comes from one spike on Kiro 0.66.91; a later Kiro could
  change what it restores (the restart verifies the settings, so a change fails closed).
- A cut-off turn may be partly saved in Kiro's session store; the note tells the model to
  check before it continues.
- `switch_model` is awaited inside the WebSocket receive loop (`app.py`), so waiting for a
  restart there (up to 60 s, usually about 1 s) delays that client's next commands, as the
  existing 30 s model-switch request already can.

## Review

- Plan review: one pass, 1 ❌, 5 ⚠️, 6 ℹ️; all accepted and fixed in the plan:
  the shell prefix now groups the write so a failed redirection prints nothing (❌); the
  fake matches the prompt's last paragraph so the resume note doesn't break scenario
  selection; argv spawns of a missing executable aren't wrapped (D15); `_restart` clears
  `config_options` and `switch_model` waits before its model check; `close()` cancels and
  awaits the restart first; OOM wording is factual with a count and a turn-based window
  (D16) and exact texts are in the Requirements; one owner per `backend_stopped`; the status
  during a restart is D14; a `RunnerCrash` AC and wider M4 Verify were added; M2's tests are
  stated to survive M3.
  Second pass: 6 ⚠️/ℹ️ groups, all accepted and fixed: the live notices' texts and an
  idle-kill AC; a `switch_model`-during-restart AC; D15's reason corrected; D17 for an
  interrupt during the wait; `_fail` sets `_dead_reason`; `_oom_baseline` keeps its time; no
  note for a cut-off compaction; the note uses the reason's detail; one name,
  `tool_warning()`; D13 moved into order; a Risk for the inline model-switch wait. No ❌.

## Outcome

PR: pending
CI: pending

## Follow-ups

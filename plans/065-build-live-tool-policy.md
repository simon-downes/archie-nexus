# 065 — Live tool policy reload

Status: done
Branch: build-it/065-live-tool-policy (base: main @ 5cb7363)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/065-live-tool-policy
Updated: 2026-05-16

## Objective

Tool policy (`tools:` in `config.yaml`) is read once when the agent starts, so enabling a
gate such as Notion writes needs a new session. When done, each exec call uses the policy
as it is in `config.yaml` at that moment, so an edit takes effect on the next exec call in
the same session.

## Context

The user's request: "currently tool config is loaded once at session start which is
annoying when i need to do things like enable write access for notion." They also asked
whether re-reading per tool call costs latency, whether caching is worth its complexity,
and whether the existing tool cache could be used.

Codebase facts found at intake:

- `agent/src/archie_agent/app.py:127` builds `policy_snapshot(ToolsConfig(providers=_config.tools))`
  once in `lifespan` and passes it to `AgentHarness(tool_policy=...)`.
- `AgentHarness` stores it as `self._tool_policy` and passes it to `run_exec(policy_snapshot=...)`
  for root exec calls (`harness.py:945`) and to `create_task_tool(tool_policy=...)`, which
  hands it to `ChildDispatch` for child exec calls (`agents.py:207`).
- `run_exec` writes the snapshot to `<run_dir>/context.json`; the runner installs it in a
  contextvar that the Jira, Notion, Google and Slack namespaces read. The runner never reads
  `config.yaml` (decision D8 of plan 038); this build keeps that contract.
- Native tools (`read`, `grep`, `shell`, ...) do not consult tool policy.
- The orchestrator mounts the host `~/.nexus` directory into the container at
  `ARCHIE_HOME_DIR`, so host edits to `config.yaml` are visible to the agent process.
- Measured in this container: `load_nexus_config()` + `policy_snapshot()` on the live
  616-byte config takes about 0.8 ms median (0.9 ms p99, 200 runs). One trivial `run_exec`
  call takes about 190 ms (8 runs), dominated by the runner subprocess start.
- `exec/tools/cache.py` (`ToolCache`) is a TTL-expiry JSON file cache under
  `~/.nexus/cache` for provider metadata (for example Gmail labels).
- `orchestrator/session_index.py` has an mtime+size keyed in-memory cache pattern.
- `persona/skills/google/SKILL.md:29` documents "Policy is snapshotted at session start".

## Baseline

- Check commands: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`
- Setup: `uv sync`
- Setup leftovers: none
- Failing before this work: none (1240 passed; ruff check and format clean)
- Not included: the user's uncommitted changes to `agent/src/archie_agent/exec/tools/notion/{client,normalization,query,reads}.py`,
  `persona/skills/notion/SKILL.md`, `tests/test_notion_{normalization,query,reads}.py`, and
  untracked `persona/skills/aws-agent-access/`, `plans/060-spec-container-aws-profiles.md`,
  `tests/test_notion_client.py`. None touch policy loading.

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Behaviour when `config.yaml` is invalid mid-session | Keep using the last valid policy | Fail closed (error every exec call); fall back to defaults | User: config breaks are usually short-lived and easy to fix; running agents must not stop working | user Q1 |
| D2 | Where reload warnings appear | Live `error_notice` to the session's TUI clients only | Also agent container log; also orchestrator error log | User: MVP is TUI only; may change later | user Q2 |
| D3 | Policy source | Agent re-reads the mounted `config.yaml` before each exec call | Fetch from orchestrator over HTTP; TTL cache; mtime cache | Read costs ~0.8 ms against ~190 ms per exec; HTTP adds coupling and an availability dependency; a TTL delays edits | self, measured |
| D4 | Warning deduplication | Notify on state transitions: first failure, a changed error message, and recovery | Notify on every failing exec call | Every-call notices flood the TUI while the user fixes the file | self (user concern) |
| D5 | Runner contract | Unchanged: the agent writes the snapshot to `context.json`; the runner never reads `config.yaml` | Runner reads config | Keeps plan 038 D8's boundary; only the snapshot's freshness changes | self |
| D6 | Missing `config.yaml` | Treated as valid and yields defaults (reads on, writes off) | Treat as error | Matches `load_nexus_config()` startup behaviour; the result fails closed for writes | self |

## Requirements

- R1: Each root exec call MUST use the policy from `config.yaml` as read at call time.
  - AC: enabling `notion.write` in the file makes the next root exec call see `write.enabled: true` without restarting.
- R2: Each subagent exec call MUST use the same live policy source.
  - AC: a `ChildDispatch` exec call sees a policy change made after the child started.
- R3: If the file cannot be read, parsed or validated, the exec call MUST proceed with the last valid policy.
  - AC: the exec call runs; its `context.json` holds the last valid snapshot.
- R4: The agent MUST emit one live `error_notice` (kind `tool_policy_reload_failed`) on the first failure, and again only when the error message changes.
  - AC: three exec calls against the same invalid file produce one notice.
- R5: The agent MUST emit one live `error_notice` (kind `tool_policy_reloaded`) when a reload succeeds after a failure.
- R6: Notices MUST NOT be persisted, MUST NOT end the turn in the TUI, and MUST NOT be logged by the agent (D2).
  - AC: the kinds are not in the TUI's end-turn set; `ErrorNotice` is already live-only.
- R7: Docs that say policy changes need a new session MUST be updated.

## Design

- New module `agent/src/archie_agent/tool_policy_live.py` with `LiveToolPolicy`:
  - constructed with the startup snapshot and a `loader` callable (default: `load_nexus_config()` → `policy_snapshot(ToolsConfig(providers=config.tools))`);
  - `refresh() -> tuple[dict, ErrorNotice | None]` is synchronous: on success it stores and returns the new snapshot, plus a recovery notice if the previous state was failed; on any exception it returns the last valid snapshot, plus a failure notice if the error message differs from the last reported one;
  - `LiveToolPolicy.static(snapshot)` has no loader, for tests and callers that pass a plain dict.
- `AgentHarness(tool_policy=...)` accepts `dict | LiveToolPolicy | None`; a dict is wrapped with `static`. A new `async _current_tool_policy()` calls `refresh()`, emits any notice through the event bus (best effort), and returns the snapshot. Root exec uses it; `create_task_tool(tool_policy=...)` receives the coroutine function, and `ChildDispatch` awaits it before each exec call.
- `app.lifespan` builds `LiveToolPolicy(startup_snapshot)` with the default loader.
- No change to `run_exec`, the runner, provider policy modules, or the TUI.

## Milestones

- M1: `LiveToolPolicy` with unit tests (valid reload, invalid fallback, dedupe, recovery, missing file).
- M2: wire into harness, task tool, `ChildDispatch`, and `app.py`; tests for root and child exec seeing a live change and for one notice across repeated failures.
- M3: update `persona/skills/google/SKILL.md`; full checks.

## Change log

- M1 done: `LiveToolPolicy` and 9 unit tests.
- M2 done: harness, task tool, `ChildDispatch` and `app.py` wired; 6 integration tests run real exec subprocesses.
- M3 done: Google skill, `docs/config.md`, `docs/event-spec.md`, the `tool_policy.py` docstring, and supersede notes in plan 038.

## Risks

- A TUI client that connects after a failure notice does not see it (notices are live-only and deduplicated per agent process). Accepted under D2.
- An editor that truncates before writing can expose an empty file for a moment; for one exec call that yields defaults (reads on, writes off). Fails closed for writes; accepted, same as D6.
- `refresh()` reads the file on the event loop (~1 ms). Accepted against ~190 ms per exec call.

## Review

Independent review: no blocking issues. Fixed: plan 038 still said a new session was needed (S1); a misnamed test, missing harness→task-tool wiring test and missing TUI no-end-turn test (S2); doc wording, docstring, `docs/config.md`, extra dedupe tests (nits). Not taken: moving the read to a thread (needs a lock; not worth it at ~1 ms), the zero-byte-file guard, and the dead `ARCHIE_TOOL_POLICY` env fallback in `jira/policy.py`.

## Outcome

Checks: `uv run pytest` 1256 passed (baseline 1240); `ruff check` and `ruff format --check` clean.

PR: pending

## Follow-ups

- Send reload warnings to the agent log and/or orchestrator error log (user deferred, D2).
- Remove the unused `ARCHIE_TOOL_POLICY` env fallback in `agent/src/archie_agent/exec/tools/jira/policy.py`.

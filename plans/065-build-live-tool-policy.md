# 065 — Live tool policy reload

Status: interviewing
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

## Requirements

## Design

## Milestones

## Change log

## Risks

## Review

## Outcome

PR: pending

## Follow-ups

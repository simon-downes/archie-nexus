# 091 — Kiro /compact and question tool

Status: planning
Branch: build-it/091-kiro-compact-question (base: main @ 8cdad07)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/091-kiro-compact-question

## Objective

Kiro-backed sessions answer `/compact` with "not available" and have no `question` tool, so
the agent can't ask the user a structured question and the user can't shrink the context on
demand. Wire `/compact` to Kiro's `_kiro/session/compact` and serve the existing `question`
tool to the Kiro root agent over MCP, so both behave in the TUI as they do on native
sessions.

## Context

The user asked to "improve the kiro harness element of archie. specifically to add support
for /compact and to implement the question tool". Both were deferred follow-ups: plan 077 D21
(`/compact` answered with `manual_compaction_failed`) and plan 079 ("Kiro support for
`question`"). On 2026-11-10 the user reported "the question tool doesn't seem to be working
in kiro" (session archie-01m73gq4c8).

A live spike against kiro-cli 3.1.0 on 2026-11-15 settled Kiro's behaviour (brain entry
`projects/archie/kiro-acp-backend-research.md`, section "Spike 2"):

- `_kiro/session/compact {sessionId}` returns `{success: true}` after 7–10 s between turns,
  at once with no history, and `{success: false}` at once while a turn runs. Before the
  result it sends one `session_info_update` with `_meta.kiro.kind: "summarization_completed"`,
  `summarization: {status: "success", summary: {conversationSummary, truncated}}`. Manual
  compaction sends no `running` frame, no `context_usage` afterwards, and no credits
  (`session_usage` didn't move).
- Automatic compaction mid-turn sends `summarization_started` (`status: "running"`), then
  `summarization_completed`, then `context_usage` frames with a placeholder
  `usagePercentage: 20`, then real figures. Its credits are in the turn's `turn_completion`.
  `session/new` `_meta.kiro.settings.compaction = {enabled: true, autoCompactionThreshold: N}`
  lowers the threshold (useful for tests only).
- A blocking MCP `question` call works: the ACP `tool_call` has title `@archie/question` and
  the arguments as `rawInput`. `session/cancel` ends the turn at once (tool call `failed`),
  but Kiro doesn't cancel the MCP request, so the pending question stays until the harness
  clears it. The MCP handler never sees ACP's `toolCallId`.
- An explicit `tools: ["@archie/shell", …]` list in `archie-worker.json` hides a tool from the
  worker subagent.

Codebase facts:

- `KiroHarness` (`agent/src/archie_agent/kiro/harness.py`): `run_manual_compaction` emits
  the D21 notice; `pending_question` is always `None`; `answer_question` always rejects;
  `status_event` reports `working`/`idle` only. The MCP registry is a plain
  `create_registry()` (`kiro/harness.py:152`).
- Native flow to mirror: `AgentHarness.run_manual_compaction` and `_compact`
  (`agent/src/archie_agent/harness.py:432-597`), `QuestionBroker` and `create_question_tool`
  (`agent/src/archie_agent/question.py`), `status_event`/`answer_question`
  (`harness.py:289-328`).
- `app.py:400-427` routes `/compact` (`is_compact_command`) to `run_manual_compaction` after
  `try_begin_turn`, and `AnswerCommand` to `answer_question`; nothing there is
  backend-specific.
- Agent files: `kiro/agent_files.py` (`root_agent_config`, `worker_agent_config`). MCP
  server: `kiro/mcp_server.py` (`_EXCLUDED = {"skill", "task"}`). Translator:
  `kiro/translate.py` (`TurnTranslator.feed`, maps `@archie/<name>` to canonical tools).
- Compaction display assumes tokens: TUI `CompactionEntry`
  (`cli/src/archie_cli/tui/conversation.py:463`), `_complete_compaction`
  (`cli/src/archie_cli/tui/transcript_view.py:353`), web `compaction_text`
  (`orchestrator/src/archie_orchestrator/transcript.py:163`). The shared
  `CompactionNode` (`shared/src/archie_shared/transcript.py:135`) is built from
  `CompactionStart`, which carries `model_key`.
- Test doubles: `tests/fake_kiro.py` (scripted ACP agent), `tests/test_kiro_harness.py`,
  `tests/test_kiro_translate.py`, `tests/test_kiro_mcp.py`, `tests/test_question_tool.py`.

## Baseline

- Check commands: `env -u ARCHIE_BACKEND scripts/dev test`, `scripts/dev ruff check .`,
  `scripts/dev ruff format --check .`
- Setup: `scripts/dev sync`
- Setup leftovers: none (`.venv-archie` is gitignored)
- Failing before this work: `ruff format --check` wants to reformat
  `persona/skills/python/assets/output.py`. With the session's `ARCHIE_BACKEND=kiro` in the
  environment, 14 tests fail and 9 error in `tests/test_ws_integration.py` and
  `tests/test_question_tool.py` (the app lifespan starts the Kiro backend); with
  `env -u ARCHIE_BACKEND` those pass. Tests are run with `env -u ARCHIE_BACKEND`.
- Not included: `.DS_Store`, `plans/060-spec-container-aws-profiles.md`,
  `plans/070-build-nexus-plugins.md` (untracked in the user's checkout, unrelated)

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
  `env -u ARCHIE_BACKEND` the full suite passes (5531 passed). Tests are run with
  `env -u ARCHIE_BACKEND`.
- Not included: `.DS_Store`, `plans/060-spec-container-aws-profiles.md`,
  `plans/070-build-nexus-plugins.md` (untracked in the user's checkout, unrelated)

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Automatic compactions | Map Kiro's mid-turn `summarization_*` frames to `compaction_start`/`compaction_complete` (reason `threshold`) and drop the placeholder 20 % context figure | Manual `/compact` only | Without it the context drops and filler replies appear with nothing explaining why | user Q1 |
| D2 | Event shape for Kiro compactions | Reuse `compaction_start`/`compaction_complete` with `tokens_before=0`, `tokens_after=null`, `cost_saved_usd=0`, `model_key=kiro/<id>` on the start; no new fields | New optional `context_pct_*` fields on both events; an `error_notice`-only result | No wire change (both events are `forbid_unknown_fields`, so new fields break older CLIs); metrics, replay and the TUI end-of-turn logic already handle these events; Kiro reports no tokens, and no context figure after a manual compaction | self |
| D3 | Display of Kiro compaction rows | `CompactionNode.model_key` (new, from `compaction_start`); TUI and web rows for a `kiro/` model show no token or cost figures: `Compaction manual (Kiro, 8204ms)`, failure `Compaction manual (Kiro, 8204ms) Failed: <error>`, pending `Compaction manual (Kiro)` | Showing `0 → ?` tokens | Zero tokens is misleading; `CompactionNode` is not a wire type | self |
| D4 | Manual compaction flow | `run_manual_compaction`: dead backend → `manual_compaction_failed` notice with `DEAD_MESSAGE`; no turn yet → `manual_compaction_failed` "Nothing to compact" (native wording); else `working` status, `compaction_start` (reason `manual`, `turn=session.turn_index`, `iteration=0`), `_kiro/session/compact` with a 120 s timeout, then `compaction_complete`: `success` with the summary from the `summarization_completed` frame (or `null` if none came); `failure` with "Kiro did not compact: a turn or compaction is in progress" for `{success: false}`; `failure` with the error text for a JSON-RPC error, timeout or Kiro exit. Always releases the turn flag and sends a status | A notice per outcome without events | Mirrors `AgentHarness.run_manual_compaction` (`harness.py:563`), so the TUI's manual end-of-turn path works unchanged | self |
| D5 | Interrupt during manual compaction | Ignored: no `session/cancel` while compacting; the request finishes or times out | Sending `session/cancel` | Kiro's compact has no cancel and its own summary call is capped at 60 s; a cancel with no prompt active has unknown effect | self |
| D6 | Where summarization frames are handled | `translate.py` gets `summarization(update) -> (kind, summary, reason) \| None`; the harness captures the summary for manual compaction when no translator is active; `TurnTranslator.feed` maps them during a turn | One owner for both | During a turn the translator owns iteration and request ids; between turns there is no translator | self |
| D7 | Automatic compaction mapping | `summarization_started` → flush segment text, `compaction_start` (reason `threshold`, scope from `agentSubtaskId`, current iteration or 0, new `request_id`); `summarization_completed` → `compaction_complete` `success` with the summary and `duration_ms` from the start (a start is synthesised with duration 0 when none came); `summarization_failed` → `failure` (reason `error`) or `interrupted` (reason `canceled`); an open compaction at `finish()` → `interrupted`. The next text or tool call opens a new iteration | Mapping only root compactions | The events carry `scope`; the transcript keys compactions by scope | self |
| D8 | Placeholder context figure | `RootContext` in `translate.py`: after a root `summarization_completed`, root `context_usage` frames with exactly 20 are dropped until a frame with another value arrives; the harness (live `session_status`) and each `TurnTranslator` (turn stats) use it | Dropping the next N frames; no filter | The spike showed exactly 20 in a pair right after the summary, then real figures; a real 20 is repeated by later frames | self |
| D9 | Question tool on Kiro | `KiroHarness` owns a `QuestionBroker(self._emit_status)` and adds the question tool to its MCP registry, wrapped so each call's id is a new ULID (any `_tool_use_id` argument is dropped); `pending_question`, `status_event` (`blocked` plus the question) and `answer_question` match `AgentHarness` | MCP elicitation (`_kiro/mcp/elicitation`); correlating with ACP `toolCallId` | The MCP handler never sees `toolCallId`, and the TUI treats `question.id` as opaque; elicitation adds a round trip through Kiro for no gain | self |
| D10 | Clearing a question Kiro abandons | New `QuestionBroker.cancel()` fails the pending answer with `QuestionError("question_cancelled", "the turn ended before an answer")`, so the handler returns `Error: …` and the broker clears and publishes status; `KiroHarness.interrupt` and the end of every `handle_message` call it | Cancelling the MCP handler task | Kiro doesn't cancel the MCP request on `session/cancel` (spike); a returned error ends the MCP call cleanly | self |
| D11 | Keeping `question` from the worker | `worker_agent_config(mcp_url, tools)` lists `@archie/<name>` for every served tool except `question`; the root keeps `@archie` | Removing it at the MCP server per caller | Both agents share one MCP server; the spike showed the explicit list hides the tool | self |
| D12 | TUI palette | "Compact context" is listed for Kiro sessions and `action_compact` sends `/compact` for them | Keeping it native-only | The agent now supports it; typed `/compact` already reaches Kiro | self |

## Requirements

- MUST compact a Kiro session on `/compact` through `_kiro/session/compact`.
  - AC (fake Kiro): after one turn, `run_manual_compaction` sends `_kiro/session/compact
    {sessionId}` and the log gains `compaction_start` (reason `manual`, `tokens_before` 0,
    `model_key` `kiro/gpt-5.6-luna`) then `compaction_complete` (`success`, the fake's summary);
    the turn flag is released and the last `session_status` is `idle`.
- MUST report failures without leaving the turn active.
  - AC: `{success: false}` → `compaction_complete` `failure` with the D4 message; a JSON-RPC
    error → `failure` with the error text; no turn yet → `manual_compaction_failed` "Nothing
    to compact" and no request sent; dead backend → `manual_compaction_failed`. In each case
    `turn_active` is false afterwards.
- MUST NOT send `session/cancel` when interrupted during a manual compaction.
  - AC: `interrupt()` during a slow fake compaction sends no `session/cancel`, and the
    compaction completes.
- MUST render Kiro compaction rows without token or cost figures in the TUI and the web
  viewer (D3).
  - AC: TUI conversation host shows `● Compaction manual (Kiro, 8204ms)`; web
    `compaction_text` returns no "tokens" text for a `kiro/` node; native rows are unchanged.
- MUST list "Compact context" in the palette for running Kiro sessions.
  - AC: `test_palette_lists_for_native_kiro_ended_and_no_session` lists it for `ws-b`.
- MUST map Kiro's automatic compaction frames per D7 and drop the placeholder per D8.
  - AC (translator): started/completed frames yield `compaction_start` then
    `compaction_complete` `threshold` `success` with the summary; text after it opens a new
    iteration; a failed frame yields `failure`; a turn that ends mid-compaction yields
    `interrupted`; context frames of 20 right after the summary don't change
    `context_pct`/`context_pct_max`, and the harness sends no `session_status` with 20.
- MUST give the Kiro root agent the `question` tool and keep it from `archie-worker`.
  - AC: the harness MCP server lists `question`; `archie-worker.json` `tools` lists each
    served tool except `@archie/question`; `archie.json` keeps `@archie`.
- MUST block a Kiro `question` call until answered and expose it as live state.
  - AC (fake Kiro): during the call `pending_question` is set and `status_event()` is
    `blocked` with it; `answer_question(option=0)` makes the tool result
    `User selected option 1: <label>` and the turn completes; a wrong id →
    `question_not_pending`.
- MUST clear a pending Kiro question when the turn is interrupted or ends.
  - AC: `interrupt()` while waiting → `turn_interrupted`, `pending_question is None`, a
    `session_status` with `question=null`, and the MCP call returns an `Error:` result; the
    next turn runs.
- MUST update `docs/kiro.md` and the README palette line for both features.
  - AC: `docs/kiro.md` no longer says `/compact` is not available or that `question` is
    excluded, and describes the event mapping for compactions.

Out of scope: per-request accounting of compaction credits (manual compaction reported
none; automatic compaction's credits are already in the turn's `turn_completion`); a
context figure after a manual compaction (Kiro sends none until the next turn); MCP waits
over the 600 s `requestTimeout` (the user will observe them in real sessions); cancelling
a Kiro compaction; questions from the worker subagent.

## Design

### Overview

The Kiro harness gains the two native behaviours by reusing their contracts: `/compact`
calls Kiro's compact extension and reports it with the existing compaction events, the
translator maps Kiro's automatic compactions to the same events, and the existing
`question` tool and broker are served over MCP to the root agent only, with the harness
clearing questions that Kiro abandons.

### Code structure

- `agent/src/archie_agent/kiro/translate.py`: `summarization()`, `RootContext` (D8),
  compaction mapping in `TurnTranslator.feed` and `finish` (D7).
- `agent/src/archie_agent/kiro/harness.py`: `run_manual_compaction` (D4), interrupt guard
  (D5), `QuestionBroker` wiring (D9, D10), status with `blocked`, `RootContext` for live
  status, worker tool list for `agent_files`.
- `agent/src/archie_agent/kiro/agent_files.py`: `worker_agent_config(mcp_url, tools)` and
  `write_agent_files(..., worker_tools=...)` (D11).
- `agent/src/archie_agent/kiro/mcp_server.py`: `tool_names()` (served names, excluding
  `skill`/`task`).
- `agent/src/archie_agent/question.py`: `QuestionBroker.cancel()` (D10).
- `shared/src/archie_shared/transcript.py`: `CompactionNode.model_key: str = ""`, set from
  `CompactionStart.model_key`.
- `cli/src/archie_cli/tui/conversation.py` `CompactionEntry` (Kiro variant),
  `cli/src/archie_cli/tui/transcript_view.py` (`widget_for`, `_complete_compaction`),
  `orchestrator/src/archie_orchestrator/transcript.py` `compaction_text` (D3).
- `cli/src/archie_cli/tui/commands.py` and `cli/src/archie_cli/tui/app.py`
  `action_compact` (D12).
- `tests/fake_kiro.py`: `_kiro/session/compact` (scenario env `FAKE_KIRO_COMPACT`:
  `ok` default, `busy`, `error`, `slow`); prompts `autocompact`, `autocompact_fail`,
  `question`.
- Docs: `docs/kiro.md`, `README.md` palette line, `docs/event-spec.md` note on Kiro
  compaction fields.

### Data flow

- Manual: `/compact` → `app.py` `try_begin_turn` → `run_manual_compaction` →
  `compaction_start` → `_kiro/session/compact` → Kiro sends `summarization_completed`
  (captured by `_on_notification` while `self._compacting`) → result → `compaction_complete`
  → turn flag released → `session_status idle`.
- Question: model calls `archie::question` → MCP handler → `broker.ask` → `_emit_status`
  (`blocked`) → TUI panel → `answer` command → `answer_question` → result text → Kiro →
  ACP `tool_call_update` → translator `tool_result`. Interrupt → `broker.cancel()` +
  `session/cancel` → Kiro `failed` update and `cancelled` stop reason → MCP call returns
  `Error: …`, which Kiro discards.

### Error handling

- Compact request error, timeout or Kiro exit → `compaction_complete` `failure` (D4).
- A `LogAppendError` while compacting → `storage_error` notice, as in `handle_message`.
- Status emission failures are logged and ignored (existing `_emit_status`).

## Milestones

### [ ] M1 — Manual /compact on Kiro

- **Approach:** D2–D6 and D12. Fake Kiro handles `_kiro/session/compact` (records the
  request; `ok` sends a `summarization_completed` frame then `{success: true}`; `busy` →
  `{success: false}`; `error` → JSON-RPC error; `slow` → waits 1 s first). Harness flow per
  D4 following `AgentHarness.run_manual_compaction`. `CompactionNode.model_key` and the
  Kiro row text per D3. Palette per D12. Replace `test_compact_is_not_available`. Docs:
  `docs/kiro.md` session control `/compact` and limits, `README.md` palette line,
  `docs/event-spec.md` note that Kiro compactions carry `tokens_before` 0 and
  `tokens_after` null.
- **Test seam:** `KiroHarness` against `tests/fake_kiro.py` (`tests/test_kiro_harness.py`);
  `_ConversationHost` in `tests/test_tui_canonical.py`; `compaction_text` in
  `tests/test_web_session_detail.py` (or the closest existing web transcript test);
  palette in `tests/test_tui_app.py`.
- **Edge cases:** no turn yet → notice, no request; `busy` → failure; `error` → failure;
  dead backend → notice; interrupt during `slow` → no `session/cancel`.
- **Deliverable:** `/compact` on a Kiro session shows a Kiro compaction row and ends the
  turn.
- **Verify:** `env -u ARCHIE_BACKEND scripts/dev test tests/test_kiro_harness.py
  tests/test_tui_canonical.py tests/test_tui_app.py tests/test_web_session_detail.py -q`
  passes.
- **Outcome:**

### [ ] M2 — Automatic compaction mapping

- **Approach:** D7 and D8 in `translate.py`; the harness uses `RootContext` for live
  status. Fake Kiro prompt `autocompact` sends started, completed (summary), `context(20)`
  twice, `context(4.2)`, text, completion; `autocompact_fail` sends started then
  `summarization_failed`. Docs: `docs/kiro.md` event-mapping rows for `summarization_*`
  and the placeholder.
- **Test seam:** `TurnTranslator.feed`/`finish` in `tests/test_kiro_translate.py`;
  harness against the fake in `tests/test_kiro_harness.py`.
- **Deliverable:** an automatic compaction appears as a `threshold` compaction row, and the
  status bar never shows the placeholder 20 %.
- **Verify:** `env -u ARCHIE_BACKEND scripts/dev test tests/test_kiro_translate.py
  tests/test_kiro_harness.py -q` passes.
- **Outcome:**

### [ ] M3 — Question tool on Kiro

- **Approach:** D9–D11. `QuestionBroker.cancel()`; harness registry =
  `create_registry()` plus the wrapped question tool; `ArchieMcpServer.tool_names()`;
  worker tools list via `agent_files`. Fake Kiro prompt `question` calls the MCP
  `question` tool and waits for its result (on `session/cancel` it sends the `failed`
  update and a `cancelled` result without waiting, like real Kiro). Update
  `test_kiro_status_and_answer_have_no_question` and `test_kiro_mcp.py`'s "question not
  listed" assertion (its registry has no question tool; keep that assertion and add the
  harness-level one). Docs: `docs/kiro.md` tools and subagents lines.
- **Test seam:** `KiroHarness` against the fake (`tests/test_kiro_harness.py`);
  `QuestionBroker` unit test in `tests/test_question_tool.py`; agent JSON in
  `tests/test_kiro_harness.py`.
- **Edge cases:** wrong id → `question_not_pending`; interrupt while waiting → cleared,
  `Error:` result, next turn runs; Kiro exits while waiting → cleared by the turn end.
- **Deliverable:** a Kiro agent can ask a question, the TUI panel shows it, and the answer
  reaches the model.
- **Verify:** `env -u ARCHIE_BACKEND scripts/dev test tests/test_kiro_harness.py
  tests/test_question_tool.py tests/test_kiro_mcp.py -q` passes, and
  `rg -n "not available|or \`question\`|Archie sessions only" docs/kiro.md README.md` finds
  nothing.
- **Outcome:**

## Deviations

## Risks

- Kiro's frame shapes come from one spike on kiro-cli 3.1.0; a later Kiro may rename the
  `summarization_*` kinds. Unknown kinds are ignored, so the failure mode is a missing row.
- MCP calls that wait longer than the 600 s `requestTimeout` are unverified (user's call to
  observe in real sessions).
- Metrics rows for Kiro compactions have `tokens_before` 0, which insights averages may
  include.

## Review

## Outcome

PR: pending
CI: pending

## Follow-ups

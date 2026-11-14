# 089 — Kiro context usage

Status: planning
Branch: build-it/089-kiro-context-usage (base: feat/default-tui-entrypoint @ 68ed85c)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/089-kiro-context-usage

## Objective

Kiro sessions show no context-window usage: the TUI status bar reads 0 %, and the session
log, `archie.db`, insights and `archie stats` record 0 context for every Kiro turn. Kiro
already reports `usagePercentage` in `session_info_update` frames (`kind: context_usage`),
and Archie ignores them. When done, the TUI shows Kiro's live context percentage, each Kiro
turn's `llm_request` persists it, and insights and `archie stats` show it.

## Context

The user worried that long Kiro sessions grow context and cost more credits with no
feedback in the TUI. Investigation of `~/.nexus/logs/kiro/*.acp.jsonl` found:

- `session_info_update` frames with `_meta.kiro.kind == "context_usage"` carry
  `usagePercentage` (float, e.g. 42.1972), sometimes `summarizationThreshold` (80) and a
  `breakdown`. They arrive as a pair (percentage-only, then with breakdown) roughly as each
  root model request finishes, and between turns (one right after `session/new`). None
  carried `agentSubtaskId`. Frames carry no request ID.
- The breakdown percentages are consistent with `usagePercentage`; its token counts are not
  (they imply windows from 0 to 1.1M), so no token count can be derived.
- Across 20 logs, 180 of 196 root turns had as many distinct percentages as
  `turn_completion.requestIds`, but only 1,399 of 3,574 request IDs (39 %) are matched; the
  16 mismatched turns hold 72 % of the request IDs, and the cause is unknown.

The user asked a council (app developer, data analyst, skeptic, pragmatist) for the
approach. Consensus (4/4): keep one `llm_request` per Kiro turn (077 D2); carry the live
percentage on `session_status`; persist the turn's percentage on its `llm_request`; store
percentages, not tokens derived from an assumed window; record per-turn frame and request
counts so per-request records can be re-evaluated later; feed live and persisted values
from the same frames. 3/1 for also storing the turn's maximum. The user then asked to build
it, noting the metrics index is `archie.db` (the `metrics_*` tables), not `metrics.db`.

Codebase facts:
- `agent/src/archie_agent/kiro/harness.py` `_on_notification` receives every ACP
  notification and feeds `session/update` to the turn's `TurnTranslator`
  (`agent/src/archie_agent/kiro/translate.py`), which only reads `turn_completion` from
  `session_info_update`. `status_event()` builds `SessionStatus`; `_emit_status()` sends it.
- `shared/src/archie_shared/events.py`: `LLMRequest` (Kiro-only `credits` uses
  `msgspec.UNSET` so native lines don't encode it, 077 D27), `SessionStatus` (live-only).
  Every struct uses `forbid_unknown_fields`. `shared/src/archie_shared/protocol.py`
  `PROTOCOL_VERSION = 4`.
- TUI: `cli/src/archie_cli/tui/session.py` `Ledger.context_tokens/context_pct`,
  `_accumulate_ledger` (root `llm_request` sets context via `estimate_context_pct`, which
  returns 0 for `kiro/*` keys: they're not in the catalog), `SessionStatus` handling; live
  frames are buffered during replay and applied after it. `pane.py on_status` copies the
  ledger to `status.py StatusBar` (`Ctx: 40.0k (20%)`).
- `orchestrator/src/archie_orchestrator/metrics.py` (`METRICS_VERSION = 5`; a version
  change drops and rebuilds `metrics_*` from the logs), `insights.py session_detail`
  (turn max context, per-request context), `templates/insights/session.html` and
  `static/insights/insights.js` (context chart), `cli/src/archie_cli/stats.py render_session`.
- Tests: `tests/fake_kiro.py` (scripted ACP agent), `tests/test_kiro_harness.py`,
  `tests/test_kiro_translate.py`, TUI controller tests in `tests/test_tui_canonical.py`.

## Baseline

- Check commands: `scripts/dev test`, `scripts/dev ruff check .`, `scripts/dev ruff format --check <files>`
- Setup: `scripts/dev sync`
- Setup leftovers: none (`git status --short` clean after sync)
- Failing before this work: 14 failed, 9 errors, all in `tests/test_question_tool.py` and
  `tests/test_ws_integration.py`: the agent app starts its Kiro lifespan in this container
  ("No Kiro API key") and exits. 5409 passed. `ruff check .` passes.
- Not included: `.DS_Store`, `plans/060-spec-container-aws-profiles.md`,
  `plans/070-build-nexus-plugins.md` (untracked in the user's checkout, unrelated)

# 089 — Kiro context usage

Status: building
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

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Granularity of Kiro `llm_request` | keep one per turn (077 D2) | one per inferred model request (hold one back for credits) | frames match request IDs for only 39 % of requests, failing on the long turns; per-request rows would be invented | council (user-requested), 4/4 |
| D2 | Live carrier | `SessionStatus.context_pct: float \| msgspec.UNSET`, omitted until Kiro reports a value | a new live-only `context_usage` event; nothing live (turn end only); `float \| None = None`, which would encode `null` on every native status frame | `session_status` is the current-state snapshot and is sent on every connect, so a reattached TUI gets the value at once; one-hour turns need mid-turn updates; UNSET keeps native status frames byte-identical, so only Kiro frames change shape | user, council |
| D3 | Persisted fields on `LLMRequest` | `context_pct` (turn's last `usagePercentage`), `context_pct_max`, `context_samples` (distinct percentages in the turn), `kiro_requests` (`len(turn_completion.requestIds)`); all `float/int \| msgspec.UNSET`, omitted when unset | a persisted event per frame; tokens derived from a 1M window; `context_pct_after` naming | council 4/4 on last + counts, 3/1 on max; one name (`context_pct`) is used on the status, the request, the ledger and `archie.db`, and the docs state it is Kiro's post-response figure (deviates from the council's suggested `context_pct_after`) | council, self |
| D4 | Encoding for native requests | `UNSET`, not encoded (077 D27 pattern) | always encode `null` | native `llm_request` lines stay byte-identical | self |
| D5 | When the turn fields are set | `context_pct`/`context_pct_max`/`context_samples` only when the turn saw at least one root context frame; `kiro_requests` only when `turn_completion` has a `requestIds` list | carry the previous turn's value | an unset field means "Kiro didn't report it", and readers keep the previous value | self |
| D6 | Which frames count | `session_info_update`, `_meta.kiro.kind == "context_usage"`, numeric `usagePercentage`, no `agentSubtaskId` | include subagent frames | the root context is what compacts; none seen with a subagent ID, so this is defensive | self |
| D7 | When the agent sends `session_status` | as today, plus whenever a counted frame changes the harness's latest value, after startup and while the backend is alive | every frame; throttled | frames come in identical pairs, ~1 per 20 s at most; dedupe removes the duplicate | self |
| D8 | TUI rule for context | a `session_status` with `context_pct` not None sets `ledger.context_pct`; a root `llm_request` with `context_pct` set sets it; a Kiro root `llm_request` without it leaves ledger context unchanged; native unchanged | let Kiro requests reset context to 0 | live frames apply after replay (buffered), so the newest value wins; without the rule each turn end would flash 0 % | self |
| D9 | Status bar for Kiro | `Ctx: 42%` (percentage only), set by a `context_pct_only` flag the controller derives from a `kiro/` model key | `Ctx: 0 (42%)` | Kiro reports no tokens; showing 0 is wrong | self |
| D10 | `archie.db` | `metrics_requests` gains `context_pct REAL`, `context_pct_max REAL`, `context_samples INTEGER`, `kiro_requests INTEGER` (NULL for native); `METRICS_VERSION` 6 | keep the counts in the logs only | the version bump already rebuilds the tables; the counts become a SQL query for re-evaluating D1 | self |
| D11 | Insights and `archie stats` | turn rows gain `context_pct` = root `MAX(context_pct_max)`; request rows gain `context_pct`; the turn table's context cell and the session chart show the percentage when it is not null. The chart plots each root request's `context_pct` (the post-turn value; the table shows the turn's max) with the title "Context % per main-agent turn" when any root request has one; requests without it (older Kiro turns) are gaps | a shared Python formatting helper | the readers take different inputs (Jinja rows, Rich tables) and formats; the one rule is "`context_pct` not null → show the percentage", tested in each reader (deviates from the council's suggested helper) | self |
| D12 | Protocol version | `PROTOCOL_VERSION` 5 | no bump | an older CLI, orchestrator tracker (`tracker.py` `_on_frame`) or live viewer (`web.py`) rejects a Kiro status frame carrying `context_pct` and skips it, losing state, model and question; the TUI's handshake warning names the mismatch. Agent, orchestrator and CLI deploy together on this single host | self |
| D13 | Native sessions | `SessionStatus.context_pct` stays unset; context keeps coming from `llm_request` | fill it from the native harness | native already updates per model request and replays it; nothing gained | self |

## Requirements

- MUST include Kiro's latest root `usagePercentage` in every Kiro `session_status`
  (`context_pct`), unset (not encoded) before the first frame and for native sessions.
  - AC: harness test: after a fake-Kiro turn that sends 2.5 then 3.1 (each twice), the last
    captured `SessionStatus.context_pct == 3.1` and `harness.status_event().context_pct == 3.1`.
- MUST send a `session_status` when a counted frame changes the value, and not for a repeat.
  - AC: in that test the captured statuses that carry `context_pct` have values
    `[2.5, 3.1, 3.1]`: one per change plus the existing turn-end status; no status for the
    duplicate frames. `encode_event` of a native `SessionStatus` has no `context_pct` key.
- MUST ignore context frames that carry `agentSubtaskId`.
  - AC: translator unit test (a subagent frame leaves the turn's fields unset) and harness
    unit test calling `harness._on_notification(...)` directly with a subagent frame (no
    status emitted, `status_event().context_pct` unchanged).
- MUST write `context_pct`, `context_pct_max`, `context_samples` and `kiro_requests` on a
  Kiro turn's `llm_request` per D3/D5, and omit all four from native `llm_request` lines.
  - AC: translator test with frames 2.5, 2.5, 4.0, 3.0 and `requestIds` of length 3 →
    `(3.0, 4.0, 3, 3)`; a turn with no frames and no `requestIds` → all four `UNSET`;
    `encode_event` of a native `LLMRequest` contains none of the four keys; a Kiro line
    round-trips through `decode_event(..., persisted=True)`.
- MUST show the Kiro context percentage in the TUI status bar as `Ctx: <n>%`, updated from
  `session_status` live and from `llm_request` on replay, per D8/D9.
  - AC: controller test: a Kiro `SessionStatus(context_pct=42.2)` sets
    `ledger.context_pct == 42.2` without resetting the streamed output estimate; a later Kiro
    `llm_request` without `context_pct` leaves it at 42.2; replaying a Kiro `llm_request(context_pct=55.0)` sets 55.0. StatusBar test:
    `context_pct_only=True, context_pct=42.2` renders `Ctx: 42%` and no token count.
- MUST keep native TUI context behaviour unchanged.
  - AC: existing `tests/test_tui_canonical.py` context tests pass unchanged; a native
    `SessionStatus` (no `context_pct`) leaves the ledger's context unchanged.
- MUST index the four fields into `archie.db` `metrics_requests` (NULL for native) at
  `METRICS_VERSION` 6.
  - AC: metrics test: a Kiro request line yields the four values; a native line yields NULLs;
    a v5 `archie.db` is rebuilt to v6.
- MUST show the Kiro percentage in insights' session page and `archie stats session` per D11.
  - AC: insights test: Kiro session turn row `context_pct == 4.0`, request row
    `context_pct == 3.0`, native rows `context_pct is None`; the rendered session page shows
    `4.0%` in the turn table and the chart title "Context % per main-agent turn"; stats test:
    the Turns table shows `4.0%` for the Kiro turn and the token count for a native turn.
- MUST bump `PROTOCOL_VERSION` to 5 and document the new fields and their compatibility in
  `docs/event-spec.md`, `docs/kiro.md` and `docs/architecture.md`.
  - AC: `rg -n "context_pct" docs/event-spec.md docs/kiro.md docs/architecture.md` finds each
    file; `PROTOCOL_VERSION == 5`.

Out of scope: per-request Kiro `llm_request` (D1); the context breakdown or a `/context`
view; `/compact` via `_kiro/session/compact`; a Kiro context window or token counts;
subagent context; backfilling old Kiro session logs; the sessions feed/sidebar.

## Design

### Overview

One helper, `translate.context_pct(update) -> float | None`, reads a counted frame. The
harness keeps the latest value for `session_status`; the turn's translator keeps the last,
max and distinct set for the turn's `llm_request`. Both read the same frames in
`_on_notification`. The TUI applies status values live and request values on replay.
`archie.db` indexes the request fields, and insights/stats show them.

### Data model

- `SessionStatus.context_pct: float | UnsetType = UNSET` (live-only, omitted when unset).
- `LLMRequest` (persisted, Kiro only, omitted when unset):
  - `context_pct: float | UnsetType` — last root `usagePercentage` of the turn (after the response).
  - `context_pct_max: float | UnsetType` — highest in the turn.
  - `context_samples: int | UnsetType` — distinct root percentages in the turn.
  - `kiro_requests: int | UnsetType` — `len(turn_completion.requestIds)`.
- `metrics_requests`: `context_pct REAL`, `context_pct_max REAL`, `context_samples INTEGER`,
  `kiro_requests INTEGER`, all nullable. `METRICS_VERSION = 6`.

### Code structure

- `shared/src/archie_shared/events.py`, `protocol.py` — fields, version.
- `agent/src/archie_agent/kiro/translate.py` — `context_pct()` helper; `TurnTranslator`
  collects the turn's values in `feed`, sets the fields in `_llm_request`.
- `agent/src/archie_agent/kiro/harness.py` — `_context_pct` state; `_on_notification`
  updates it and emits status on change; `status_event()` includes it.
- `cli/src/archie_cli/tui/session.py` (status + ledger rule, `context_pct_only`), `pane.py`,
  `status.py`.
- `orchestrator/src/archie_orchestrator/metrics.py`, `insights.py`,
  `templates/insights/session.html`, `static/insights/insights.js`; `cli/src/archie_cli/stats.py`.
- Tests: `tests/fake_kiro.py` (context frames and `requestIds` in the default turn),
  `tests/test_kiro_harness.py`, `tests/test_kiro_translate.py`, `tests/test_kiro_credits.py`
  (encoding), `tests/test_tui_canonical.py`, a StatusBar test in `tests/test_tui_app.py`,
  `tests/test_orchestrator_metrics.py`, `tests/test_insights.py`, `tests/test_web_insights.py`,
  `tests/test_cli_stats.py`.

## Milestones

### [ ] M1 — Live Kiro context in the status bar

- **Approach:** add `SessionStatus.context_pct` and bump `PROTOCOL_VERSION`. In
  `translate.py` add `context_pct(update)`. In `KiroHarness._on_notification`, for
  `session/update` call it; if not None and different from `self._context_pct`, store it and,
  when `_started` and not `_dead`, schedule `_emit_status()` (follow the `_fail` task
  **await** `self._emit_status()` inline: `AcpClient` awaits `on_notification`, and the
  translator's emits are awaited there too, so the status stays in order with the turn's
  events. `status_event()` passes `context_pct` (UNSET until known). `fake_kiro.py`: add
  `context(pct)` and send `context(2.5)` twice before the tool call and `context(3.1)` twice
  before the final chunk of the default turn. TUI: in `SessionStatus` handling set
  `self.context_pct_only = kiro_id is not None` and, when `event.context_pct` is a number, set
  `ledger.context_pct` before the existing `_status_changed()` call (not
  `_accounting_changed()`, which drops the streamed output estimate); in
  `_accumulate_ledger` skip the context update for `kiro/` root requests (M2 refines it).
  `pane.on_status` copies `context_pct_only`; `StatusBar` gets a `context_pct_only`
  reactive and `_context_text` renders `Ctx: N%` when set. Docs: `event-spec.md`
  `session_status` row and a protocol 5 compatibility note. ⚠️ `test_ws_integration` asserts
  protocol 4; update it (it fails in this container anyway).
- **Test seam:** `KiroHarness` with `fake_kiro.py` and a captured `event_bus.emit`;
  `harness._on_notification` called directly for the subagent frame;
  `SessionController.apply`; `StatusBar.fit`.
- **Deliverable:** a Kiro session's status bar shows `Ctx: 3%` live.
- **Verify:** `scripts/dev test tests/test_kiro_harness.py tests/test_tui_canonical.py tests/test_tui_app.py tests/test_protocol_version.py -q` passes; full `scripts/dev test` shows only the baseline failures; `scripts/dev ruff check .` passes.
- **Outcome:**

### [ ] M2 — Persist the turn's context on its `llm_request`

- **Approach:** add the four `LLMRequest` fields (UNSET pattern of `credits`). In
  `TurnTranslator.feed`, for counted frames track last, max and a set of values; in
  `_llm_request` set the fields per D5, `kiro_requests` from `completion["requestIds"]` when it
  is a list. TUI `_accumulate_ledger`: a root conversation request with `context_pct` set sets
  `ledger.context_pct`; Kiro without it leaves context unchanged. `fake_kiro.py`:
  `completion()` gains a `requestIds` list (3 IDs in the default turn). Docs: `event-spec.md`
  `llm_request` row and compatibility note, `kiro.md` (mapping table row, Limits).
- **Test seam:** `TurnTranslator.feed/finish`; `encode_event`/`decode_event`;
  `SessionController.apply` during replay; the harness test's log.
- **Deliverable:** a Kiro session replayed in the TUI shows the last turn's percentage.
- **Verify:** `scripts/dev test tests/test_kiro_translate.py tests/test_kiro_credits.py tests/test_kiro_harness.py tests/test_tui_canonical.py tests/test_canonical_session_events.py -q` passes; full `scripts/dev test` shows only the baseline failures; `scripts/dev ruff check .` passes.
- **Outcome:**

### [ ] M3 — Context in `archie.db`, insights and `archie stats`

- **Approach:** `metrics.py`: four columns, `_REQUEST_COLUMNS`, values from the event
  (`None` when UNSET), `METRICS_VERSION = 6`. `insights.py session_detail`: turn
  `context_pct` = `MAX(r.context_pct_max)` over root requests, request `context_pct`.
  `session.html`: turn context cell uses `m.pct(t.context_pct)` when not none; when any root
  request has `context_pct`, the chart uses each root request's `context_pct` (null gaps)
  and the pct title, passing a `label`; `insights.js` line chart uses `d.label` (default
  "Context tokens"). `stats.py render_session` Context cell uses the existing
  `_pct(t["context_pct"])` when not None. Docs:
  `architecture.md` metrics section (columns, version 6).
- **Test seam:** `MetricsIndex.sync` on fixture logs; `insights.session_detail`; the web
  session page via the existing `tests/test_web_insights.py` client; `archie stats session`
  via `CliRunner` in `tests/test_cli_stats.py`.
- **Deliverable:** `/ui/insights` session page and `archie stats session <id>` show Kiro
  turns' context percentage.
- **Verify:** `scripts/dev test tests/test_orchestrator_metrics.py tests/test_insights.py tests/test_web_insights.py tests/test_cli_stats.py -q` passes; full `scripts/dev test` shows only the baseline failures; `scripts/dev ruff check .` passes.
- **Outcome:**

## Deviations

## Risks

- Kiro may change the frame cadence or semantics; values would still be Kiro's own figure,
  only the update rate would change.
- The `context_samples`/`kiro_requests` mismatch cause is unknown; they are recorded, not
  interpreted.
- The metrics version bump rebuilds `metrics_*` from all logs on the first sync (seconds).

## Review

- Plan review: 1 pass, 0 ❌, 4 ⚠️, 6 ℹ️, all accepted. ⚠️ emit mode → M1 awaits
  `_emit_status()` inline. ⚠️ `_accounting_changed()` would drop the output estimate → M1
  sets the value before `_status_changed()`. ⚠️ `None = None` encodes `null` on native
  status frames → D2 uses UNSET; D12 names the tracker and live viewer. ℹ️ subagent seam
  → direct `_on_notification` call. ℹ️ `requestIds` in the fake → M2. ℹ️ Verify → full
  suite and ruff per milestone. ℹ️ chart vs table numbers and older turns → D11 (any
  root request; gaps; chart shows the post-turn value). ℹ️ stats → `_pct`. ℹ️
  `session_status` docs → M1. No second pass (no ❌).

## Outcome

PR: pending
CI: pending

## Follow-ups

- Re-evaluate per-request Kiro `llm_request` once `context_samples` vs `kiro_requests` data
  explains the mismatched turns.
- `/compact` via `_kiro/session/compact` (untested extension method).

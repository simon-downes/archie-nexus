# 061 — Compaction tool-style events

Status: done
Branch: build-it/061-compaction-events (base: main @ d26e7c9)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/061-compaction-events
Updated: 2026-06-16

## Objective

Expose context compaction to the client as a tool-like operation with a visible pending state and a terminal success or failure state. Persist both operation events so reconnect and replay preserve compaction activity, including failures. Render the requested compact format: `● Compaction <reason> (<before> → <after>, $<cost>, <duration>ms)`.

## Context

The agent currently persists the compactor `llm_request` and emits `context_compacted` only on success. Failures are live-only `error_notice` events, so the TUI cannot show compaction as an operation or reconstruct it after reconnect. The TUI already has pending/completed tool entries inside `IterationBlock`, but `context_compacted` is rendered separately as a post-hoc summary marker.

The user requested persisted `compaction_start` and `compaction_complete` events, with the client display behaving like regular tool calls. Cold-cache compaction already estimates the next request cost before deciding whether to compact; that estimate must be visible in the operation result. The existing token estimator includes system prompt and tool configuration when it computes a complete context estimate, while provider baselines retain the provider's context-token semantics.

## Baseline

- Check commands: `uv run pytest`; `uv run ruff check .`; `uv run ruff format .`
- Setup: `uv sync`; no config copied
- Setup leftovers: none
- Failing before this work: none; `uv run pytest` passed with 1,118 tests and `uv run ruff check .` passed
- Not included: `persona/skills/aws-agent-access/`, `plans/060-spec-container-aws-profiles.md`

## Decisions

| # | Decision | Chosen | Rejected | Why | Source |
|---|---|---|---|---|---|
| D1 | Should compaction start and completion be durable? | Persist both events | Live-only start/completion | The TUI's replay cursor and session log are the canonical history path; live-only failures disappear on reconnect | user Q1 |
| D2 | How should the client represent compaction activity? | Reuse the regular pending/completed tool-entry lifecycle | Keep the existing standalone `context_compacted` marker | The requested `●` pending/completed behavior already exists in `ToolEntry`, avoiding a second activity model | self |
| D3 | What should the completion cost field display? | Display the estimated cost saved on the next conversation request; for cold-cache starts, use the already-calculated expired-cache request estimate | Display only compactor model cost or add labels that change the requested format | The user explicitly wants the cold-cache next-request estimate and supplied an unlabeled dollar slot; `llm_request` remains the accounting source for actual compactor spend | self |
| D4 | How should the new events correlate? | Share a generated operation/request ID with the compactor `llm_request` | Generate independent IDs and correlate indirectly by ordering | Existing request identity uses `request_id`, and the compactor request ID is available before the provider call once generation moves to the harness boundary | self |
| D5 | Should old `context_compacted` remain? | Keep decoding/rendering compatibility for historical records, but emit only `compaction_complete` for new operations | Emit both for new compactions | Emitting both duplicates one user-visible operation and complicates replay; the new pair carries the same summary plus failure state | self |

## Requirements

- MUST persist one `compaction_start` before every attempted manual, threshold, or cold-cache compaction.
  - AC: harness tests observe `compaction_start` before the compactor `llm_request`, including preflight failures.
- MUST persist one `compaction_complete` for every started compaction, with success, failure, or interrupted status.
  - AC: success and provider/preflight/interruption tests observe exactly one matching completion and its error or result fields.
- MUST include reason, request/operation ID, before-token estimate, after-token estimate when available, duration, and compaction cost in the persisted events.
  - AC: round-trip event tests and harness tests assert these fields independently of implementation calculations.
- MUST expose the cold-cache next-request estimated cost in the compaction operation.
  - AC: cold-cache harness test asserts the start event contains the threshold estimate used for the decision.
- MUST render compaction with the exact user-facing shape `● Compaction <reason> (<before> → <after>, $<cost>, <duration>ms)` on success, and `● Compaction <reason> (<before> tokens, <duration>ms) Failed: <error>` on failure.
  - AC: TUI tests assert literal rendered text for threshold, cost, and cold-cache success/failure cases.
- MUST make the pending compaction indicator and terminal status use the same visual status behavior as regular tool calls.
  - AC: TUI tests assert the entry is pending after start, success after completion, and error after failed completion.
- MUST preserve `llm_request` as the sole token/cost accounting source.
  - AC: existing accounting tests continue to pass and compaction totals remain derived from the compactor request.
- MUST define before/after estimates as provider-facing context estimates, including system prompt/tool configuration when calculated heuristically; documentation MUST state the provider-baseline qualification.
  - AC: estimator and event documentation tests/assertions describe the inclusion rule.

Out of scope: changing compaction policy thresholds, changing model pricing, adding client-side repricing, changing orchestrator metrics, or migrating historical session files.

## Design

### Overview

The agent creates one operation ID before invoking `Compactor.run`. It emits persisted `CompactionStart`, invokes the existing compactor using the same ID, emits the existing compactor `LLMRequest`, then emits persisted `CompactionComplete` regardless of outcome. Successful transcript replacement remains atomic after completion. The TUI maps the start event to a pending `ToolEntry`-style compaction widget and completion to its terminal state.

### Code structure

- `shared/src/archie_shared/events.py`: add tagged persisted start/complete records and union membership.
- `agent/src/archie_agent/compaction.py`: accept the caller's request ID so start, request, and completion correlate.
- `agent/src/archie_agent/harness.py`: emit the operation pair and carry cold-cache estimated cost into the start event.
- `cli/src/archie_cli/tui/conversation.py`: add the compact tool-style entry and terminal rendering.
- `cli/src/archie_cli/tui/app.py`: reduce new events and connect them to the conversation widget.
- `docs/event-spec.md` and `docs/architecture.md`: document the durable event and accounting contract.
- Existing event, harness, and TUI test modules are the test seams; no new dependency or module is needed.

### Data model

- `compaction_start`: persisted; `id`, `turn`, `iteration`, `scope`, `request_id`, `reason`, `tokens_before`, `estimated_next_request_cost_usd: float | None`, `model_key`, `sent_at`.
- `compaction_complete`: persisted; `id`, `turn`, `iteration`, `scope`, `request_id`, `reason`, `status: success | failure | interrupted`, `tokens_before`, `tokens_after: int | None`, `cost_saved_usd: float`, `duration_ms`, `error: str | None`, `summary: str | None`.
- The compactor request's `llm_request.cost_usd` is the immutable compactor request cost when usage exists. The UI's dollar slot uses `estimated_next_request_cost_usd` for cold-cache compactions and `cost_saved_usd` otherwise; cumulative accounting still sums `llm_request`.

### Data flow

- Manual/automatic trigger → calculate before estimate and optional next-request estimate → persist start → run compactor → persist `llm_request` → persist complete → replace transcript only for success.
- Provider/preflight failure → persist start → persist failed complete with error and no replacement; no live-only error is required for the compaction failure itself.
- Client replay/live delivery → start mounts pending compaction entry → complete updates the same entry; an unmatched completion mounts a terminal entry for tolerant replay.

### Error handling

Provider failure, interruption, and preflight failure each produce a completion event with an error and leave the transcript unchanged. Storage failure follows the existing session event-bus failure path. Existing `context_compacted` logs remain a compatibility concern and are not rewritten.

## Milestones

### [x] M1 — Persist the compaction operation lifecycle

- **Approach:** Add strict tagged persisted events to the shared union. Generate the request ID before `Compactor.run`, pass it through to the compactor request, and emit start/complete around the existing request and transcript replacement. Keep `llm_request` unchanged as accounting authority.
- **Test seam:** Shared event encode/decode and agent harness compaction lifecycle tests.
- **Wiring:** Harness owns operation state; `Compactor` consumes the preassigned request ID; event bus persists and broadcasts start, request, and complete in order.
- **Edge cases:** preflight too-large transcript → failed complete without request; provider error → failed complete with error; interrupt → interrupted complete; success → complete with summary and after tokens.
- **Tasks:**
  - Add event records, union tags, and round-trip tests.
  - Pass request IDs into compactor requests.
  - Emit lifecycle events and cold-cache estimate from the harness.
  - Update harness tests for ordering and failure outcomes.
- **Deliverable:** Every attempted compaction has a durable start/complete pair with correlated request and result data.
- **Verify:** `uv run pytest tests/test_session_events.py tests/test_compaction_harness.py tests/test_compaction.py -q` passes and asserts start/request/complete ordering.
- **Outcome:** Added persisted `compaction_start`/`compaction_complete` events, correlated compactor requests, cold-cache estimate propagation, and durable failure outcomes. Focused lifecycle tests passed; no design deviation.

### [x] M2 — Render compaction as a regular tool-style activity

- **Approach:** Add a dedicated compact activity widget following `ToolEntry` pending/completed/error status semantics, rather than mounting the old summary marker. Format success and failure exactly as requested, with the reason text mapped to `threshold tokens`, `threshold cost`, or `threshold cold cache` as applicable.
- **Test seam:** TUI canonical reducer and conversation widget tests through `_apply_event` and rendered widget state/text.
- **Wiring:** Start event creates an entry keyed by request ID; complete event updates it; replay uses the same reducer path as live events.
- **Edge cases:** completion without a visible start → mount terminal entry; historical start followed by live completion → update, not duplicate; failed operation → error status and exact error text.
- **Tasks:**
  - Add pending/terminal compaction widget and conversation methods.
  - Reduce start/complete events in the TUI.
  - Replace success-only `context_compacted` rendering for new events while retaining old-log handling as needed.
  - Add literal format and status tests.
- **Deliverable:** The client shows compaction with the requested `●` format and regular tool-call status transitions.
- **Verify:** `uv run pytest tests/test_tui_canonical.py tests/test_session_events.py -q` passes with literal success/failure display assertions.
- **Outcome:** Added pending and terminal compaction rendering, exact success/failure format, replay correlation, and legacy marker suppression for new events. Focused TUI tests passed.

### [x] M3 — Document and complete the cross-package checks

- **Approach:** Update the canonical event catalog, architecture flow, and token-estimate semantics. Run focused and full checks plus formatting.
- **Test seam:** Documentation review plus repository check commands.
- **Tasks:**
  - Document event fields, persistence order, replay behavior, and estimate semantics.
  - Run full pytest, Ruff check, and Ruff format.
  - Review the final diff for protocol and compatibility impact.
- **Deliverable:** The event contract and implementation agree, and all repository checks pass.
- **Verify:** `uv run pytest && uv run ruff check . && uv run ruff format --check .` exits 0.
- **Outcome:** Updated event and architecture documentation. Full pytest passed (1,119 tests), Ruff check passed, touched-file formatting passed; repository-wide format check remains blocked by 19 unrelated pre-existing files.

## Change log

- C1 (M1/M2): New compactions initially emitted the legacy `context_compacted` event as well as the lifecycle pair. Review and the public-interface decision required one terminal operation, so new emission was removed while old-log decoding remains supported.
- C2 (post-review): A follow-up review found the TUI entry never left its pending state, because `Static.update()` rejected a `markup` argument and a bare `except` hid the error. The pending header also showed `○ ●`. Fixed both, and the entry keeps its terminal markup so a completion that arrives before mount still renders. Replaced the mocked TUI tests with mounted-widget tests that assert literal text and colours. Other changes: interruption is reported by `CompactionOutcome.interrupted` instead of matching the error text; the duplicate compaction `ErrorNotice` was removed, so the failed completion is the only failure signal; `compaction_complete` now carries `estimated_next_request_cost_usd`, so replay without the start shows the same dollar value; the unreachable `threshold cost` label was removed; the throbber is restored after an automatic compaction; replay reset clears pending compactions. Added harness tests for preflight failure, `status`/`error` fields, and the cold-cache estimate.

## Risks

- Existing `context_compacted` records remain in historical logs; the implementation keeps replay decoding and legacy rendering compatible, while new compactions emit only the lifecycle pair.
- The dollar value needs a clear producer-side distinction between compactor spend and the avoided/estimated next-request cost; the event fields keep both concepts available without changing accounting.

## Review

- Plan review: not run; build-it proceeded directly from the requested implementation.
- Code review: 2 rounds. Correctness approved with no findings. Conventions suggestions were applied: plan field names now match the serialized contract and pending compactions are strongly typed. Risk reviewer initially reported a stale protocol-version claim (the version is 3) and one valid duplicate-event concern; the duplicate legacy emission was removed, and historical `context_compacted` decoding/rendering remains supported.
- Merge risk: Medium: this changes a persisted public event union and requires protocol-v3 clients, but the protocol warning, round-trip tests, full suite, and strict event schema limit unnoticed incompatibility.

## Outcome

Implemented persisted `compaction_start` and `compaction_complete` events, correlated compactor requests, cold-cache next-request cost display, exact tool-style TUI formatting, failure/interruption states, protocol version 3, and historical `context_compacted` compatibility. New compactions emit only the lifecycle pair. Full pytest passed with 1,121 tests; Ruff check and touched-file format checks passed. Repository-wide format check still reports 19 unrelated pre-existing files.

PR: error — `pr.py open` could not fetch origin: GitHub SSH returned `Permission denied (publickey)`
CI: none

## Follow-ups

- None currently.

# 076 — Compaction state recovery

Status: planning
Branch: build-it/076-compaction-state-recovery (base: main @ 53923ce)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/076-compaction-state-recovery

## Objective

Compaction summaries lose workspace state, scratch artefacts, environment workarounds,
learned file facts and verbatim drafts. After compaction, agents never use the session log
to recover what the summary dropped unless the user tells them to. The logged
`cost_saved_usd` understates the saving by 4–5×, and nothing records which skills were
lost. When this work is done, the compaction prompt asks for the missing state. The
replacement turn names the session log and explains how to search it. `compaction_complete`
reports a cache-write-based saving and the skills loaded at compaction.

## Context

The source is the analysis in `~/.nexus/brain/projects/archie/compaction-performance-analysis-2026-10.md`
(85 compaction events, 2026-10-07 to 2026-10-26). The user chose recommendations 1, 2 and
6. They excluded 4 (durable memory guidance), 5 (cold-cache trade-off) and 7 (optional
Next Steps), and they dropped 3 (failure handling). Recommendation 3 is out because all
four failures were expired credentials. A fallback model would fail the same way, and the
user's message is already kept in `session.turns`. The report's side finding (unknown agent
names) is being handled in another session.

Discovery:
- `agent/src/archie_agent/compaction_prompts.py` holds `SYSTEM`, `INSTRUCTIONS`, `FRAMING`
  and `CONTINUATION`. `INSTRUCTIONS` lists seven optional sections. The transcript being
  compacted includes any earlier summary, so carry-forward rules belong in `INSTRUCTIONS`.
- `compaction.build_replacement(summary, mode, pending)` builds the replacement user turn
  as `[FRAMING, <summary>, pending… | CONTINUATION]`. Its only production caller is
  `AgentHarness._compact` in `harness.py`, which has `self.log_path`.
- Session logs are JSONL. Each line has a `type` tag; `user_message`, `assistant_message`
  and `tool_result` carry `content`, `tool_call` carries `name` and `input`, and all carry
  `turn` (`shared/src/archie_shared/events.py`, `docs/event-spec.md`).
- `_compact` computes `cost_saved_usd` at the plain input rate. The TUI shows
  `estimated_next_request_cost_usd` when present and falls back to `cost_saved_usd`
  (`cli/src/archie_cli/tui/app.py:397`).
- `_loaded_content_keys` holds `(skill, "__body__" | reference)` pairs and is cleared on
  successful compaction (`skills.BODY_KEY`).
- `CompactionComplete` uses `forbid_unknown_fields=True`. The previous field addition
  (`estimated_next_request_cost_usd`, plan 061) added an optional field with a default;
  this build follows that pattern.

## Baseline

- Check commands: `env -u ARCHIE_PLUGINS_DIR -u ARCHIE_HOME_DIR -u ARCHIE_PERSONA_DIR -u ARCHIE_SESSION_ID uv run pytest -q -p no:cacheprovider`; `uv run ruff check .`; `uv run ruff format --check .`
- Setup: `uv sync` in the worktree; nothing copied in.
- Setup leftovers: none
- Failing before this work: none (2143 passed with the env vars unset; with them set, plugin tests conflict with `/opt/archie/plugins`)
- Not included: none (the build is based on `main`; the user's `improve-tool-strategy` checkout is untouched)

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Which recommendations to build | 1, 2, 6 | 3, 4, 5, 7 | The user excluded 4, 5 and 7. Rec 3's premise was wrong (credential expiry, message already kept) | user |
| D2 | How "Files" changes | Split into `## Files and Findings` (`path[:line] — what was learned`) and `## Commands and Identifiers` | Keep one merged section | The report found lists of paths without findings caused bulk re-reads; commands and IDs are a different kind of item | self |
| D3 | Verbatim drafts | New `## Working Text` section, kept verbatim | Ask the compactor to save a file | The compactor has no tools, so it can't write a file | self |
| D4 | Where the log pointer goes | A separate text block after the summary in every mode, built by `build_replacement` from a `log_path` argument | Put it in `FRAMING` or only in `CONTINUATION` | `FRAMING` is a constant. Manual and start-of-turn replacements need the pointer as much as mid-turn ones | self |
| D5 | Log recipe | Inline prose: the path, the four event types, the `turn`/`content`/`input` fields, "grep before re-deriving or asking" | A `recall` tool | The report says a tool isn't justified until the prompt pointer has been tried | self |
| D6 | `cost_saved_usd` formula | Tokens removed × the active model's `cache_write` rate (or `input` when it has none), documented as a lower bound | Also model later cache reads | Later reads depend on how long the session continues, which isn't known at compaction | self |
| D7 | Skill field | `skills_loaded: list[str] = []`: sorted names of skills whose body was loaded, set on success and failure | Include reference paths; success only | Measuring skill loss needs skill names only. A failed compaction keeps skills, but recording them is harmless and keeps the logic in one place | self |

## Requirements

- MUST make the compaction `INSTRUCTIONS` list these sections in this order: Goal,
  Instructions and Preferences, Progress, Decisions and Discoveries, Workspace State,
  Environment and Workarounds, Artefacts, Files and Findings, Commands and Identifiers,
  Working Text, Current Task, Next Steps.
  - AC: a test asserts that each `## ` heading appears in `INSTRUCTIONS` in that order.
- MUST tell the compactor what each new section holds: absolute repo roots, branch and
  worktree per checkout, and whether each still exists (Workspace State); how to reach
  each external system, written as rules to follow (Environment and Workarounds); scratch
  and `/tmp` paths and what each holds (Artefacts); one `path[:line] — what was learned`
  per line (Files and Findings); the latest draft or agreed text, verbatim (Working Text).
  It MUST also say to carry Workspace State and Environment and Workarounds forward from
  an earlier summary unless the transcript supersedes them.
  - AC: a test asserts that `INSTRUCTIONS` contains "as of compaction", "carry forward",
    "verbatim" and "path[:line]".
- MUST add a block naming the absolute session log path to every replacement turn
  (manual, start of turn, mid turn), placed after the summary and before any pending
  message or continuation. The block names the event types `user_message`,
  `assistant_message`, `tool_call` and `tool_result`, the `turn` field, and the `content`
  and `input` fields, and says to search the log before re-deriving work or asking the user.
  - AC: `test_replacement_shape` checks the block's position and its path for all three modes.
  - AC: a harness test asserts the turn after manual compaction contains `str(harness.log_path)`.
- MUST compute `cost_saved_usd` on success as `max(0, tokens_before − tokens_after) ×
  (cache_write or input rate) / 1e6`, using the active conversation model's rate at
  `tokens_before`.
  - AC: a harness test using the fixture model (input 5, cache_write 6.25) asserts the
    cache-write formula; a non-caching model asserts the input-rate formula.
- MUST add `skills_loaded: list[str]` (default `[]`) to `CompactionComplete`. It holds the
  sorted, unique skill names with a loaded body at compaction time, on success, failure
  and interruption.
  - AC: a harness test with `("build-it", "__body__")` and `("build-it", "references/x.md")`
    loaded asserts `skills_loaded == ["build-it"]`; an existing test without skills sees `[]`.
  - AC: a log line without `skills_loaded` still decodes (backward compatibility).
- MUST update `docs/event-spec.md` and `docs/architecture.md` for the new field, the
  `cost_saved_usd` meaning, and the log pointer in the replacement turn.
  - AC: `rg skills_loaded docs/event-spec.md` matches; the docs describe `cost_saved_usd`
    as a cache-write lower bound.

Out of scope: compaction failure handling (rec 3), memory-file guidance (rec 4), cold-cache
trigger changes or keeping the last exchange verbatim (rec 5), Next Steps wording (rec 7),
the unknown-agent side finding, and the TUI's cost display.

## Design

### Overview

There are three independent changes to the compaction path. The prompt text lives in
`compaction_prompts.py`. The replacement turn gains a log-pointer block built in
`compaction.py`. The harness passes `log_path`, changes the saving formula, and fills a new
event field.

### Code structure

- `agent/src/archie_agent/compaction_prompts.py`: rewrite `INSTRUCTIONS`; add `LOG_POINTER`,
  a template string with a `{log_path}` placeholder.
- `agent/src/archie_agent/compaction.py`: `build_replacement(summary, mode, pending, log_path)`
  appends `TextBlock(LOG_POINTER.format(log_path=log_path))` after the summary.
- `agent/src/archie_agent/harness.py` `_compact`: pass `self.log_path`; change the saved
  rate; compute `skills_loaded` from `_loaded_content_keys` before the clear and pass it
  to both `CompactionComplete` constructions.
- `shared/src/archie_shared/events.py`: `CompactionComplete.skills_loaded: list[str] = []`.
- Tests: `tests/test_compaction.py` and `tests/test_compaction_harness.py`, extended in place.

### Data model

- `CompactionComplete.skills_loaded`: `list[str]`, optional on decode (default `[]`), sorted skill names.

## Milestones

### [ ] M1 — Compaction prompt asks for state sections

- **Approach:** Rewrite `INSTRUCTIONS` in `compaction_prompts.py`, keeping the existing rules
  (omit empty sections, never invent a goal, preserve exact text, resolve terse replies,
  restate the task). Add one short line of guidance per new section. Keep "as of
  compaction" wording for workspace facts. Write workarounds as rules ("Use profile X for
  Y"). ⚠️ `Compactor.transcript_budget` subtracts the prompt length, so a longer prompt
  slightly shrinks the budget. That's acceptable.
- **Test seam:** the `INSTRUCTIONS` constant (`tests/test_compaction.py`).
- **Deliverable:** compaction requests ask for the twelve sections.
- **Verify:** `uv run pytest tests/test_compaction.py -q` passes, including the new heading-order test.
- **Outcome:**

### [ ] M2 — Replacement turn points to the session log

- **Approach:** Add `LOG_POINTER` to `compaction_prompts.py`. Add a `log_path` parameter to
  `build_replacement` and update the call in `harness._compact` and the two calls in
  `tests/test_compaction.py`. Check the size: the block is about 100 tokens, and
  `tokens_after` already includes it through `_estimate_context([replacement])`.
- **Test seam:** `build_replacement` and `AgentHarness.run_manual_compaction`.
- **Deliverable:** every compacted turn names the log and how to search it.
- **Verify:** `uv run pytest tests/test_compaction.py tests/test_compaction_harness.py -q` passes.
- **Outcome:**

### [ ] M3 — Compaction instrumentation: cache-write saving and skills_loaded

- **Approach:** In `harness._compact`, replace `.input` with `rate.cache_write or rate.input`,
  matching how `_before_request` prices the next request. Compute
  `skills_loaded = sorted({name for name, key in self._loaded_content_keys if key == BODY_KEY})`
  before the clear (import `BODY_KEY` from `archie_agent.skills`). Add the field to
  `events.py`. Update `docs/event-spec.md` (field table row and the `cost_saved_usd`
  sentence) and `docs/architecture.md` (the compaction paragraph).
- **Test seam:** `AgentHarness.run_manual_compaction` and the persisted `CompactionComplete`;
  `decode_event` for the old shape.
- **Edge cases:** `tokens_after > tokens_before` → saving is 0 (unchanged `max`); a model
  with no cache_write → input rate; reference loaded without its body → not listed.
- **Deliverable:** `compaction_complete` reports the new saving and the loaded skills.
- **Verify:** the full check commands pass, with no new failures.
- **Outcome:**

## Deviations

## Risks

- A longer prompt can make the compactor produce longer summaries, which raises
  `tokens_after` slightly. The report's median after compaction is 9.3k, so a few hundred
  extra tokens are immaterial.
- Agents may grep the log more often after compaction. That is the intended behaviour; each
  grep is cheap compared with re-deriving work.

## Review

- Plan review:
- Code review:
- Merge risk:

## Outcome

PR: pending
CI: pending

## Follow-ups

- Credential-expiry messaging for failed compactions (dropped recommendation 3).

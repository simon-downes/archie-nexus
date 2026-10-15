# Plan format

The standard for a build-it plan. The orchestrator writes to it; the plan reviewer checks
against it. Fill a section only as far as it carries information an implementor or
reviewer needs: a one-milestone change gets a short plan.

---

## Plan rules

**Requirements**
- Use MUST by default, SHOULD only with the reason it may be skipped, and MAY for
  permitted options. Each states observable behaviour or a real constraint, not a coding
  technique.
- Every MUST has at least one AC that a test or command can check. If you can't picture
  the test, the requirement isn't decided yet.
- Give a number or a named mechanism instead of an unmeasured adjective (fast, secure,
  robust, properly). Bad: `MUST validate inputs`. Good: `MUST reject ls --json with an
  unknown --sort key` / `AC: exits 2, prints {"error": "unknown sort key: x"} to stderr`.

**Milestones**
- Each is a vertical slice: one observable deliverable through every layer it touches. A
  layer-only milestone is allowed only when labelled **prefactor**.
- M1 is the tracer bullet: the core behaviour, end to end, as thin as possible. Between
  two small core candidates, pick the one that removes more uncertainty. Later milestones
  each add one behaviour to the working slice.
- Order by dependency. Each milestone fits in one working session; a small change may
  need only one.
- No decision is left for implementation: the implementor never picks a library, decides
  where code lives, or invents a pattern. "Follow the pattern" names a file to open.
  Exact API calls, option names and library edge cases are details for implementation;
  nobody pre-verifies them.
- Every behavioural milestone names its test seam: the public boundary the tests use.
- Verify is an exact command or observation and its passing result
  (`uv run pytest tests/test_ls.py -k json` passes), never "test it".
- Write only milestones you can state precisely now. Suspected work goes in Risks.

**Traceability:** every MUST traces to a milestone, and every milestone to a requirement.
Every file, module and dependency the plan names exists or is marked new.

---

## Template

Omit optional fields and Design subsections that don't apply. Keep the `Status:`,
`Branch:`, `PR:` and `CI:` lines in exactly this form, and the filename as
`<NNN>-build-<slug>.md`: the scripts parse them.

```markdown
# <NNN> — <Title>

Status: planning | building | reviewing | done
Branch: build-it/<NNN>-<slug> (base: <base branch, or "(detached)"> @ <short sha>)
Worktree: <absolute path>

## Objective

<2-4 sentences: the problem and the outcome when done.>

## Context

<The idea in the user's words, why it exists, codebase facts found during discovery, and
constraints. Enough for a fresh session to understand the why; not a transcript.>

## Baseline

- Check commands: <commands, or "none">
- Setup: <commands run; untracked config copied in, by name only>
- Setup leftovers: <untracked files setup created or copied, or "none">
- Failing before this work: <checks, or "none">
- Not included: <the user's uncommitted changes, by path, or "none">

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | <question> | <option> | <option(s)> | <reason tied to this codebase> | user Q1 |
| D2 | ... | ... | ... | ... | self |

## Requirements

- MUST <observable behaviour or constraint>
  - AC: <testable criterion>

Out of scope: <explicit exclusions>

## Design

<Only the subsections that apply; see the table below.>

## Milestones

### [ ] M1 — <objective>

- **Approach:** <how and why: files, patterns to follow, ⚠️ gotchas, assumptions to confirm>
- **Test seam:** <the public boundary the tests exercise>
- **Edge cases:** <optional, for external input or failure modes: scenario → behaviour>
- **Deliverable:** <one observable outcome>
- **Verify:** <exact command or observation, and what a pass looks like>
- **Outcome:** <filled when done: what was built, the evidence, any deviation>

## Deviations

- <M#: what changed from the reviewed plan, why, and the effect on scope>

## Risks

- <known risk, open assumption, or unresolved review finding>

## Review

- Plan review: <passes, and how the findings were resolved>
- Code review: <roles run (and why `risk` was skipped), rounds, findings fixed, each
  rejection with its reason>
- Merge risk: <level>: <failure path and what limits it>

## Outcome

<Filled at the end: what was delivered, verification evidence, what was not verified.>

PR: <pending | URL | no remote | no change | the error>
CI: <passed | none | still running after 20 minutes | failing check, cause, what was done>

## Follow-ups

- <deferred work, out-of-scope findings>
```

---

## Section details

- **Decisions:** one row per choice that changes scope, behaviour, interfaces, data, or
  where code lives: a choice that would have sent the implementor down a different path.
  Leave obvious choices in the code. A row with no rejected option wasn't a decision, so
  drop it. A Why that would fit any project ("simpler", "best practice") is not a reason:
  tie it to a constraint of this one.
- **Deviations** start after the plan review. Ticks, Outcome lines and `Status` changes
  are not deviations.
- **Review** and **Outcome** record evidence (commands and their results), not claims.

### Design subsections

Reference existing patterns instead of describing them, and say where this work deviates
from them and why.

| Subsection | Include when | Done when |
|---|---|---|
| Overview | always | a newcomer can state what is built and the strategy in one breath |
| Stack | new or reused dependencies | each says new vs reused and why it beat the alternative |
| Code structure | code changes | every new module has a path; every "follow the pattern" names a file |
| Data model | new or changed data | every field has a type and required/optional; every store has a lifecycle |
| Data flow | the flow isn't obvious, or state is shared across modules | each key scenario has a happy path and its failure paths; shared state names its producers and consumers |
| Error handling | external calls, multi-step ops, shared state | every named failure has a named response (retry, rollback, fail open, surface) |
| Non-functional | security, performance or observability surface | each concern names a number or a mechanism |
| Config / infra | env vars, secrets, CI, migrations | each item is named |

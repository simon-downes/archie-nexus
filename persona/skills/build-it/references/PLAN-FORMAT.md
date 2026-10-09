# Plan format

The template for a build-it plan, plus the per-section details that aren't in the main
skill. The rules for requirements, milestones and traceability are in SKILL.md under
**Plan rules**; follow them when you fill this in.

---

## Template

```markdown
# <NNN> — <Title>

Status: interviewing | planning | implementing | reviewing | done
Branch: build-it/<NNN>-<slug> (base: <base branch, or "(detached)"> @ <short sha>)
Worktree: <absolute path>
Updated: <YYYY-MM-DD>

## Objective

<2-4 sentences: the problem and the outcome when done.>

## Context

<The original idea in the user's words, why it exists, relevant codebase facts found at
intake, and constraints. Enough that a fresh session understands the "why"; not a
transcript.>

## Baseline

- Check commands: <commands, or "none">
- Setup: <setup commands run in the worktree; untracked config files copied in, by name only>
- Setup leftovers: <untracked, non-ignored files that setup created or copied, by path; or "none">
- Failing before this work: <checks that already fail, or "none">
- Not included: <the user's uncommitted changes, by path, which the build started without; or "none">

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | <question> | <option> | <option(s)> | <reason tied to this codebase> | user Q1 |
| D2 | ... | ... | ... | ... | self |

## Requirements

- MUST <observable behaviour or constraint>
  - AC: <testable acceptance criterion>
- SHOULD <...>
  - AC: <...>

Out of scope: <explicit exclusions>

## Design

<Only the subsections that apply; see the table below.>

## Milestones

### [ ] M1 — <objective>

- **Approach:** <how and why: libraries, files, patterns to follow, ⚠️ gotchas>
- **Test seam:** <the public boundary the tests exercise>
- **Wiring:** <only if shared state or cross-module flow: state, producers, consumers, call site>
- **Edge cases:** <only if external input or failure modes: scenario → behaviour, one per line>
- **Tasks:**
  - <concrete unit of work>
- **Deliverable:** <one observable outcome>
- **Verify:** <exact command or observation, and what a pass looks like>
- **Outcome:** <filled when done: what was built, evidence, deviations (→ C#)>

### [ ] M2 — ...

## Change log

- C1 (M<n>): <what changed> — <why, with evidence> — <effect on scope>

## Risks

- <known risk, open uncertainty, or unresolved review finding>

## Review

- Plan review: <pass count, findings and how each was resolved>
- Code review: <round count, roles run (and why `risk` was skipped, if it was), findings
  and how each was resolved; each rejected finding with the reason>
- Merge risk: <Low | Medium | High | Severe>: <one sentence: the failure path and what limits it>

## Outcome

<Filled at the end: what was delivered, verification evidence, what was not verified.>

PR: <pending | URL | no remote | no change | the error>
CI: <passed | none | still running after 20 minutes | the failing check, its cause, and what was done>

## Follow-ups

- <deferred work, out-of-scope review findings>
```

---

## Section details

- **Header.** The `Branch:` line must keep the form `(base: <branch> @ <sha>)`: the
  scripts read the base from it. `Updated` changes whenever the plan does.
- **Decisions.** One row per decision that would have sent the implementor down a
  different path. "Rejected" is required; a decision with no rejected alternative was not
  a decision, so drop it.
- **Approach vs Tasks.** Approach is how and why; Tasks are what. Don't repeat one in the
  other.
- **Wiring and Edge cases** are required whenever their triggers apply, and omitted
  otherwise.
- **Review and Outcome** record evidence (commands run and their results), not claims.

### Design subsections

Include only the subsections the work needs. Each must meet its "done when" bar. Reference
existing patterns instead of describing them again, and call out where this work deviates
from them and why.

| Subsection | Include when | Done when |
|---|---|---|
| Overview | always | a newcomer can state what is built and the strategy in one breath |
| Stack | new or reused dependencies | each dependency says new vs reused and why it beat the alternative |
| Code structure | always for code changes | every new module names its path; every "follow the pattern" names a file to open |
| Data model | new or changed data | every field has a type and required/optional; every store names its lifecycle |
| Data flow | the flow isn't obvious | each key scenario has a happy path and its failure paths |
| Error handling | external calls, multi-step ops, shared state | every named failure has a named response (retry, rollback, fail open, surface) |
| Non-functional | security, performance, or observability surface | each concern names a number or a mechanism, not "handle appropriately" |
| Config / infra | env vars, secrets, CI, migrations | each item is named; categories that don't apply say "none" |

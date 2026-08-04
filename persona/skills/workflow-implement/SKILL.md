---
name: workflow-implement
description: >
  Execute an approved implementation plan, milestone by milestone. Use when implementing
  approved plans from local plan files or an issue tracker, or when the user says
  "implement this", "build it", "start coding", "execute the plan", "let's implement", or
  references an approved plan file. Not for creating or designing a plan (use workflow-plan),
  reviewing finished work (use workflow-review), or ad-hoc changes with no plan. Works
  through milestones sequentially with progress tracking.
---

# Purpose

Execute approved plans by working through milestones sequentially.

---

# When to Use

- User approves a plan and requests implementation
- Executing milestones from `./plans/<NNN>-<description>.md`

# When Not to Use

- Work that doesn't have an approved plan (use workflow-plan first)
- Reviewing completed work (use workflow-review)

---

# Workflow

## 1. Load Plan

The plan source depends on how it was created:

**From issue tracker:** the user provides an issue identifier (e.g. "implement PLAT-123").
Fetch the issue and read the plan from its description.

**From local file:** read from `./plans/<NNN>-<description>.md`. If no path specified,
list `./plans/` to show available plans and ask which one.

Verify the plan contains: Objective, Requirements, Technical Design, and Milestones.

If incomplete or unclear → ask for clarification before proceeding.

**Build a progress ledger.** After loading the plan, create an explicit checklist of the
milestones — one line each with `[ ]` — as the durable record of progress. This externalises
state so it survives context compaction and makes resumption deterministic.
- **Local file plan:** write `./plans/<NNN>-<description>-progress.md`.
- **Tracker plan:** maintain the checklist as a checklist in the issue (or a pinned comment).
Update the ledger (`[ ]` → `[x]`) immediately after each milestone is committed.

**Resuming:** read the progress ledger to identify the last completed milestone. Cross-check
against `git log`, confirm with the user, then continue from the next unchecked milestone.

## 2. Execute Each Milestone

For each milestone, work through these steps:

### Setup (first milestone only)

If the plan came from an issue tracker:
- Create a branch from the default branch: `<user>/<ISSUE-KEY>-<description>`

If the plan is a local file, create a branch: `<user>/<plan-number>-<description>`
(e.g. `simon/001-rate-limiting`).

**Post-start actions:** After creating the branch, update the issue status to
"In Progress" (or nearest equivalent) if an issue tracker is configured for the project.
Determine the project configuration and issue tracker provider (refer to `# Available Tools`).
If no tracker is configured, skip silently — issue operations are supportive, not blocking.
If the update fails, warn and continue.

### A. Understand the Context

Read the milestone's Approach for technical direction, then investigate the relevant areas
of the codebase to understand implementation details. The Approach narrows where to look.

Load any skills referenced or implied by the Approach (e.g., if it mentions Terraform
patterns, load the Terraform skill).

### B. Work Through Tasks

Implement each task from the milestone's Tasks list:

1. **Test first for behavioural tasks.** For any task that creates or modifies behaviour:
   write the test first (**red**), then the implementation to pass it (**green**). Target
   the test seam named in the milestone's Approach — the public interface, not internals.
   Skip test-first only for pure wiring, config, or infrastructure with no behavioural
   contract to assert.
2. Perform the work following existing conventions and the Approach guidance.
3. Work only on the current milestone; implement it fully before moving on.

A task is complete when its observable effect exists in the codebase — a new file, a
modified interface, a passing test. If the effect can't be observed, the task isn't done.

**Test anti-patterns to avoid** (a test that can't fail is not a test):
- **Tautological** — the assertion recomputes the expected value the way the code does, so
  it passes by construction. Expected values come from an independent source: a known-good
  literal, a worked example, the spec.
- **Implementation-coupled** — mocks internal collaborators, tests private methods, or
  verifies through a side channel (e.g. querying the database instead of the interface). The
  tell: it breaks on refactor when behaviour hasn't changed. Test at the named seam.
- **Horizontal-slicing** — writing all tests, then all implementation. Work one test → one
  implementation → repeat, so tests track real behaviour rather than an imagined shape.
- **Mock at boundaries only** — external APIs, databases, time/randomness. Never mock your
  own classes or internal collaborators.

If a task is too coarse to implement directly, break it into sub-steps and work
through them.

### C. Verify Deliverable

Two checks:

1. **Review** — invoke `workflow-review` in milestone mode, which triages whether a
   full review is needed based on the milestone's scope and risk. This runs mechanical
   checks (qa-runner) and reasoning-level quality review (code-reviewer subagent).
   Fix any findings marked ❌ before proceeding.
2. **Milestone-specific** — use the milestone's Verify section to confirm the deliverable
   (a specific command, test, or observable behaviour).

If either check fails → attempt to fix. If three meaningfully different attempts fail,
stop, summarise what was tried, and ask the user for guidance.

### D. Commit and Update Ledger

If in a git repository, run exactly:
```bash
git add -A
git commit -m "<type>: <milestone description>"
```
Use conventional commit format — `<type>` is one of `feat`, `fix`, `docs`, `refactor`,
`test`, `chore`. Each milestone gets its own commit — preserve the complete history
(never force-push a shared branch).

Then mark this milestone `[x]` in the progress ledger (local file or issue checklist).

### E. Report and Continue

After each milestone:
- Report completion to user with a brief summary
- Proceed immediately to the next milestone — maintain momentum, don't pause for confirmation

If something unexpected arises that changes the plan's assumptions, stop and discuss.

## 3. Complete Implementation

When all milestones are done:

1. Summarise deliverables to user
2. Confirm every milestone in the progress ledger is marked `[x]`
3. If the plan came from an issue tracker, update the issue status to "In Review"
   (or nearest equivalent). Determine the project configuration and issue tracker
   provider (refer to `# Available Tools`). If the update fails, warn and continue.
4. If the plan is a local file, move it (and its `-progress.md` ledger) to a `done/`
   subdirectory alongside it (e.g. `plans/done/`)
5. Run `workflow-review` in full mode across all changes (from branch base to HEAD)
6. Fix all raised issues including suggestions — unless you specifically disagree
   with a finding (state why and leave it)

---

# Handling Issues

## Unclear Requirements or Design

Stop immediately and ask for clarification.

## Discovered Work Not in Plan

Complete the current milestone as planned, then:

1. Report to user after milestone completion
2. Propose plan amendment if needed

**Never implement unplanned work without approval.**

## Plan Appears Flawed

Stop and discuss with user:

1. Explain the specific issue
2. Propose concrete amendments to the plan
3. Show changes in diff format
4. Wait for approval

**Never modify the plan without explicit approval.**

See [references/PROBLEM-HANDLING.md](references/PROBLEM-HANDLING.md) for detailed examples.

---

# Key Principles

- **Plan is the source of truth** — Approach gives direction, Tasks define the work,
  Verify confirms completion. Don't reinvent what the plan already provides.
- **Autonomy within bounds** — the plan sets direction, but use judgment for
  implementation-level details it doesn't specify.
- **Work only the current milestone** — implement it fully before moving on.
- **Test first for behaviour** — red before green; test at the seam the plan names.
- **Stop when unclear** — don't guess or improvise beyond the plan.

---

# Example

**User:** "Let's implement the plan"

1. Load `./plans/001-rate-limiting.md`
2. Milestone 1: Read Approach (use ioredis, key format, TTL strategy), explore
   existing Redis config and patterns, implement Tasks, run qa-runner, verify
3. Commit, report to user
4. Repeat for remaining milestones
5. Report completion, offer Review Mode

See [references/EXECUTION.md](references/EXECUTION.md) for a detailed walkthrough.

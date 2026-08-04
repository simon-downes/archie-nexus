---
name: workflow-review
description: >
  Review code changes for quality, standards compliance, and plan alignment. Supports
  milestone reviews during implementation, full implementation reviews, and standalone
  PR/ad-hoc reviews. Orchestrates qa-runner for mechanical checks and a code-reviewer
  subagent for reasoning-level quality assessment. Use when reviewing code changes,
  verifying implementations against plans, checking code quality before committing or
  merging, reviewing pull requests, or when asked to "review this", "check the code",
  or "is this ready to merge".
---

# Purpose

Evaluate code changes for quality and correctness. Orchestrates mechanical checks (qa-runner)
and reasoning-level review (code-reviewer subagent) into a single workflow.

---

# When to Use

- Verifying milestone work during implementation (consumed by workflow-implement)
- Reviewing a complete implementation against its plan
- Reviewing a pull request or ad-hoc code changes
- Checking code quality before merging

# When Not to Use

- Reviewing plans before implementation (use action-review-plan)
- Creating or modifying plans (use workflow-plan)
- Running only mechanical checks without reasoning review (use qa-runner directly)

---

# Review Modes

## Milestone Review

**Trigger:** consumed by workflow-implement during the verify step, before commit.

**Scope:** files changed in the current milestone.

**Checks:**
- Mechanical quality (qa-runner)
- Reasoning quality (code-reviewer subagent)
- Milestone deliverable alignment (does the work match the plan's Deliverable?)

**Plan alignment:** light — current milestone's Approach, Tasks, and Deliverable only.

**When to run vs skip:**
- Always review: security-sensitive work, foundational milestones (first 1-2), milestones
  establishing new patterns, large milestones (> ~15 files changed)
- Consider skipping: pure boilerplate/CRUD, late milestones wiring already-reviewed
  components, small isolated changes (< ~5 files)
- When in doubt, review — catching a bad pattern early is cheaper than fixing it late

## Implementation Review

**Trigger:** user requests review after all milestones complete.

**Scope:** all files changed during implementation.

**Checks:**
- Mechanical quality (qa-runner)
- Reasoning quality (code-reviewer subagent — may partition for large changes)
- Full plan alignment (requirements tracing, design adherence)

**Plan alignment:** full — trace every MUST requirement to implemented code, verify
design decisions were followed.

**Scope resolution:**
1. Feature branch → `git diff $(git merge-base main HEAD)`
2. Plan exists → find milestone commits from git log, diff the range
3. User specifies scope → use that
4. None of the above → ask

## Standalone Review

**Trigger:** user asks to review a PR, diff, or set of files. No plan exists.

**Scope:** the diff or specified files.

**Checks:**
- Mechanical quality (qa-runner)
- Reasoning quality (code-reviewer subagent)

**No plan alignment.** The review covers code quality only. Output includes a clear
disclaimer: "This review covers code quality and standards. Functional completeness
cannot be verified without a plan or specification."

**Intent inference:** attempt to determine what the changes are meant to achieve from
PR description, commit messages, or the diff itself. Present the inferred intent for
the user to validate. If intent cannot be inferred, proceed with quality-only review.

---

# Workflow

## 1. Determine mode and scope

Based on how the skill was invoked:
- Called by workflow-implement → milestone review mode
- Plan exists and all milestones complete → implementation review mode
- No plan or user specifies files/PR → standalone review mode

**Scope resolution by mode:**

**Milestone review:**
- Changed files are the unstaged/staged changes in the working tree (pre-commit)

**Implementation review:**
1. Feature branch → `git diff $(git merge-base main HEAD)`
2. Plan exists → find milestone commits from `git log`, diff the range
3. User specifies scope → use that
4. None of the above → ask

**Standalone / PR review:**
- User specifies files → use those
- PR number provided → use `tool-git` to retrieve PR context (load the source provider
  reference for PR commands):
  - View PR description, metadata, and author intent
  - View the changeset (diff)
  - View commit messages for intent inference
- No PR or files specified → ask

For all modes, get the full current content of changed files (not just diff hunks)
for the code-reviewer subagent.

## 2. Run mechanical checks

Spawn `qa-runner` subagent to run formatting, linting, and tests.

If mechanical checks fail → report failures. These must be fixed before proceeding
to reasoning review (no point reviewing code that doesn't pass basic checks).

## 3. Run reasoning review

Spawn `code-reviewer` subagent with:
- Full content of changed files
- Context files: entry points, config, shared types touched by the changes
- Relevant review dimensions (see below)
- The plan's milestone spec (milestone review) or full plan (implementation review)

**Partitioning (large reviews only):**
- Under ~50 changed files → single code-reviewer subagent
- Over ~50 changed files → partition by module/component, one subagent per partition
  (up to 4 parallel), orchestrator synthesises findings

## 4. Run plan alignment (plan-based modes only)

The orchestrator handles this directly (not delegated) because it needs the full plan
and cross-cutting view.

**Milestone review:**
- Does the work satisfy the milestone's Deliverable statement?
- Were Approach constraints followed?
- Were all Tasks addressed?

**Implementation review:**
- Trace each MUST requirement to implementing code — flag any not covered
- Trace each SHOULD requirement — flag if not covered and not explicitly deferred
- Verify design decisions were followed (technology choices, code structure, patterns)
- Check for scope creep — changes that don't trace to any requirement

## 5. Synthesise and present

Merge findings from qa-runner, code-reviewer, and plan alignment into a single report.

**Report on two independent axes; do not blend them into one verdict:**
- **Standards axis** — does the code follow project conventions and avoid code smells?
  (qa-runner + code-reviewer quality dimensions)
- **Spec axis** — does the code implement what the plan/milestone asked for?
  (plan alignment)

These are orthogonal: a change can be clean but wrong, or correct but sloppy. Give each
axis its own verdict so one never masks the other.

## 6. Post-review actions

If the verdict is APPROVE or APPROVE WITH SUGGESTIONS and the work is on a feature branch:
- Create a pull request using `tool-git` (load the source provider reference)
- If the plan came from an issue tracker, include the issue identifier in the PR body
- The issue should already be in "In Review" (set by workflow-implement) — no status
  change needed. PR merge handles the final transition.

**Post-PR actions:** After creating the PR, send a Slack notification with the PR title
and link to the project's configured channel (refer to `# Available Tools`). Message
format: `<PR title> <<url>|#<number>>`. If Slack is not configured for the project or
the notification fails, skip silently — notifications are informational, not blocking.

If issue operations fail, warn and continue.

---

# Review Dimensions

The code-reviewer subagent checks these dimensions. The orchestrator selects which are
relevant based on what changed.

**Always check:**
- **Coding standards** — conventions, naming, structure, consistency with existing codebase
- **Error handling** — specific exceptions, appropriate messages, resource cleanup
- **Documentation** — docstrings on public interfaces, comments on non-obvious logic

**Check when relevant:**
- **Architecture** — cross-cutting strategies, module boundaries, dependency flow.
  Always relevant for full codebase reviews or changes spanning multiple modules.
- **API contract** — output format consistency, consumer-appropriateness, token efficiency.
  Always relevant for CLI tools or libraries with external consumers.
- **Security** — input validation, secrets handling, auth, error message leakage.
  Always relevant when changes touch: auth, user input, API endpoints, data access,
  configuration, or external integrations.
- **Performance** — algorithmic efficiency, unnecessary work, N+1 patterns.
  Relevant when changes touch: data processing, database queries, loops over collections,
  hot paths.
- **Technical debt** — shortcuts taken, TODOs introduced, patterns that will cause
  maintenance burden. Flag but don't block.

**Test code** is reviewed differently from production code:
- Are new code paths covered by tests?
- Do tests assert meaningful outcomes (not just exercise code)?
- Are test names descriptive of the behaviour being verified?
- Are mocks/fixtures realistic and not masking real behaviour?

**Trace outward:** when reviewing changed functions or interfaces, check callers and
consumers beyond the diff. A changed function signature with unchecked callers is a
finding. A modified shared type with unconsidered consumers is a finding.

See [references/REVIEW-DIMENSIONS.md](references/REVIEW-DIMENSIONS.md) for detailed
criteria the code-reviewer subagent uses.

---

# Output Format

```
# Review: <description>

Mode: <milestone | implementation | standalone>
Scope: <what was reviewed>
Plan: <plan file, or "none">

## Mechanical Checks
<qa-runner results: pass/fail with details>

## Standards Axis (Code Quality)
<code-reviewer findings, grouped by dimension — conventions, smells, error handling>

## Spec Axis (Plan Alignment) (if applicable)
<requirements coverage, design adherence, scope check>

## Questions
<ambiguous decisions or unclear intent worth discussing>

## Verdict
Standards: APPROVE | APPROVE WITH SUGGESTIONS | REQUEST CHANGES
Spec: APPROVE | REQUEST CHANGES | N/A (no plan)

<brief rationale per axis — do not collapse into a single verdict>
```

**Finding severity:**
- ❌ **Issue** — must fix before merging/continuing
- ⚠️ **Concern** — should fix, but not blocking
- ℹ️ **Suggestion** — improvement opportunity
- ❓ **Question** — needs clarification from the author

---

# Subagent Contract

## qa-runner (existing)

Runs formatting, linting, and tests. Returns pass/fail with details.

## code-reviewer (new)

**Agent:** `code-reviewer`

**Resources (always loaded):** project `CONTRIBUTING.md` and `README.md` — provides
project conventions and setup context.

**Input from orchestrator:**
- Full content of changed files (not diff hunks)
- Context files (entry points, config, shared types as needed)
- Selected review dimensions
- Coding standards: `policy-general-coding` content plus language-specific policy
  (e.g., `policy-lang-python`) — the orchestrator determines which languages are in
  the diff and includes all relevant policy content
- Plan context (milestone spec or design section, if applicable)

**Output:** structured findings per dimension with severity, file references, and
specific quotes of problematic code. Report standards findings (conventions, smells,
error handling) separately from any spec/correctness observations — do not merge them
into a single quality verdict. The orchestrator keeps the two axes distinct downstream.

**Access:** read-only (read, glob, grep, shell, code). Must be able to trace outward
from changed files to check callers and consumers.

**Constraint:** surfaces findings and questions. Does not suggest rewrites or
alternative implementations.

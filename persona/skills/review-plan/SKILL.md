---
name: review-plan
description: >
  Review a spec-level implementation plan for completeness, unresolved decisions, and
  implementability before it reaches an implementor. Use when reviewing a plan before
  implementation, checking a plan for gaps or unresolved decisions, validating a plan is
  ready to hand off, or when asked to "review this plan", "check the plan", or "is this
  plan ready". Delegates the audit to a research subagent with a clean context window and
  read-only codebase access, so claims like "follows the pattern in auth.ts" are verified
  rather than trusted. Not for reviewing code or pull requests (use workflow-review),
  creating or modifying plans (use workflow-plan), or reviewing a roadmap.
---

# Purpose

Audit a spec-level implementation plan for completeness and implementability. Surface gaps,
unresolved decisions, and ambiguities so they can be resolved before the plan reaches an
implementor. The reviewer identifies problems; it does not rewrite the plan or propose fixes.

---

# Scope: which plans this reviews

This skill targets **spec-level** plans — the directly-implementable plans workflow-plan
produces through its four-phase process (Objective/Requirements → Design → Milestones →
Review). It is the automatic quality gate for Phase 4.

- **spec** → full review (this skill).
- **project** → a *lighter audit*, not this full pass: check slices are honest vertical cuts,
  boundaries are clean, and every locked decision is actually load-bearing. workflow-plan runs
  this inline; only reach for this skill's milestone/design criteria if a project plan has
  drifted into spec-level detail.
- **roadmap** → no review (a living index doc).

# When to Use

- Before presenting a spec plan to the user for approval (workflow-plan Phase 4).
- When the user asks to review an existing plan.
- When validating a plan created by someone else.
- Before handing a plan to workflow-implement.

# When Not to Use

- Reviewing code or pull requests (use workflow-review).
- Creating or modifying plans (use workflow-plan).
- Reviewing a roadmap (living doc, no formal review).

---

# Workflow

## 1. Locate the plan

- User specifies a path or tracker ID → use that.
- Plan was just drafted in the current session → use the draft content.
- Ambiguous → ask which plan to review.

## 2. Spawn the review subagent

Delegate to a **research subagent** per the **Subagent contract** below. A clean context
window matters — the reviewer must judge the plan as an implementor in a fresh session
would, not through the lens of the conversation that produced it.

Tell the subagent explicitly: **surface findings only — do not rewrite the plan or suggest
fixes**, and return findings in the Output Format from the criteria doc.

## 3. Return findings

Present the subagent's structured findings to the caller as-is. The caller decides what to do:

- **workflow-plan** (automatic mode): resolves findings, re-reviews if needed.
- **User** (standalone mode): reads findings, decides what to fix.

---

# Integration with workflow-plan

As Phase 4's automatic quality gate:

1. Planner completes Phase 3 (milestones).
2. Invoke this skill with the draft plan.
3. If findings exist: the planner resolves what it can from the codebase/context, and asks
   the user only for what it genuinely cannot answer (one question at a time).
4. Re-run the review. **Maximum 2 review passes** to avoid infinite loops; if findings persist
   after 2 passes, note them when presenting the plan.
5. Present the clean plan to the user.

The user should never see a plan with unresolved decisions that could have been caught.

---

# Subagent contract

- **Type:** research (read-only: read, glob, grep, index, bash). Clean context window,
  separate from the planning conversation.
- **Input** (inline into the prompt):
  - the complete plan content (objective, requirements, design, milestones);
  - the review criteria from [references/REVIEW-CRITERIA.md](references/REVIEW-CRITERIA.md);
  - the project root path, so it can verify codebase claims (referenced files,
    directories, patterns, and dependencies actually exist).
- **Output:** structured findings per the Output Format in the criteria doc.
- **Constraint:** the reviewer surfaces findings. It does not rewrite the plan or suggest fixes.

---

# Example

**User:** "Review plans/031-rate-limiter.md before I hand it to implement."

1. **Locate:** path given → use it.
2. **Spawn** a research subagent per the Subagent contract: inline the full plan, the
   review criteria, and the repo root; instruct it to surface findings only.
3. Subagent verifies claims against the codebase and returns structured findings — e.g.
   *"Milestone 2 references `middleware/throttle.ts`, which does not exist"* and *"the
   Redis-vs-in-memory decision is left open in Design."*
4. **Return** those findings to the user as-is. They decide what to resolve; this skill
   does not propose the fixes.

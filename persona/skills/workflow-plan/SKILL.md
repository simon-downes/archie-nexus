---
name: workflow-plan
description: >
  Turn vague intent into an executable, self-contained plan. Use when the user says
  "let's plan", "plan this", "I want to build...", "how should we approach", "make a
  plan", "roadmap", "break this down", or describes work — of any size — that needs
  design before coding. Not for executing an already-approved plan (use workflow-implement),
  reviewing finished work (use workflow-review), or trivial changes that need no design.
  Guides planning at three levels of scope — roadmap (a list of projects), project (one
  large initiative broken into implementable slices), and spec (a single directly-implementable
  plan) — and selects the appropriate level from the request.
---

# Purpose

Guide the planning process from vague intent to a self-contained artifact that can be
acted on in a future session with zero existing context. Planning happens at **three
levels of scope**; this skill selects the right level, then runs the process at that
resolution.

---

# Planning Levels

The same core process — Objective → Context → grill decisions → decompose — runs at three
resolutions. What differs is **how much decomposition remains before implementation** and
**how deep the decisions go**. Scope, not size, picks the level: a *spec* may be a tiny
feature or one slice of a huge one — what makes it a spec is that it is directly
implementable.

| Level       | Decomposes into      | Decisions resolved                          | Artifact                          |
|-------------|----------------------|---------------------------------------------|-----------------------------------|
| **roadmap** | projects             | sequencing, priorities, top-level shape     | one living roadmap doc            |
| **project** | specs (vertical slices) | load-bearing / architectural decisions locked | a numbered project plan        |
| **spec**    | milestones           | *all* decisions — implementor-ready         | a numbered spec plan (4 phases)   |

They nest: a roadmap item becomes a project; a project slice becomes a spec; a spec's
milestones get implemented. Not every chain starts at the top — most work enters at the
level the user names.

---

# Mode Selection (do this first)

**Infer the level from the request, state your pick in one line, and proceed** — only ask
if genuinely ambiguous. Cues:

- **roadmap** — "waves", "over the next quarter", "list of things we want to build",
  multiple loosely-related initiatives, "where should we go next".
- **project** — "break this down", a single large initiative that clearly needs several
  shippable pieces, "this is too big for one plan", architectural change spanning layers.
- **spec** — "implement X", "plan for PLAT-123", a change describable as one coherent
  deliverable, or a slice promoted from a project.

State it like: `Planning level: **project** (large initiative to decompose into
implementable slices). Say if you'd rather a single spec or a roadmap.` Then continue.

If the work is a **simple bug fix, trivial change, or already fully specified**, say so and
skip planning entirely.

---

# Planning Artifacts

Plans are stored in one of two places depending on whether an issue tracker is available:

**With issue tracker:** the plan lives in the issue description. The issue identifier
(e.g. PLAT-123, #42) is the plan identifier. No local plan files are created. (Roadmaps
stay local regardless — they are living index docs, not units of work.)

**Without issue tracker:** plans are stored as local files in `./plans/` relative to the
project root.

## Local Plan Files (no tracker)

**File naming:** `<NNN>-<type>-<description>.md` where `<NNN>` is a zero-padded
incrementing number and `<type>` is `project` or `spec`.
To determine the next number, check both `./plans/` and `./plans/done/` for the highest
existing number and increment. All levels share one numbering sequence.

- Roadmap: a single living file `./plans/roadmap.md` (no number — it is the index).
- Project: `./plans/<NNN>-project-<description>.md`
- Spec: `./plans/<NNN>-spec-<description>.md`

**Completed plans:** move to `./plans/done/` when fully implemented. The number is
preserved. A project moves to done when all its specs are done.

**Determining the project root:**
1. The nearest ancestor directory containing `.git` (handles sub-projects with own repos)
2. Determine the project root from the project detection tool (refer to `# Available Tools`)
3. If neither is available, ask the user

When working in a sub-project (e.g. a nested module inside a monorepo), the sub-project's
`.git` takes precedence — plans go in the sub-project's `./plans/`.

**Confirm the plan location** before writing when the project root is ambiguous or when
working in a sub-project.

Examples:
- `./plans/roadmap.md`
- `./plans/019-project-orchestrator-control-plane.md`
- `./plans/020-spec-serve-and-ls.md`

---

# The Process by Level

All levels share **Objective**, **Context**, and **decision grilling** (facts are looked
up, only decisions go to the user, one question at a time, each carries a recommendation).
See [references/DECISION-GRILLING.md](references/DECISION-GRILLING.md) — it is the shared
engine for every level.

## Roadmap level

Produce/refresh a living list of projects with just enough shape to sequence them.

1. **Objective + Context** — what the roadmap is optimising for (e.g. "personal daily
   productivity"), and the constraints that order it.
2. **Grill priorities** — resolve sequencing and gating decisions (what's next vs. later,
   what blocks what, what's explicitly excluded). Do **not** design the projects here.
3. **Architectural-risk gate** — flag any item that could force a big architectural change;
   such an item's *decision* should be made now (as a project) even if built later.
4. **Write/refresh `roadmap.md`** — ordered projects, each a 2–5 line entry (objective,
   rough scope, dependencies, status). No milestones, no design.

See [references/ROADMAP.md](references/ROADMAP.md). No formal review phase — the roadmap is
a living doc; revise it as projects land.

## Project level

Take one initiative too large for a single spec and decompose it into implementable
vertical slices, locking only the decisions the slices must all honour.

1. **Objective + Context** — the initiative and why (link the roadmap item if any).
2. **Grill load-bearing decisions** — resolve the *architectural* decisions that constrain
   every slice (the ones expensive to change later). Record them as explicit, numbered,
   locked decisions. Leave slice-local decisions to each spec.
3. **Lightweight design** — only the cross-cutting design needed to justify the locked
   decisions and the slice boundaries. See [references/DESIGN.md](references/DESIGN.md)
   (architectural subset — skip implementation-level sections).
4. **Decompose into slices** — each slice is a **tracer-bullet / vertical slice**
   (end-to-end, independently shippable), *not* a horizontal layer. Each slice will later
   become its own spec via this skill. Split a slice here if it's too big rather than
   allowing one slice to spawn multiple specs.
5. **Write the project plan** — Objective / Context / Locked Decisions / (light) Design /
   Slices / Sequencing / Status table (slice → dedicated spec → status) / Open decisions.

See [references/PROJECT.md](references/PROJECT.md). Review is lighter than spec-level:
audit slices for vertical-cut quality, honest boundaries, and that every locked decision
is actually load-bearing. The project plan is **not implemented directly**.

## Spec level

The full four-phase process below. This is where implementable plans are produced.

---

# Four-Phase Planning Process (spec level)

## Phase 1: Objective + Requirements

**Goal:** Transform vague intent into clear, testable requirements with all ambiguity resolved.

1. **Write Objective** — concise problem description and desired outcome (2-4 sentences)

2. **Capture Context** — document the problem being solved and why. Include:
   - What triggered this work (user observation, bug, conversation insight)
   - Key context from the conversation that led to this plan
   - What was tried or considered before planning began
   - If this spec was promoted from a project, link the project plan and cite the locked
     decisions it must honour.

   This doesn't need to be exhaustive — just enough that an implementor understands
   the "why" without needing the original conversation.

3. **Analyse Initial Input** — identify core objective, what's stated, what's implied, obvious gaps.
   If the user references an existing issue (e.g. "plan for PLAT-123"), fetch the issue to use
   its title, description, and comments as additional context for planning.

4. **Investigate the Codebase (bounded)** — before asking the user anything, explore the
   codebase to answer discoverable questions: existing patterns, conventions, dependencies,
   relevant modules. Do a focused pass — project structure, existing patterns for this type
   of work, and any referenced files — aimed at answering discoverable questions, not
   exhaustively mapping the repo. When a question can't be answered from code, add it to the
   decision tree rather than digging further. For unfamiliar repos, explore by progressive
   disclosure (see `# Available Tools` → "Exploring a codebase") and delegate wide surveys to a research subagent.
   Also check project documentation (README, CONTRIBUTING, AGENTS.md) and determine
   the project configuration (refer to `# Available Tools`). If an issue tracker is
   configured (`issues.provider`), note it for use after plan approval. If no
   tracker is configured, skip issue tracking silently.

   If multiple sources indicate different trackers, prefer: the user's explicit instruction,
   then project configuration, then project documentation.

5. **Grill the Decision Tree** — interview the user relentlessly to resolve every decision
   the design space contains. See [references/DECISION-GRILLING.md](references/DECISION-GRILLING.md)
   for the full method. In brief: look up facts, ask only decisions, one question at a time,
   recommend an answer, follow branches, stop when no design-level decision remains that would
   force the implementor to choose between materially different approaches.

6. **Synthesise Requirements (stop asking, start writing)** — this step is *not* interactive.
   Write up the decisions already resolved in the grilling step. Do NOT re-interview.
   Use RFC 2119 keywords (MUST/SHOULD/MAY), focus on observable behaviour, add testable
   acceptance criteria, group logically. If writing surfaces a genuine gap, re-enter the
   decision tree for *that branch only*, then return to writing.

7. **Iterate Until Approved** — present to user, incorporate feedback, refine until confirmed

**Approval Gate:** "Do the objective and requirements look correct?"

On approval, emit a phase marker before continuing (see Phase Markers below).

See [references/REQUIREMENTS.md](references/REQUIREMENTS.md) for format rules and examples.

---

## Phase 2: Technical Design

**Goal:** Resolve all cross-cutting technical decisions so the implementor never needs to choose
a dependency, decide where code lives, or establish a new pattern.

1. **Identify Cross-Cutting Decisions** — review the requirements and determine what needs
   resolving at the project/feature level (technology choices, structural decisions, patterns
   and conventions, infrastructure, non-functional concerns). See
   [references/DESIGN.md](references/DESIGN.md) for the decision categories and examples.

2. **Resolve from Codebase First** — for existing projects, many decisions are already made.
   Reference existing patterns rather than re-deciding. Call out where this work deviates
   from established patterns and why. If promoted from a project plan, honour its locked
   decisions rather than re-opening them.

3. **Draft Design** — document resolved decisions with rationale. Focus on choices and their
   reasoning, not system descriptions. Keep it concise.

4. **Review with User** — present design, iterate based on feedback

**After approval, emit a phase marker, then automatically proceed to Phase 3.**

---

## Phase 3: Milestones

**Goal:** Break work into incremental, testable deliverables (each a **tracer bullet** — a
thin **vertical slice** cutting through every layer end-to-end) with enough technical context
that an implementor in a fresh session can execute without re-discovering decisions.

**Fog of war:** only create a milestone for work you can *state precisely now*. The test is
whether you can state the objective and deliverable precisely — not whether you can answer
every question inside it yet. Work you suspect is needed but can't yet sharpen goes in a
trailing **Not yet specified** section, not into a vague milestone. Sharpen and promote it
later rather than inventing detail now.

Each milestone has four required sections and two conditional sections:

- **Approach** — technical context that shapes how the work is done: which libraries/patterns
  to use, where in the codebase this fits, constraints, ⚠️ gotchas, and the **test seam**
  (the boundary at which behavioural changes get tested). The "how and why."
- **Wiring** *(when milestone introduces shared state or cross-module coordination)* —
  data flow: what state is created, who mutates it, who reads it, what call sites look like.
- **Edge Cases** *(when milestone handles external input or has failure modes)* —
  non-happy-path scenarios with decided behaviour. One line each: scenario → behaviour.
- **Tasks** — concrete units of work, in roughly execution order. The "what gets done."
- **Deliverable** — single testable outcome. What's true when this milestone is complete.
- **Verify** — how to confirm the deliverable (a command, test, or observable behaviour) —
  including HOW to observe, not just what.

**Before presenting milestones, audit each one:**
- Would the implementor need to choose a library/tool, decide where new code lives, or
  establish a new pattern? → resolve it in Approach.
- Is this a **vertical slice** (end-to-end observable behaviour) rather than a horizontal
  one (a single layer)? Restructure horizontal slices into vertical ones. **Exception:** a
  **prefactor** ("make the change easy, then make the easy change") is a legitimate
  infrastructure-only slice — label it as such.
- Shared state / cross-module data flow? → add Wiring. External input / failure modes? →
  add Edge Cases.
- Are Tasks specific enough to track but not so detailed they prescribe code? Does Verify
  give a concrete way to confirm the deliverable, including how to observe?

**After completing milestones, emit a phase marker, then automatically proceed to Phase 4.**

See [references/MILESTONES.md](references/MILESTONES.md) for the milestone format template,
full rules, and worked examples.

---

## Phase 4: Review

**Goal:** Verify the plan is complete and implementable before presenting to the user.

1. **Run review** — invoke `review-plan` with the draft plan. The reviewer audits
   against quality criteria in a clean context window.

2. **Resolve findings** — for each finding:
   - Check the codebase first (can you answer it from code?)
   - If not, ask the user (one question at a time)
   - Update the plan to address the finding

3. **Re-review if needed** — if findings were resolved, run the review again. Maximum 2
   review passes. If findings persist after 2 passes, note them when presenting the plan.

4. **Present complete plan** — show all four phases together.

**Approval Gate:** "Here is the complete plan. Shall we move to Implementation Mode?"

5. **Persist the plan** (see Planning Artifacts for location rules):
   - **With tracker:** create or update the issue with the full plan as its description; the
     issue identifier becomes the plan identifier (no local file). See
     [references/ISSUE-FORMAT.md](references/ISSUE-FORMAT.md) for operations. On failure,
     fall back to a local file.
   - **Without tracker:** write the plan to a local file in `./plans/`.
   - If this spec was promoted from a project, update the project plan's Status table
     (slice → this spec's identifier → planned).

---

## Key Principles

- **Plan as artifact** — the plan must be self-contained. An implementing agent in a fresh
  session with zero context should be able to execute it. Include the problem context
  so the implementor understands *why*, not just *what*.
- **Resolve decisions, don't defer them** — the plan must resolve all dependency, tooling,
  and structural decisions. The implementor should never need to choose a library, decide
  where new code lives, or establish a new pattern. (At project level, resolve only the
  *load-bearing* decisions and defer slice-local ones to each spec.)
- **Codebase first** — before asking the user a question, check if the answer is discoverable
  from existing code, docs, or manifests.
- **One question at a time** — when you do need user input, ask one focused question with
  your recommended answer and alternatives.
- **Grill facts vs. decisions** — look up facts; only put decisions to the user.
- **Right level, right resolution** — don't over-specify a roadmap or project; don't
  under-specify a spec.

---

## Phase Markers

At each phase transition, emit a terse marker. This is not for the user — it re-anchors the
workflow state so it survives context compaction. Keep it to 3-4 lines:

```
---
Level: spec | Phase: 2 (Technical Design)
Approved: Objective + Requirements ✓
Pending: cross-cutting design decisions
---
```

For roadmap/project levels, mark the analogous step (e.g. `Level: project | Step: decompose
into slices`).

---

# Example

**User:** "I want to add rate limiting to the API"

1. Select level: a single directly-implementable feature → **spec**.
2. Phase 1 (Objective + Requirements): confirm goal and constraints; grill facts from the
   codebase (existing middleware, Redis availability) before asking the user anything.
3. Phase 2 (Technical Design): put load-bearing decisions to the user one at a time
   (store, key strategy, limit response behaviour) with a recommendation each.
4. Phase 3 (Decompose): break into dependency-ordered milestones, each with one testable
   deliverable.
5. Phase 4 (Review): audit the plan, then write `./plans/NNN-spec-rate-limiting.md`.

For a larger request ("build a billing system"), select **project**, resolve only
load-bearing decisions, and decompose into specs. For "what should we build next quarter",
select **roadmap**.

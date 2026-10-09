# Plan review

Reviewer instructions for a build-it plan. You audit the plan as an implementor in a fresh
session would. You have the plan file and the codebase, and nothing else. Report findings only:
do not edit the plan or any other file, and do not propose fixes.

## Contents

- Checks 1–6
- Output format

The plan follows `references/PLAN-FORMAT.md` and the **Plan rules** section of the build-it
skill's SKILL.md. Skip any check that doesn't apply (for example, no data model).

---

## Checks

### 1. Objective and requirements

- Is the objective specific? Could two readers picture different outcomes?
- Does every MUST have an AC that a test or command can check?
- Are there unmeasured adjectives (fast, secure, robust, properly)?
- Do any requirements contradict each other?

### 2. Decisions

- Does every Decisions row name a rejected option?
- Does every "Why" survive the anti-generic test? A reason that fits almost any project
  ("simpler", "standard") is a finding.
- Is any choice left vague ("use a cache", "add validation") with no named tool or approach?

### 3. Codebase claims

Verify against the repository:
- referenced files and directories exist (or are marked as new);
- referenced patterns match what the code actually does;
- referenced dependencies are in the manifest (or marked as new);
- named check and test commands exist (justfile, pyproject, package.json, Makefile).

For each claim that fails, name the closest real match if there is one.

### 4. Design

- Does each included Design subsection meet its "done when" bar in PLAN-FORMAT.md?
- Is a subsection missing that the work clearly needs (error handling for external calls,
  config for new env vars)?
- Does every named failure have a named response? "Handle errors gracefully" is a finding.

### 5. Milestones

For each milestone:
- Are Approach, Test seam, Tasks, Deliverable, and Verify present? Are Wiring and Edge cases
  present where shared state or external input or failure modes exist?
- Is it one deliverable, observable, and vertical? A layer-only milestone not labelled
  prefactor is a finding.
- Is M1 (after any prefactor) a tracer bullet: the core behaviour, end to end, as thin as
  possible? A first milestone that builds a peripheral feature, or the core one layer at
  a time, is a finding.
- Would the implementor have to choose a library, decide where code lives, invent a
  pattern, or look up something the plan could have stated? Each of these is a finding.
- Does Verify say how to observe the result (a command and the passing result)?
- Do Approach and Tasks overlap, or do two milestones do the same work?

### 6. Coherence

- Does every MUST trace to a milestone? Does every milestone trace to a requirement?
- Do Design and milestone Approaches contradict each other?
- Is the ordering by dependency?

---

## Output format

```
## Plan review: <plan title>

Verdict: ✅ Clean | ⚠️ Findings | ❌ Significant gaps

### <Check name>
- ❌ **<Location>**: <finding, quoting the plan text>
- ⚠️ **<Location>**: <finding>
- ℹ️ **<Location>**: <finding>
```

- Location is one of: `Objective`, `Requirements`, `Decisions D#`, `Design: <subsection>`,
  `M#`, `M#, <field>`.
- ❌ Gap: information the implementor will need is missing, or a decision is unresolved.
- ⚠️ Concern: ambiguity or a probable problem.
- ℹ️ Note: a smaller improvement. The orchestrator fixes every finding it agrees with.
- Report only checks that have findings. One finding is one problem. If there are no
  findings, write `Verdict: ✅ Clean` and one sentence saying what you verified.

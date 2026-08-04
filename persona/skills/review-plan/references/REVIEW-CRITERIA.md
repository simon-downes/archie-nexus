# Plan Review Criteria

## Purpose

Defines what the review subagent checks in a spec-level plan and how it reports findings.

## How to use these dimensions

Work through each dimension in order. **Skip dimensions that don't apply** to the plan (e.g.
skip Data Model checks if the plan has no data model; skip Non-Functional if the work has no
security/performance surface).

Several checks reference the **Done-when** bars and the **anti-generic-rationale test** that
the plan's DESIGN sections were authored against. Don't re-derive those bars here — check that
the written prose actually clears them. A section that can't clear its own Done-when bar is a
finding.

**Deferred work is not a gap.** Work honestly parked in a milestone's "Not yet specified"
section (workflow-plan's fog-of-war rule) is intentional, not missing. Only flag it if it is
load-bearing for an *earlier* milestone, or if it's phrased as a committed milestone while
still vague.

---

### 1. Objective and Requirements

- Is the objective clear and specific?
- Are requirements testable (do acceptance criteria exist)?
- Are RFC 2119 keywords used consistently (MUST/SHOULD/MAY)?
- Are there requirements that contradict each other?

---

### 2. Technical Design — Decisions

For each design section present in the plan:

- Are technology choices explicit (named libraries, tools, versions)?
- Does each **new dependency name why it beat the alternative**, and say whether it's new or an
  existing dependency reused? (Technical Stack Done-when bar.)
- Do **Key Decisions** name both the option chosen *and* the option rejected, with each
  rationale surviving the **anti-generic test** — a reason that would apply to almost any
  project ("simpler", "more scalable", "industry standard") is a finding?
- Are there vague references that need specifics? (e.g. "use a caching solution" — which one?)

---

### 3. Technical Design — Codebase Claims

Verify claims against the actual codebase (this is why the reviewer has read-only access):

- Referenced files exist (e.g. "follows pattern in src/middleware/auth.ts").
- Referenced directories exist (e.g. "add to src/services/").
- Referenced patterns are accurate (e.g. "existing middleware uses the Express pattern").
- Referenced dependencies are actually in the project's manifest.

Flag claims that cannot be verified (file not found, pattern doesn't match) — name the closest
actual match where one exists.

---

### 4. Technical Design — Error Handling & Non-Functional

Check these only when the plan includes the corresponding design sections (or clearly should,
given external calls, shared mutable state, or a security/performance surface).

**Error Handling & Edge Cases:**
- For each external call or multi-step operation, is the failure behaviour named (retried,
  rolled back, or left intermediate)? "Handle errors gracefully" is a finding — a named
  trigger → named response passes.
- Is idempotency stated for operations that must be safely retryable (key, natural, or dedup)?

**Non-Functional Concerns:**
- Does each concern name a **number or a named mechanism**, not a placeholder? "Fast enough" /
  "we'll monitor it" / "handle appropriately" are findings; "p95 under 200ms at 500 req/s",
  "log denials at warn", "fail open on Redis failure" pass.

---

### 5. Milestones — Completeness

For each milestone:

- Are the required sections present — **Approach, Tasks, Deliverable, Verify**?
- Are the conditional sections present *when triggered*?
  - **Wiring** when the milestone introduces shared state or cross-module coordination.
  - **Edge Cases** when the milestone handles external input or has failure modes.
- Is the **Deliverable** a single testable outcome (not compound)?
- Does **Verify** give a concrete way to confirm *and say HOW to observe it* (command, test,
  or observable behaviour) — not just what to observe?
- Where the milestone changes behaviour, does Approach name the **test seam** (the boundary at
  which it's tested)?
- Are Tasks specific enough to track progress without prescribing exact code?

---

### 6. Milestones — Unresolved Decisions

The core check. For each milestone, ask:

- Would the implementor need to **choose a library or tool** not specified in the plan?
- Would the implementor need to **decide where new code lives** (file, module, directory)?
- Would the implementor need to **establish a new pattern** not referenced in the plan?
- Would the implementor need to **look up how something works** that the plan could have explained?

Each "yes" is a finding.

---

### 7. Milestones — Structure

- **Vertical slice check:** does each milestone deliver end-to-end observable behaviour, or is
  it a horizontal layer (all types, or all config, with nothing observable)? A horizontal slice
  is a finding — *unless* it's explicitly labelled a **prefactor** ("make the change easy, then
  make the easy change"), which is a legitimate infrastructure-only slice.
- Do milestones follow dependency order (foundational before dependent)?
- Does **Approach** contain only context/guidance (not tasks)?
- Do **Tasks** contain only work items (not context/guidance)?
- Does **Wiring** contain data-flow only (state, producers, consumers, call sites)?
- Is **Edge Cases** one line each, scenario → decided behaviour?
- Is there overlap between sections within a milestone, or between milestones (same work twice)?

---

### 8. Coherence

Cross-cutting checks across the whole plan:

- Do milestones cover all requirements? Trace each MUST requirement to at least one milestone.
- Does the design support the requirements (none left unaddressed)?
- Are there milestones that don't trace back to any requirement?
- Are there contradictions between design decisions and milestone approach?
- Requirements intentionally deferred should be *noted as such* (not silently dropped) — an
  unaddressed MUST with no note is a finding; a noted deferral is not.

---

## Output Format

```
## Plan Review: <plan name or objective>

### Verdict: ✅ Clean | ⚠️ Findings | ❌ Significant gaps

### Findings

#### <Dimension name>

- **<Location>**: <finding>
  <brief explanation of why this is a problem>
```

**Location format:**
`Objective` / `Requirements` / `Design: <section>` / `Milestone N` /
`Milestone N, Approach` / `Milestone N, Task M`

**Finding severity:**
- ❌ **Gap**: missing information the implementor will need.
- ⚠️ **Concern**: ambiguity or potential issue worth flagging.
- ℹ️ **Note**: minor observation, not blocking.

**Rules:**
- Only report actual findings — omit dimensions with no issues.
- Be specific: quote the problematic text, name the missing decision.
- Don't suggest fixes — just identify the problem.
- Don't rewrite plan content.
- Keep findings concise (one finding = one problem).

**Clean plan example:**
```
## Plan Review: API Rate Limiting

### Verdict: ✅ Clean

No findings. All decisions resolved, milestones are vertical slices tracing to
requirements, codebase references verified.
```

**Findings example:**
```
## Plan Review: User Authentication

### Verdict: ⚠️ Findings

### Findings

#### Unresolved Decisions

- ❌ **Milestone 2, Approach**: "use a session store" — which session store? Redis,
  database-backed, in-memory? Not specified in Design: Technical Stack.

- ❌ **Milestone 3, Task 2**: "add validation" — no validation library chosen. Design
  should specify zod, joi, or manual validation.

#### Design — Decisions

- ⚠️ **Design: Key Decisions**: "chose Postgres because it's more scalable" — fails the
  anti-generic test; no rejected option named and the reason applies to any project.

#### Error Handling & Non-Functional

- ❌ **Design: Non-Functional**: "auth should be performant" — no number or condition. Needs
  a latency/throughput target at a stated load.

#### Codebase Claims

- ⚠️ **Design: Code Structure**: references src/middleware/auth.ts but the file does not
  exist. Closest match: src/auth/middleware.ts.

#### Structure

- ⚠️ **Milestone 1**: delivers "all type definitions and DB schema" with nothing observable —
  a horizontal slice, not a vertical one. Restructure, or label as a prefactor if intended.

- ⚠️ **Milestone 2, Approach**: "Create the user model and add password hashing" is a task,
  not approach context. Move to Tasks.

#### Coherence

- ℹ️ **Requirement 4** (SHOULD support OAuth): not addressed in any milestone and not noted as
  deferred. Intentional deferral should be stated.
```

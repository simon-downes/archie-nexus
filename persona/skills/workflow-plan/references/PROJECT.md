# Project Level

A project is **one initiative too large for a single spec**, decomposed into
independently-implementable **vertical slices**. The project plan locks only the
decisions every slice must honour — the load-bearing / architectural ones — and defers
everything slice-local to the specs.

**Artifact:** `./plans/<NNN>-project-<description>.md` (shares the incrementing sequence
with specs). A project plan is **not implemented directly**: each slice becomes its own
spec (via this skill) and *that* gets implemented. A project moves to `./plans/done/` when
all its specs are done.

**Exemplar:** `archie-nexus/plans/019-*-orchestrator-control-plane.md` — a real project
plan whose slices (M1, M2a, M2b, M3–M6) are the spec children.

---

## Process

Runs the shared engine (see [DECISION-GRILLING.md](DECISION-GRILLING.md)) at
architectural resolution. You grill for the decisions that constrain *all* slices; you
deliberately leave slice-local choices unresolved.

1. **Objective + Context.** The initiative and why. If it came from a roadmap item, link
   it. Capture what triggered it and what's been tried/considered.

2. **Grill load-bearing decisions.** Resolve the *architectural* decisions — the ones
   expensive to change once slices start landing, or that every slice must agree on
   (data ownership, key interfaces/boundaries, cross-cutting mechanisms, where major
   pieces live). Record them as explicit, **numbered, locked** decisions (D1, D2, …).
   **Do not** resolve slice-local decisions here — that is each spec's job, and doing it
   now over-constrains the slices.

3. **Lightweight design.** Only the cross-cutting design needed to *justify* the locked
   decisions and the slice boundaries. Use the architectural subset of
   [DESIGN.md](DESIGN.md) (overview, architecture, components, key decisions, risks) and
   **skip** the implementation-level sections — those belong to specs.

4. **Decompose into vertical slices.** Each slice is a **tracer bullet** — end-to-end
   through every layer, independently shippable, delivering observable value — *not* a
   horizontal layer ("all the types", "all the config"). If a slice is too big, split it
   *here* into two slices rather than letting one slice later spawn multiple specs.
   **Invariant: one slice → one spec.** Name each slice's objective, rough boundary, which
   locked decisions it owns/exercises, and its dependencies on other slices.

5. **Write the project plan** (sections below).

---

## Review (lighter than spec Phase 4)

No `action-review-plan` pass. Instead, self-audit:
- **Vertical-cut quality** — is each slice genuinely end-to-end and independently
  shippable, or a disguised horizontal layer?
- **Honest boundaries** — do the slice dependencies reflect real ordering, or wishful
  parallelism? Is the sequencing sound?
- **Load-bearing-ness** — is every locked decision actually load-bearing? A decision that
  only one slice cares about should be *unlocked* and pushed down to that spec.
- **One slice → one spec** holds for every slice.

---

## Format

```markdown
# <NNN> Project: <name>

## Objective
<2–4 sentences: the initiative and desired outcome.>

## Context
<Why now, what triggered it, prior art. Link the roadmap item if any.>

## Locked Decisions
These constrain every slice; specs must honour them and not re-open them.
- **D1: <decision>** — <the choice> · <one-line rationale>
- **D2: <decision>** — …

## Design (cross-cutting only)
<Just enough architecture to justify the locked decisions and slice boundaries.
Skip implementation detail — that lives in each spec.>

## Slices
1. **M1 — <objective>**
   - Boundary: <what's in this slice, what's notably out>
   - Owns / exercises: D1, D3
   - Depends on: nothing
2. **M2 — <objective>**
   - Boundary: …
   - Depends on: M1
   …

## Sequencing
<The order slices should be built and why; note conceptual vs. hard dependencies.>

## Status
| Slice | Spec | Status |
|-------|------|--------|
| M1    | `020-spec-<slug>.md` | planned |
| M2    | —    | not started |

## Open decisions (deferred to specs)
<Decisions intentionally left unresolved, noted so nobody mistakes them for oversights.>
```

Guidelines:
- **Locked ≠ everything.** The whole point is a *thin* set of decisions. When in doubt,
  leave it to the spec.
- **The Status table is the graduation ledger.** As each slice is promoted to a spec, fill
  in the spec identifier and status; the project is done when every row is done.
- **Fog of war applies to slices too.** If a later slice can't yet be stated precisely,
  give it a placeholder objective and sharpen it when its predecessors land, rather than
  inventing detail now.

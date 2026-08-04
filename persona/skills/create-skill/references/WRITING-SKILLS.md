# Writing Great Skills

The craft layer for `create-skill`: how to write a skill body that is tight, correctly
scoped, and doesn't rot. Load this when authoring or restructuring a skill body.

*Adapted from Matt Pocock's writing on skills (via the PAI project's
`WritingGreatSkills.md`) and the vendor-neutral skills guidance in eos.*

---

## Two kinds of load

A skill imposes two costs, and you trade them off deliberately:

- **Context load** — tokens the skill puts in the window. Cheap to add, but it
  accumulates and crowds out everything else.
- **Cognitive load** — attention the agent must spend parsing and holding the skill.
  A long, hedged, meandering skill is expensive even if the token count looks fine.

Write for both: the shortest text that reliably changes behaviour.

---

## Progressive disclosure and the branching test

Organise a skill as a ladder: description → body → reference files. Each rung is loaded
only when the previous one points to it.

Decide what goes where with the **branching test**:

- **Inline it** (in the body) if *every* path through the skill needs it.
- **Make it a pointer** (a `references/` file) if only *some* paths reach it.

This keeps the body small and the deep detail available without paying for it upfront.

---

## Completion criteria

The strongest instructions are both **clear** (unambiguous about what "done" means) and
**demanding** (checkable and exhaustive, so the agent can't declare victory early).

- Prefer criteria the agent can verify against reality over vague aspirations.
- Enumerate what must be true at the end, not just what to attempt.

---

## Leading words

Word choice steers behaviour. Precise, forceful leading words tighten execution:

- `tight`, `exhaustive`, `relentless` raise the bar the agent holds itself to.
- `red` / a hard stop signals a non-negotiable gate.
- Vague verbs ("handle", "deal with", "consider") invite the agent to do the minimum.

Use strong words where you mean them; don't inflate everywhere or they stop meaning
anything.

---

## Failure modes to design against

- **Premature completion** — the agent stops before the work is truly done. Cure with
  exhaustive, checkable completion criteria.
- **Duplication** — the same instruction in two places drifts apart. Keep a single
  source of truth; point instead of copy.
- **Sediment** — stale lines accumulate over edits. Prune every time you touch a skill.
- **Sprawl** — a skill that tries to do everything triggers on nothing cleanly. Split it.
- **No-op** — a skill that only restates what a capable model already does. Delete it.
- **Negation** — "don't do X" is weaker than stating the positive. Prefer "do Y".

---

## Pruning and single source of truth

Editing a skill is the moment to remove what no longer earns its place. Every line
should change behaviour; if it merely reassures or restates, cut it. When two skills or
a skill and the prompt overlap, one of them owns the truth and the other points to it —
never maintain two copies.

# Roadmap Level

A roadmap is a **living index of projects** — the top-level, deliberately-vague view of
where the work is going. It is optimised for *sequencing and priority*, not design. It is
never implemented directly; its entries graduate into project plans (or straight into
specs when small enough).

**Artifact:** a single living file `./plans/roadmap.md`. No number — it is the index, not
a unit of work. Revise it in place as projects land; there is no formal review phase.

---

## Process

Runs the shared engine (see [DECISION-GRILLING.md](DECISION-GRILLING.md)) at the coarsest
resolution. You are grilling for *ordering and gating*, not project internals.

1. **Objective + Context.** What the roadmap optimises for (e.g. "personal daily
   productivity", "get to a shippable v1") and the constraints that order it (time,
   dependencies, appetite, external deadlines).

2. **Grill priorities.** Resolve the sequencing and gating decisions: what's next vs.
   later, what blocks what, what is explicitly excluded (and why). Do **not** design the
   projects — resist the pull to specify how each will work. If you find yourself
   resolving implementation choices, you've dropped a level.

3. **Architectural-risk gate.** Scan the items for any that could force a large,
   expensive-to-reverse architectural change. Such an item's *decision* should be made
   now — promote it to a project and grill its load-bearing decisions early — even if the
   building happens much later. Cheap-to-change items can stay vague.

4. **Write / refresh `roadmap.md`.** An ordered list of projects. Each entry is 2–5 lines:
   objective, rough scope, dependencies, status. No milestones, no design, no locked
   decisions.

---

## Format

```markdown
# Roadmap

_What this roadmap optimises for: <one line>._

## Now
1. **<Project name>** — <one-line objective>.
   - Scope: <rough boundary — what's in, what's notably out>
   - Depends on: <other projects / external facts, or "nothing">
   - Status: in progress → `019-project-<slug>.md`

## Next
2. **<Project name>** — <objective>.
   - Scope: …
   - Depends on: #1
   - Status: not started

## Later
3. **<Project name>** — <objective>.
   - Status: idea

## Explicitly not doing (for now)
- <thing> — <why deferred/excluded>
```

Guidelines:
- **Order matters** — the sequence *is* the decision. Group loosely (Now / Next / Later)
  rather than pretending to a precise backlog.
- **Link out, don't inline.** When an item becomes a project, its entry links to the
  project plan (`NNN-project-*.md`); the roadmap stays a thin index.
- **Status is a pointer, not a tracker.** Keep it to a word plus a link. The project plan
  owns detailed status.
- **Keep entries small.** If an entry needs more than ~5 lines to be understood, it's
  really a project — go plan it at project level.

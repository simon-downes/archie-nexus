# Decision Grilling

The shared interview engine used by **every** planning level (roadmap, project, spec).
Its job is to drive out the decisions that shape the plan — and *only* the decisions —
so the resulting artifact contains no unresolved choice that would force whoever acts on
it to guess.

What "a decision" means shifts by level, but the method does not:

| Level   | Decisions you are grilling for                                    |
|---------|-------------------------------------------------------------------|
| roadmap | sequencing, priorities, what's in/out, what gates what            |
| project | load-bearing / architectural decisions every slice must honour    |
| spec    | *all* remaining decisions — down to implementor-ready              |

---

## The core loop

**Map the space before you walk it (breadth-first, then depth-first).** First enumerate —
for yourself, not as questions — the full set of decisions this level must resolve; fan
out across the *whole* initiative before drilling into any one branch. This matters most
at **project** level, where the goal is to surface *all* the load-bearing decisions: don't
tunnel into the first interesting decision and miss the others. Keep this map (a short
checklist of open decisions) and work through it; add to it as answers reveal new
decisions, tick items off as they're settled. The map is breadth-first analysis; the
questions you actually ask the user are depth-first, one at a time. "One question at a
time" governs the *conversation*, not your *analysis* — never let it collapse your view of
the space to a single branch.

1. **Look up facts first.** Before asking anything, resolve every discoverable question
   from code, docs, manifests, issue trackers, or the existing roadmap/project plan. A
   fact is anything with a knowable answer that doesn't require a human preference or a
   judgement call. **Never ask the user something you can find out yourself.**

2. **Ask only decisions.** Put a question to the user only when it is a genuine choice
   between materially different options that you cannot settle from facts. If two paths
   lead to the same place, pick one and move on — don't manufacture a decision.

3. **One question at a time.** Ask a single focused question, then wait. Don't batch a
   questionnaire at the user. (This is an *interaction* rule — you still hold the whole
   decision map in view; you're just resolving it one exchange at a time.) Each answer
   reshapes the map and may prune later questions.

4. **Always carry a recommendation.** Every question ships with your recommended answer
   and the realistic alternatives, plus a one-line *why*. The user should be able to
   reply "yes" and move on. You are steering, not surveying.

5. **Follow the branches — but don't lose the map.** An answer opens new decisions and
   closes others; walk the tree it creates rather than reading from a frozen list. After
   a branch is settled, return to the map and pick the next most consequential / most
   blocking *open* decision — which may be in a different part of the space, not deeper in
   the one you just resolved. Depth-first resolution, breadth-first coverage.

6. **Know when to stop.** Stop when no decision remains that would force the actor to
   choose between materially different approaches *at this level*. For a spec that means
   implementor-ready; for a project it means every slice is unblocked on the load-bearing
   choices (slice-local decisions are deliberately left to each spec); for a roadmap it
   means the ordering and gating are settled.

---

## Rules of engagement

- **Facts vs. decisions.** The single most important discipline. Looking things up is
  free and builds trust; asking answerable questions burns it.
- **Recommend, don't survey.** A bare "what do you want?" offloads the thinking you were
  brought in to do. Bring an opinion.
- **Surface conflicts, never resolve them by assumption.** When two answers (or a new
  answer and an earlier one) contradict, name the contradiction, show both, explain why
  they can't both hold, and ask the user to choose. Do not silently pick one.
- **Validate understanding at transitions.** Before leaving the grilling step, summarise
  the decisions reached, state any assumptions you're relying on, and check nothing
  material was missed.
- **Probe deeper when** a choice is vague or subjective, when you can't picture how you'd
  verify the outcome, when edge cases or failure modes are unclear, or when the user uses
  an ambiguous term. If you can't picture how to test it, it isn't decided yet.
- **Don't over-grill for the level.** At roadmap/project level, decisions that are
  legitimately slice-local or implementation-detail are *out of scope* — deferring them
  is correct, not lazy. Grill to the level's resolution and no further.

---

## Stop / continue checklist

Continue grilling if any of these is true:
- A remaining choice would send the actor down materially different paths.
- A decision at this level's depth is still open (see the table above).
- **You haven't yet swept the whole space** — an item on your decision map is still
  unexamined (especially at project level: have you checked *every* slice/area for
  load-bearing decisions, not just the ones you drilled into first?).
- Two resolved answers conflict.

Stop grilling and start writing when:
- Every level-appropriate decision has a settled answer.
- Remaining unknowns are genuinely lower-resolution (correctly deferred to the next level
  down) or are facts to be discovered during the work, not choices.

Once you stop, **stop asking** — move to synthesis/writing. If writing surfaces a real
gap, re-enter the tree for *that branch only*, then return to writing.

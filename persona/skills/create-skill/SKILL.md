---
name: create-skill
description: >
  Author or revise a skill: a reusable, on-demand capability package for the agent.
  Use when asked to "create a skill", "make a skill", "add a skill", "turn this into
  a skill", or to "update"/"fix"/"improve" an existing skill. Not for one-off
  instructions (just say them), deterministic operations (write a script), or basic
  reasoning any model already does without procedure.
---

# Purpose

Produce a skill that earns its place: a self-contained package of expertise the agent
loads only when a specific situation arises. A good skill triggers reliably on the
right situation, stays silent on adjacent ones, and gives just enough procedure to act
without re-deriving it every time. This skill covers both **creating** new skills and
**modifying** existing ones.

---

# When to Use

- Creating a new reusable capability the agent should reach for in a recurring situation
- Turning a proven workflow, checklist, or hard-won procedure into something repeatable
- Updating, fixing, or sharpening an existing skill (trigger, procedure, or scope)
- Splitting or merging skills when their responsibilities have drifted

# When Not to Use

Prefer a lighter mechanism when one fits — a skill is the heaviest option:

- **A fact or preference** → put it in scoped instructions or the prompt, not a skill.
- **A deterministic operation** → write a script or CLI command; skills are for judgment.
- **Basic reasoning any model already does** → don't wrap general competence in
  procedure. Skills carry knowledge, tools, and *this project's* specific conventions —
  not a narration of ordinary thinking. (This is about the task, not the model: a skill
  worth having is one that adds project-specific substance even a strong model lacks.)
- **A one-off** → just give the instruction directly.

---

# Principles

- **Trigger on relevance.** The description is the whole activation mechanism. Front-load
  the situations that should fire it; name the adjacent situations that must *not*.
- **Progressive disclosure.** SKILL.md body holds what every use needs. Push detail only
  some paths reach into `references/` files, loaded on demand.
- **Earn the load.** Every skill and every line costs context. Include what changes the
  agent's behaviour or carries project-specific substance; cut generic filler.
- **Single source of truth.** Don't restate what another skill or the prompt owns.
  Point to it. Duplication rots as one copy drifts from the other.
- **Evidence over invention.** Never fabricate flags, paths, or APIs. If the source
  doesn't establish it, don't put it in the skill.

---

# Creation Workflow

## 1. Confirm a skill is the right tool

Run the *When Not to Use* gate above. If a fact, script, or one-off instruction fits
better, say so and stop. Only proceed if the capability is a recurring, judgment-bearing
situation the model can't just reason through.

## 2. Check for collisions

List existing skills in `<persona>/skills/`. If one already covers this ground, prefer
**updating** it (see *Modification Workflow*) over adding a near-duplicate. If two skills
would fire on the same situation, sharpen the boundary between them.

## 3. Define the contract

Before writing, pin down the eight points in the **Validation Checklist** below —
especially the trigger and the neighbouring situations that must *not* trigger it.
Ambiguity here is what makes skills misfire.

## 4. Write the description

The description is a `USE WHEN … NOT FOR …` grammar, front-loaded because it may be
truncated:

- **USE WHEN**: the situations that should activate it, OR-joined. Include the literal
  phrases a user would say ("create a skill", "turn this into…").
- **NOT FOR**: the confusable adjacent situations it must stay out of.

Keep the decisive triggers in the first ~60 characters — assume the tail gets cut.

## 5. Write the body

Follow the house style defined in `references/STRUCTURE.md`: the section order, the
supporting directories (`references/`, `scripts/`, `assets/`), and the conventions every
skill obeys. Use imperative, token-efficient prose. Reference tools via
`# Available Tools` rather than hardcoding names. Keep in the body what every use needs;
move path-specific depth to `references/`.

For the craft of writing a tight, well-scoped body, read `references/WRITING-SKILLS.md`.

## 6. Add an example

One concrete walk-through of the skill firing and running end to end. Examples resolve
ambiguity that prose can't.

## 7. Validate

Run the **Validation Checklist**. Fix every gap before proposing.

## 8. Get approval, then write

Present the proposed skill (description + body outline) for approval. On approval, write
to `<persona>/skills/<name>/SKILL.md` and any `references/` files. Name the directory in
plain lowercase-hyphenated form (`create-skill`, `review-plan`) — no dates, session, or
incident IDs.

---

# Modification Workflow

Updating an existing skill:

1. **Read it whole** — SKILL.md and every `references/` file. Understand what it owns.
2. **Locate the change** — is it the trigger (description), the procedure (body), or
   depth (references)? Change the smallest surface that fixes the problem.
3. **Re-validate** — run the full checklist; a change to one part often breaks another
   (e.g. a new trigger phrase that now collides with a neighbour).
4. **Prune** — delete what the change made redundant. Skills accumulate sediment;
   editing is the time to remove it.
5. **Get approval, then write.**

---

# Validation Checklist

An effective skill answers all eight — its contract:

1. **Trigger** — what situation activates it? (Present in the description?)
2. **Neighbours** — which adjacent situations must *not* trigger it? (Named in *NOT FOR*?)
3. **Inputs** — what does it need to run?
4. **Steps** — the procedure, in order.
5. **Tools & side-effects** — what it uses and what it changes (files, commits, state).
6. **Output** — what it produces.
7. **Success / partial failure** — how to tell it worked; what to do when it half-worked.
8. **What to load when** — body vs. which reference file, so nothing over- or under-loads.

Also confirm: no duplication of other skills or the prompt; no invented flags/paths/APIs;
description survives truncation; plain lowercase-hyphenated name.

---

# Example

**User:** "We keep doing the same dance to cut a release. Turn it into a skill."

1. **Gate:** recurring and judgment-bearing (version bumps, changelog, tag decisions) —
   not a pure script. A skill fits. Proceed.
2. **Collisions:** no existing `release` skill; `workflow-implement` doesn't cover it.
3. **Contract:** trigger = "cut a release"/"ship a version"; must *not* fire on ordinary
   commits or PR review; inputs = target version, changelog source; output = tagged
   release + notes.
4. **Description:** `USE WHEN asked to "cut a release", "ship a version", "publish"…
   NOT FOR ordinary commits or reviewing changes.`
5. **Body:** follow `references/STRUCTURE.md` — Purpose, When (Not) to Use, numbered
   release steps, example; push the version-scheme rules to `references/VERSIONING.md`.
6. **Example:** walk through cutting `v1.4.0`.
7. **Validate** against the eight points; fix the vague "publish" trigger.
8. On approval, write `<persona>/skills/release/SKILL.md`.

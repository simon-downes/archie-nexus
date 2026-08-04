---
name: council
description: >
  Multi-perspective council debate for decision-making. Spawns 3-4 subagents with
  different perspectives to deliberate on a question through structured rounds.
  Produces a synthesis with convergence, disagreements, a dissent-strength calibration,
  and a recommendation. Use when making architecture decisions, evaluating trade-offs,
  choosing between approaches, assessing risk, or when asked to "council", "debate",
  "get perspectives on", or "what do others think about".
---

# Purpose

Structured multi-perspective deliberation for decisions where reasonable people
would disagree. Produces genuine intellectual friction, not rubber-stamping.

---

# When to Use

- Architecture or design decisions with multiple viable approaches
- Trade-off evaluation (cost vs speed vs quality vs simplicity)
- Risk assessment before committing to an approach
- Priority decisions (what to build next, where to invest)
- Any decision that benefits from structured disagreement

# When Not to Use

- Questions with clear factual answers (just answer them)
- Code review or plan review (use workflow-review or review-plan)
- Simple yes/no decisions (use judgment)
- When the user has already decided and wants implementation

---

# Perspectives

Select 3-4 perspectives from [references/perspectives.md](references/perspectives.md).

**Selecting perspectives:** choose lenses that will produce genuine tension for the
specific question. Don't pick perspectives that would all agree.

**Defaults when the user doesn't specify:**
- Engineering decisions: `platform-engineer`, `app-developer`, `sre`, `pragmatist`
- Product decisions: `product-manager`, `ux-designer`, `cto`, `app-developer`
- Security/data decisions: `security-engineer`, `privacy-compliance`, `platform-engineer`, `sre`
- Strategy decisions: `cto`, `product-manager`, `cfo`, `pragmatist`

If the user specifies perspectives by name or description, match to the closest
entries in the library. If they describe a perspective not in the library, compose
one on the fly following the same format.

---

# Modes

## Quick (1 round)

Single round of initial positions. No cross-referencing. Fast sanity check.

- Up to 4 parallel subagent calls
- ~10-15 seconds
- 30-80 words per perspective
- Use for: "Am I missing something obvious?" or "Quick gut check on this"

## Full (3 rounds) — default

Three rounds with cross-referencing. Where the real value is.

- Round 1: Initial positions (parallel, 50-150 words each)
- Round 2: Responses and challenges (parallel, each reads Round 1 transcript, 50-150 words)
- Round 3: Final positions (parallel, each reads full transcript, 50-100 words)
- ~30-60 seconds total
- Use for: any decision worth deliberating

---

# Workflow

## 1. Frame the question

Extract the core decision from the user's message. Restate it as a clear question
that perspectives can take positions on. If the question is vague, clarify before
starting the debate.

## 2. Select perspectives

Use the user's specified perspectives, or select defaults based on the decision
domain. State which perspectives are participating.

## 3. Run the debate

### Round 1 — Initial Positions

Spawn all perspectives in parallel. Each subagent receives:

```
You are a {perspective_name}.

{perspective_description from perspectives.md}

COUNCIL DEBATE — ROUND 1: INITIAL POSITIONS

Question: {the framed question}

Context: {any relevant context the user provided}

Give your position on this question from your perspective.
- Speak in first person as your role
- Be specific and substantive (50-150 words, or 30-80 for quick mode)
- State your key concern, recommendation, or insight
- Be opinionated — generic advice is worthless
```

### Round 2 — Responses & Challenges (full mode only)

Spawn all perspectives in parallel again. Each receives the Round 1 transcript:

```
You are a {perspective_name}.

{perspective_description}

COUNCIL DEBATE — ROUND 2: RESPONSES & CHALLENGES

Question: {the framed question}

Here are the Round 1 positions:
{full Round 1 transcript}

Respond to the other council members:
- Reference specific points ("I disagree with {Name}'s point about X...")
- Challenge assumptions or add nuance
- Build on points you agree with
- Maintain your perspective — don't try to be balanced
- 50-150 words
```

### Round 3 — Final Positions (full mode only)

Spawn all perspectives in parallel. Each receives the full transcript:

```
You are a {perspective_name}.

{perspective_description}

COUNCIL DEBATE — ROUND 3: FINAL POSITIONS

Question: {the framed question}

Full debate transcript:
{Round 1 + Round 2 transcript}

Give your final position:
- State where you've shifted (if at all) and why
- Identify your single most important point
- Be honest about remaining disagreements
- 50-100 words
```

## 4. Synthesise

The main agent (not a subagent) reads the full transcript and produces:

### Council Synthesis

**Question:** {the framed question}

**Perspectives:** {list of participants}

**Convergence:** Points where 3+ perspectives agreed. These are likely safe bets.

**Key tensions:** Points of genuine disagreement that won't resolve — the caller
needs to make a judgment call on these.

**Blind spots:** Important considerations that no perspective raised (if any).

**Dissent strength:** How split the council was on the core decision, read from the
Round 3 final positions — e.g. `aligned (4/4)`, `leaning (3/1)`, or `contested (2/2)`.
This calibrates how much to trust the recommendation below; it is not decoration.
- **Aligned / leaning** → the recommendation is safe to act on.
- **Contested** → treat the recommendation as one defensible option, not a verdict.
  Surface the split prominently and prefer to deliberate further, gather the missing
  evidence a perspective demanded, or put the decision to the user rather than acting
  on autopilot.

**Recommendation:** Based on the weight of arguments, what I'd recommend and why.
Explicitly state which perspective's argument was most compelling and which
trade-offs the recommendation accepts. If dissent is *contested*, say so here and
name what would break the tie.

---

# Subagent Configuration

- **Type:** `general` subagent for each perspective (see `# Available Tools`).
- **Model:** same model for all (default to the current session's model).
- **Parallelism:** all perspectives within a round run in parallel — spawn them in a
  single batch. Keep a round to **at most 4 perspectives** so they run concurrently
  rather than queueing.
- **Between rounds:** sequential (Round 2 needs Round 1 output, Round 3 needs both).

---

# Invocation Examples

**Direct:**
"Council: should we use ECS or Lambda for this service?"
"Get perspectives on whether we should migrate to a monorepo"
"Quick council: is this caching approach reasonable?"

**With specific perspectives:**
"Council with security-engineer, sre, and platform-engineer: should we expose this API publicly?"
"Get the cto and cfo perspectives on building vs buying this capability"

**From within workflows:**
During `workflow-plan` Phase 2 (Technical Design), when a design decision has
multiple viable approaches, invoke the council before committing.

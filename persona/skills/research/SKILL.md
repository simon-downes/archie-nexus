---
name: research
description: >
  Research a topic by finding, verifying, and synthesising information from primary
  sources into a single cited document. Delegates the legwork to subagents; the
  thinking and decisions stay with you. Use when asked to "research", "investigate",
  "deep dive into", "find out about", "what are the best practices for", or when a
  question needs multiple external sources rather than a quick answer. Not for simple
  factual questions, codebase questions, or judgment calls.
---

# Purpose

Go out, find the latest information on a topic from primary sources, verify every
claim against a checkable source, and produce a single cited document ready to read.
Research answers *findable* questions and hands back a document to react to — it does
not make the decision for you.

---

# When to Use

- User explicitly asks for research or investigation
- A question requires consulting multiple sources for a thorough answer
- Understanding a topic in depth before making a decision
- Comparing approaches, tools, or strategies against the current state of the art

# When Not to Use

- Simple factual questions ("what's the default timeout?") — just search and answer
- Questions about the current codebase — read the code (see `# Available Tools` → "Exploring a
  codebase")
- **Judgment / decision questions** ("which should *we* pick?") — research the options,
  then route the decision to `council` or the user. Research gathers evidence; it does
  not adjudicate trade-offs.

---

# Principles

- **Primary sources only.** Official docs, source code, specs, standards, and
  first-party announcements. Never a summary-of-a-summary. A blog post is acceptable
  only when it *is* the primary source (e.g. the author announcing their own work).
- **Cite and verify.** Every non-obvious claim is anchored to a source the reader can
  check. Verify each cited URL actually resolves and says what the claim attributes to
  it — do not cite from memory or from a search snippet alone.
- **Delegated legwork, not outsourced thinking.** Subagents read and extract; synthesis
  framing and any recommendation stay with the main agent and, ultimately, the user.
- **Note gaps, don't fill them endlessly.** "No authoritative source found for X" is a
  valid finding. Stop once the core question is answered and sources are consistent.

---

# Modes

## Quick — default

In-context research for a focused question.

- 3-5 primary sources
- Main agent finds, fetches, verifies, and synthesises directly (no subagents)
- Report in-context; write an artifact only if asked or if the finding is worth keeping

## Deep

Parallel research for a broad or high-stakes question.

- 8-12 primary sources
- Split the topic into subtopics; spawn parallel `research` subagents (one per cluster
  of sources), each returning extracted findings **with verified citations**
- Main agent synthesises the returned findings and writes the artifact by default

---

# Workflow

## 1. Frame scope

Determine from the request:

- **Topic** — what to research
- **Questions** — specific questions to answer (helps focus; derive them if not given)
- **Depth** — quick (default) or deep
- **Sources** — any specific sources the user wants consulted

Apply the **scope gate**: if the real question is a decision or judgment call, say so
and route it (`council` or the user) rather than researching a foregone conclusion.
If genuinely ambiguous, ask one clarifying question; otherwise proceed.

## 2. Find primary sources

If sources are provided, use them. Otherwise discover them with the configured search
and fetch tools (refer to `# Available Tools`):

1. Search for the topic, targeting official docs, specs, source repos, and first-party
   posts
2. Prefer recent, first-party material; discard summaries and content farms
3. Gather up to the target count for the mode

If a needed source type is unavailable (no credentials, tool not installed), skip it
and note the gap.

## 3. Read, extract, and verify

**Quick mode:** the main agent fetches each source, extracts the relevant substance,
and confirms each citation resolves and supports the claim.

**Deep mode:** delegate reading to parallel `research` subagents (read-only; see
`# Available Tools`). Each receives its source cluster and the questions, and returns
extracted findings with verified citations — not raw page dumps.

### Subagent prompt template (deep mode)

```
Read the following primary sources and extract findings on this topic. Do not
summarise a summary — go to the primary source and quote/attribute precisely.

Topic: {topic}
Questions to answer: {questions}
Sources to consult: {source cluster}

For each source:
1. Fetch the content
2. Extract only substance relevant to the questions (skip marketing/boilerplate)
3. Record the source URL and its date
4. Verify the URL resolves and actually states what you attribute to it

Return, organised by subtopic (not by source):
- Findings, each tagged [HIGH] / [MED] / [LOW] confidence
- A [CONFLICT] note wherever sources disagree, citing both
- Publication dates; flag anything >2 years old as possibly stale
- Any question you could not find a primary source for

Return extracted findings with citations, concise. Do not write files.
```

## 4. Synthesise and output

The main agent synthesises the findings (never a subagent):

- Organise by subtopic, not by source
- Tag each claim `[HIGH]` / `[MED]` / `[LOW]`; surface `[CONFLICT]` items prominently
- Keep it readable in 5-10 minutes; cite inline or in a references section

**Output:**

- **Quick mode:** report in-context. Persist an artifact only if asked or clearly
  worth keeping.
- **Deep mode (and quick when persisting):** write a cited Markdown artifact to the
  brain's `_inbox/` staging area by default, using the brain write tool (refer to
  `# Available Tools`). Give it a slug from the topic. Frontmatter:

```markdown
---
name: <title>
summary: <one-line summary>
tags: [research, <topic-tags>]
---

# <Title>

<synthesised, cited content>
```

Then report: topic, number of primary sources consulted, and where the artifact
landed. The user reads it and decides whether to promote it into the brain proper.

---

# Example

**User:** "Research approaches to structured output / tool calling across LLM providers"

1. Frame: quick depth; findable question (not a decision) → proceed
2. Search for first-party sources: OpenAI, Anthropic, and Google function-calling docs;
   the JSON Schema spec
3. Fetch each, extract the substance, verify every cited URL resolves and matches
4. Synthesise by subtopic (schema support, strictness, streaming), tag confidence, note
   one `[CONFLICT]` on nested-schema limits
5. Report in-context; offer to persist to `_inbox/llm-structured-output.md`

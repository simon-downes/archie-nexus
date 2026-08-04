# Skill Structure

The house style every skill in this repo follows. Load when authoring or restructuring
a skill so the shape is explicit — don't reverse-engineer it from other skills.

---

## Directory layout

A skill is a directory under `<persona>/skills/<name>/`:

```
<name>/
  SKILL.md          # required: frontmatter + body
  references/       # optional: detail loaded on demand
  scripts/          # optional: executable helpers the skill invokes
  assets/           # optional: templates, fixtures, static files the skill uses
```

- **`references/`** — Markdown detail only some paths through the skill reach (deep
  procedures, tables, craft notes). Loaded on demand via the skill tool's `file` param.
- **`scripts/`** — deterministic helpers the skill runs (a build step, a generator).
  Reach for a script when the work is mechanical; skills carry the judgment, scripts do
  the rote steps. *(Not yet used in this repo — the concept exists for when it fits.)*
- **`assets/`** — static files the skill reads or copies (templates, example configs,
  fixtures). *(Not yet used in this repo.)*

All supporting files must live inside the skill directory — the loader refuses paths
outside it.

## Frontmatter

Only `name` and `description` are read by the loader; both are required.

```yaml
---
name: <lowercase-hyphenated>
description: >
  <one-sentence purpose>. Use when <OR-joined triggers, literal user phrases>.
  Not for <confusable adjacent situations>.
---
```

- `name` matches the directory: plain lowercase-hyphenated, no dates/session/incident IDs.
- `description` = one purpose sentence, then `USE WHEN … NOT FOR …`. Front-load the
  decisive triggers in the first ~60 characters — assume the tail gets truncated.

## Body section order

The body (everything after the closing `---`) follows this order:

1. **# Purpose** — a short paragraph: what the skill produces and why it earns its place.
2. **# When to Use** — bullets: the situations that should activate it.
3. **# When Not to Use** — bullets: the confusable neighbours, with the lighter mechanism
   to prefer instead.
4. **# Principles** — the non-negotiable ideas that shape every run. Omit if the workflow
   speaks for itself.
5. **# Workflow** — numbered steps. Split into named sub-workflows when a skill has
   distinct modes (e.g. creation vs. modification).
6. **# Example** — one concrete end-to-end walk-through. Examples resolve ambiguity prose
   can't.

Optional sections (**# Modes**, **# Validation Checklist**) slot in where they help; keep
the core order recognisable.

## Conventions

- Reference tools via `# Available Tools` rather than hardcoding names — the toolset
  varies by environment.
- Imperative, token-efficient prose. Every line should change behaviour.
- Point to a single source of truth (another skill, the prompt) instead of restating it.

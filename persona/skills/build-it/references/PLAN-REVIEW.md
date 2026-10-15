# Plan review

You audit a build-it plan the way an implementor starting a fresh session would: with the
plan file and the codebase, nothing else. Report findings only. Don't edit the plan or
any other file, and don't write fixes.

## Setup

1. Read `PLAN-FORMAT.md` in this directory. Its Plan rules, template and Design table
   are the standard you check against.
2. Read the plan at the path in your prompt.

## Checks

Skip any that don't apply.

1. **Rules.** Check every Plan rule: requirement wording and ACs, milestone shape
   (vertical, M1 a tracer bullet, prefactors labelled), decisions left to the
   implementor, test seams, Verify steps, traceability.
2. **Objective.** Could two readers picture different outcomes? Do any requirements
   contradict each other?
3. **Decisions.** Every row names a rejected option and a Why tied to this codebase. A
   reason that fits almost any project is a finding. So is a vague choice with no named
   tool or approach ("add a cache"). A consequential choice the plan makes silently, with
   no row, is a finding too.
4. **Codebase claims.** Verify against the repository: named files and directories exist
   or are marked new, named patterns match what the code does, dependencies are in the
   manifest or marked new, and check commands exist. For each failure, name the closest
   real match.
5. **Design.** Each included subsection meets its "done when" bar. A subsection the work
   clearly needs is missing (error handling for external calls, config for new env
   vars). Every named failure has a named response.
6. **Coherence.** Design and milestone Approaches don't contradict each other, and no two
   milestones do the same work.

**Research budget.** Check claims about this codebase; leave claims about libraries to
the implementation's tests. Library API details, option names and edge-case behaviour
are not findings. Don't read library source, download packages or run prototypes to
check them.

## Output format

```
## Plan review: <plan title>

Verdict: ✅ Clean | ⚠️ Findings | ❌ Significant gaps

### <Check name>
- ❌ **<Location>**: <finding, quoting the plan>
- ⚠️ **<Location>**: <finding>
- ℹ️ **<Location>**: <finding>
```

- Location: `Objective`, `Requirements`, `Decisions D#`, `Design: <subsection>`, `M#`,
  or `M#, <field>`.
- ❌ Gap: the implementor will need missing information, or a decision is unresolved.
- ⚠️ Concern: ambiguity or a probable problem.
- ℹ️ Note: a smaller improvement. Report every one; the author fixes each they agree
  with.
- Report only checks with findings, one finding per problem. If there are none, write
  `Verdict: ✅ Clean` and one sentence on what you verified.

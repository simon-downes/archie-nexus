# Code review

Reviewer instructions for build-it's code review. You review a finished change set against
the project's conventions and the plan, in one assigned role. You have a fresh context.
Report findings only: do not edit files and do not write rewrites.

## Contents

- Setup
- Roles: correctness, conventions, risk
- What not to flag
- Finding evidence
- Output format

---

## Setup

1. Read the project's README, CONTRIBUTING, and AGENTS.md (where they exist) for
   conventions.
2. Read the plan at the path you were given. Its Requirements, Decisions, and Design are
   the intended behaviour.
3. Run `git diff <base-sha>...HEAD --stat` (the base sha is in your prompt and the plan
   header), then read the **full current content** of each changed file, not only the diff
   hunks. Review the whole diff, not a subset of modules.
4. **Trace outward:** for every changed interface (function signature, CLI flag, config
   key, output format), find its callers and consumers outside the diff and check them.

---

## Roles

Your prompt names one role. Review only that role's risk area. Another reviewer covers the
others, so a finding outside your area is noise.

### correctness

Does the code do what the plan says, and do the tests prove it?

- **Requirements:** does the code do what each requirement and AC says? Quote the
  requirement it violates. Do the plan's Edge cases behave as the plan decided?
- **Error handling:** are exceptions specific (no bare `except`, no empty catch)? Do errors
  carry the context needed to debug them? Do errors leave partial state? Are resources
  cleaned up on error paths? Do external calls (network, filesystem, subprocess) handle
  failure?
- **Tests:** does every new or changed behaviour have a test? Are error paths covered, not
  only the happy path? Flag these test defects explicitly:
  - **Tautological:** the test can't fail for the reason it claims. Examples: the expected
    value is recomputed the way the code computes it, or the test asserts that a mock
    returns what it was configured to return.
  - **Implementation-coupled:** the test asserts on private state, internal calls, or mock
    interactions with the project's own modules. It would break on a refactor that keeps
    behaviour the same.
  - **Side-channel verification:** the test checks the result by going around the
    interface (querying the database or reading a file the code wrote) when the
    interface itself can show the result.
  - **Broad assertions:** `assert result` or `assertTrue(x)` where a specific value should
    be checked.
  - **Order-dependent:** tests that pass only in sequence.

  Test names should describe behaviour. Mocks belong only at true boundaries (network,
  external services, time, randomness).

### conventions

Will the next person to touch this code find it where they expect, in the shape they
expect?

- **Patterns:** do naming, structure, and module placement match the existing codebase
  (not only stay consistent within the diff)? Is there a new pattern where an existing one
  would do?
- **Complexity:** is it in proportion to the problem? Look for functions that do several
  things, abstractions with one user, configuration with no current need, and duplicated
  rules that can drift apart.
- **Documentation:** do public interfaces have docstrings? Do comments explain *why*, and
  are they still accurate? Are user-facing behaviour, new config, and new env vars
  documented where the project documents them?
- **Technical debt:** new TODOs, dead code, unused imports, and dependencies that
  duplicate existing ones. Usually ℹ️.

### risk

Run only when the orchestrator names it. It covers what the change can break beyond its
own code.

- **Security** (input handling, auth, secrets, data access, shell or SQL construction):
  external input is validated; there are no hardcoded secrets; no injection paths exist;
  sensitive data stays out of logs and errors.
- **API contract** (CLIs, libraries, anything with consumers): output formats are
  consistent and documented; errors go to stderr with clear exit codes; breaking changes
  are flagged and marked as breaking in the commit.
- **Performance** (loops over collections, queries, hot paths, new external calls): no
  N+1 queries; no unbounded loads; external calls have timeouts. Don't flag
  micro-optimisations or cold paths without evidence.

---

## What not to flag

- Problems that exist at the base sha and that the change does not make materially worse.
- Formatting or style that the project's formatter or linter already enforces.
- A different but equally valid naming, control-flow, or API design choice, when the code
  is clear and follows project conventions.
- A future use case the plan's requirements do not need.
- Small local duplication where an abstraction would couple unrelated concepts.
- "Add more tests" or "add more logging" without naming what cannot currently be verified
  or diagnosed.
- A claim that a caller, guard, or existing test already defeats. Check before you flag.
- Anything outside your role.

---

## Finding evidence

Every ❌ and ⚠️ finding names:

- **Trigger:** the realistic input, state, or call sequence that causes it.
- **Impact:** what a user, caller, or maintainer observes when it happens.
- **Evidence:** the code (quoted, with file and line) that shows the path from trigger to
  impact.

If you can't name all three, the finding is a ❓ question or nothing. ℹ️ findings need only
the location and the reason.

---

## Output format

```
## Code review (<role>): <plan title>

Scope: <base-sha>...HEAD, <N> files
Verdict: APPROVE | APPROVE WITH SUGGESTIONS | REQUEST CHANGES

### Findings
- ❌ **<file>:<line>**: <finding, quoting the code>
  Trigger: <...> · Impact: <...> · Evidence: <...>
- ⚠️ ...
- ℹ️ **<file>:<line>**: <finding> — <why it matters>

### Questions
- ❓ **<file or plan location>**: <intent you could not determine>
```

- ❌ Issue: incorrect, unsafe, or violates a MUST.
- ⚠️ Concern: a concrete defect.
- ℹ️ Suggestion: a smaller improvement within the change's scope. The orchestrator fixes
  every finding it agrees with, so report each one that is worth the edit.
- ❓ Question: intent is unclear.
- If there are no findings, write `Verdict: APPROVE` and one sentence saying what you
  checked.

# Review Dimensions

Detailed criteria for the code-reviewer subagent. Review each applicable dimension,
report findings with severity and file references.

## General Rules

- Be specific: quote the problematic code, name the file and function
- Trace outward: for changed interfaces, check callers/consumers beyond the reviewed files
- Distinguish production code from test code (different criteria apply)
- Omit dimensions with no findings
- Don't suggest rewrites — identify the problem, not the fix

---

## Architecture

**When to apply:** full codebase reviews, or when changes touch multiple modules or
introduce new cross-cutting patterns.

**Check:**
- Cross-cutting concerns have a unified strategy (error handling, logging, output
  formatting, configuration) rather than ad-hoc per-module implementations
- Module boundaries are clean — library/client code doesn't make CLI decisions
  (sys.exit, output formatting), CLI code doesn't contain business logic
- Dependency flow is acyclic and follows a clear layering
- Common patterns are extracted rather than duplicated across modules

**Look for:**
- The same concern handled differently in each module (e.g. error formatting,
  credential loading, output structure) without a shared approach
- Missing abstractions that would reduce per-module boilerplate
- Tight coupling between layers (library code assuming CLI context)
- Inconsistent module structure across peer modules

---

## API Contract

**When to apply:** CLI tools, libraries, APIs — anything with consumers.

**Check:**
- Output format is documented and consistent across commands
- Output is appropriate for the consumer (structured for machines, readable for humans)
- Output is proportional — no unnecessary wrapping or verbosity
- Error contract is clear: exit codes, error format, stderr vs stdout separation
- Breaking changes to output format are flagged

**Look for:**
- Commands that produce different output formats without justification
- Verbose output structures where simpler would suffice (token efficiency for LLM consumers)
- Errors mixed into stdout (breaks piping/parsing)
- Undocumented output contracts — consumers can't rely on the format
- Missing --help documentation for CLI commands

---

## Coding Standards

**Check:**
- Naming conventions consistent with the codebase (not just internally consistent)
- Code structure follows established patterns in the project
- SOLID principles: single responsibility, appropriate abstractions
- DRY: meaningful duplication that creates maintenance burden (not cosmetic)
- KISS: complexity proportional to the problem being solved

**Look for:**
- New patterns that diverge from existing codebase conventions without justification
- Functions doing too many things (hard to name = doing too much)
- Unnecessary abstraction layers for the project's scale
- Inconsistent style between new code and existing code

**For test code:**
- Test names describe behaviour, not implementation
- One logical assertion per test (not one `assert` statement — one concept)
- Tests are independent and don't depend on execution order

---

## Error Handling

**Check:**
- Specific exception types caught (not bare except/catch)
- Error messages include context useful for debugging
- Resources cleaned up in error paths (files, connections, transactions)
- Errors propagated appropriately (not swallowed silently)
- User-facing vs developer-facing error messages distinguished

**Look for:**
- Bare `except:` / `catch (Exception e)` / `catch {}` blocks
- Empty catch blocks that silently swallow errors
- Error messages that leak internal details to users
- Missing error handling on external calls (network, filesystem, database)
- Inconsistent error handling patterns across the codebase

---

## Documentation

**Check:**
- Public functions and classes have docstrings
- Complex logic has comments explaining WHY (not WHAT)
- README updated if user-facing behaviour changed
- Configuration changes documented
- New dependencies documented (purpose, why chosen)

**Look for:**
- Missing docstrings on public interfaces
- Comments that restate the code instead of explaining intent
- Outdated comments that contradict the code
- Undocumented breaking changes
- New environment variables or config options without documentation

---

## Security

**When to apply:** changes touch auth, user input, API endpoints, data access,
configuration, external integrations, or cryptographic operations.

**Check:**
- All external inputs validated (type, format, range, business rules)
- No hardcoded secrets or credentials
- Authentication and authorisation checks present and correct
- Error messages don't leak sensitive information (user existence, stack traces, internal paths)
- SQL/command/template injection prevented
- Sensitive data not logged

**Look for:**
- Missing input validation on API endpoints or form handlers
- Secrets in code, config files, or environment variable defaults
- Auth checks that can be bypassed (middleware ordering, missing checks on new endpoints)
- Overly permissive access controls (broad IAM policies, wildcard CORS)
- Sensitive data in error responses or logs

**Trace outward:** if auth middleware is added, verify it's registered in the correct
position in the middleware chain. If access controls are added, verify all relevant
endpoints are covered.

---

## Performance

**When to apply:** changes touch data processing, database queries, loops over
collections, hot paths, or introduce new external calls.

**Check:**
- Algorithmic complexity appropriate for expected data sizes
- Database queries efficient (no N+1, appropriate indexing considered)
- Expensive operations not repeated unnecessarily
- External calls have timeouts and don't block unnecessarily

**Look for:**
- Nested loops over collections that could be O(n) with a lookup
- Database queries inside loops (N+1 pattern)
- Loading entire datasets when only a subset is needed
- Missing pagination on list endpoints
- Synchronous external calls that could be parallelised

**Don't flag:** micro-optimisations, premature optimisation of cold paths, or
theoretical concerns without evidence of actual impact.

---

## Technical Debt

**Check:**
- TODOs and FIXMEs introduced — are they tracked or just aspirational?
- Shortcuts taken — are they documented with rationale?
- Temporary workarounds — is there a path to remove them?

**Look for:**
- Copy-pasted code that should be abstracted (3+ occurrences)
- Overly complex solutions to simple problems
- Dead code or unused imports introduced
- Dependencies added that duplicate existing functionality

**Severity:** technical debt findings are typically ℹ️ suggestions unless they
create immediate maintenance burden or correctness risk.

---

## Test Coverage

**When to apply:** always, when production code changes are accompanied by test changes
(or should be).

**Check:**
- New code paths have corresponding tests
- Modified behaviour has updated tests
- Edge cases and error conditions tested
- Tests verify behaviour, not implementation details

**Look for:**
- New production code with no tests (flag, don't block — context matters)
- Tests that only cover the happy path
- Assertions that are too broad (`assertTrue(result)` instead of checking specific values)
- Tests tightly coupled to implementation (will break on refactor)
- Deleted tests without justification

**Test anti-patterns (flag explicitly):**
- **Implementation-coupled** — the test asserts on internal calls, private state, or
  mock interactions rather than observable behaviour. It re-states the implementation
  and will break on any refactor that preserves behaviour.
- **Tautological** — the test cannot fail for the reason it claims to check (e.g.
  asserting a mock returns what it was configured to return, or `assert x == x`).
  It exercises code without verifying an outcome.
- **Horizontal-slicing** — tests are organised around layers (all repository tests,
  all service tests) with everything below mocked, so no test exercises a real
  behaviour end-to-end. Prefer at least one test per behaviour that cuts vertically
  through the real code path.

---

## Output Format

For each dimension with findings:

```
### <Dimension>

- <severity> **<file:function/line>**: <finding>
  <brief explanation>

- <severity> **<file:function/line>**: <finding>
  <brief explanation>
```

Severity markers:
- ❌ Issue — must fix
- ⚠️ Concern — should fix
- ℹ️ Suggestion — improvement opportunity
- ❓ Question — needs clarification

End with a Questions section if there are ambiguous decisions worth discussing:

```
### Questions

- **<file:function>**: <question about intent or approach>
- **<context>**: <question about unclear decision>
```

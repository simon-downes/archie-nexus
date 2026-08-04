# Milestone Planning Guide

## Purpose

Break work into incremental, testable deliverables with enough technical context that an
implementor in a fresh session can execute without re-discovering decisions.

## Milestone Structure

Each milestone has four required sections and two conditional sections:

**Approach** — technical context that shapes how the work is done:
- Which libraries, tools, or patterns to use
- Where in the codebase this work fits (modules, files, existing patterns to follow)
- Constraints from the existing system
- **Test seam** (for behavioural milestones): the boundary at which this is tested —
  a public interface, not internals — so the implementor doesn't invent test architecture
- ⚠️ Gotchas that could cause problems if missed

Approach answers: "given these tasks, here's what you need to know to do them right."

**Wiring** *(required when the milestone introduces shared state or cross-module coordination)* —
how data flows between components after the change:
- State: what is created, its type, where it's instantiated
- Producers: what writes/mutates it, via what mechanism
- Consumers: what reads it, when, via what mechanism
- Call site: what the constructor/function call looks like after this milestone

Wiring answers: "who owns what and how does it reach where it's needed?"

Include Wiring when: a milestone creates shared mutable state, passes data between modules,
changes function signatures that affect multiple call sites, or introduces callbacks/closures
that capture state. Skip when: the milestone is self-contained within one module.

**Edge Cases** *(required when the milestone handles external input, user-facing errors, or
shared mutable state)* — non-happy-path scenarios with decided behaviour:
- One line per case: scenario → behaviour
- These are design decisions, not implementation details
- The planner decides the behaviour; the implementor decides the code

Edge Cases answers: "what happens when things go wrong or inputs are unexpected?"

Include Edge Cases when: a milestone accepts user/model input, reads files that may not exist,
mutates shared state that others read, or interacts with external systems. Skip when: the
milestone is purely internal plumbing with no failure modes.

**Tasks** — concrete units of work to complete:
- Specific enough to track progress against
- In roughly the order they should happen
- Each task should be completable on its own
- Should not duplicate what's in Approach

Tasks answer: "here's what actually needs to get built."

**Deliverable** — single testable outcome:
- What's true when this milestone is complete
- Must be observable and verifiable
- Exactly one per milestone

**Verify** — how to confirm the deliverable:
- A command to run, a test to pass, a behaviour to observe
- Specific enough that the implementor knows exactly how to check
- Must include HOW to observe the outcome, not just WHAT to observe

## Format

```
1. [Milestone objective]
   Approach:
   - [technical context, guidance, pattern to follow]
   - [library/tool choice and why]
   - ⚠️ [gotcha or high-stakes item]
   Wiring:
   - State: [what, type, where instantiated]
   - Producers: [what mutates it, mechanism]
   - Consumers: [what reads it, when]
   - Call site: [function_call(new_param=value)]
   Edge cases:
   - [scenario]: [decided behaviour]
   - [scenario]: [decided behaviour]
   Tasks:
   - [concrete unit of work]
   - [concrete unit of work]
   Deliverable: [single testable outcome]
   Verify: [how to confirm — including observation mechanism]
```

## Rules

1. **Each milestone has exactly ONE deliverable**
   - Avoid compound deliverables ("X works AND Y works")

2. **Prefer smaller milestones over large ones**
   - Each milestone should feel achievable in a single session
   - Better to have 5 small milestones than 2 large ones

3. **Each milestone is a vertical slice (tracer bullet)**
   - Cut a thin path through every layer end-to-end so the milestone delivers observable
     behaviour, rather than a horizontal slice (all the types, or all the config, in
     isolation)
   - If a milestone touches only one layer with no observable signal, either merge it with
     the milestone that wires it, or label it a **prefactor** (see below)
   - **Prefactor exception:** "make the change easy, then make the easy change." A
     prefactor is a legitimate infrastructure-only milestone that reduces the blast radius
     of the vertical slices that follow. Mark it explicitly so it isn't mistaken for an
     incomplete slice.

4. **Order by dependency**
   - Foundational work before dependent work
   - Infrastructure before features
   - Core functionality before enhancements

5. **No unresolved decisions**
   - The implementor should never need to choose a library or tool
   - The implementor should never need to decide where new code lives
   - The implementor should never need to establish a new pattern
   - If any of these are unresolved, add them to Approach

6. **Approach and Tasks don't overlap**
   - Approach is context and guidance (how and why)
   - Tasks are units of work (what)
   - Don't restate approach items as tasks

## Validation Checklist

Before presenting milestones, audit each one:

- [ ] Would the implementor need to choose a library or tool? → resolve in Approach
- [ ] Would the implementor need to decide where new code lives? → resolve in Approach
- [ ] Would the implementor need to establish a new pattern? → resolve in Approach
- [ ] Is this a vertical slice (end-to-end observable behaviour)? If horizontal (one layer
      only), restructure — or label it a prefactor if it's deliberate groundwork
- [ ] For behavioural milestones, is the test seam named in Approach?
- [ ] Does this milestone introduce shared state or cross-module data flow? → add Wiring
- [ ] Does this milestone handle external input or have failure modes? → add Edge Cases
- [ ] Are Tasks specific enough to track progress?
- [ ] Is the Deliverable a single testable outcome?
- [ ] Does Verify give a concrete way to confirm (including observation mechanism)?

## Good Examples

### Example 1: Rate Limiting

```
1. Set up Redis rate limit storage
   Approach:
   - Use ioredis client (already in project, see src/config/redis.ts)
   - Key format: ratelimit:{api_key}:{minute_bucket}
   - TTL matches rate limit window — counters self-expire
   - Test seam: rate-limit-store public interface (increment/read), not ioredis internals
   - Vertical slice: delivers observable behaviour at the store boundary (counters
     increment and expire) — verifiable end-to-end on its own, not a horizontal layer
   Tasks:
   - Add rate limit config to src/config/index.ts (RATE_LIMIT_MAX, RATE_LIMIT_WINDOW_SECONDS)
   - Create rate limit storage module in src/services/rate-limit-store.ts
   - Add unit tests for counter increment and expiry logic
   Deliverable: Rate limit counters increment and expire correctly in Redis
   Verify: Run rate limit store tests — counters increment, expire after window

2. Implement rate limiting middleware
   Approach:
   - Follow existing middleware pattern in src/middleware/auth.ts
   - Token bucket algorithm — check counter, increment, reject if over limit
   - ⚠️ Middleware order matters — register after CORS, before auth
   - On Redis failure: fail open (allow request), log error at warn level
   Tasks:
   - Create src/middleware/rate-limit.ts with token bucket logic
   - Register middleware in src/app.ts pipeline
   - Add integration test for rate limit rejection
   Deliverable: API returns 429 when rate limit exceeded
   Verify: Run test suite; send 101 requests in under a minute, confirm 429 on 101st

3. Add rate limit response headers
   Approach:
   - Add headers in the rate limit middleware (not a separate middleware)
   - Include on all responses, not just 429s
   Tasks:
   - Add X-RateLimit-Remaining header to all responses
   - Add X-RateLimit-Reset header to all responses
   - Add Retry-After header to 429 responses
   - Update integration tests to verify headers
   Deliverable: All responses include rate limit headers
   Verify: Run test suite; check headers on both allowed and rejected requests
```

### Example 2: Shared State with Wiring and Edge Cases

```
1. Add skill loading tool with shared state
   Approach:
   - New file src/tools/skill.py following closure pattern (make_skill_spec)
   - Skill body = everything after second --- in SKILL.md (frontmatter stripped)
   - Reference file validation: resolve relative to SkillEntry.path.parent, reject if
     resolved path is not under that directory (same logic as validate_path)
   Wiring:
   - State: loaded_skills: list[tuple[str, str]], instantiated as [] in app.py._build_stack()
   - Producers: skill tool handler appends (name, body) during tool execution
   - Consumers: prompt-building closure reads it on each _do_request() call
   - Call site: create_default_registry(..., catalog=catalog, loaded_skills=loaded_skills)
   Edge cases:
   - Skill not in catalog: return tool_error("Unknown skill 'x'. Available: ...")
   - Duplicate load (same skill loaded twice): no-op, return "already loaded" message
   - SKILL.md has no body after frontmatter: load succeeds with empty body
   - Malformed frontmatter (missing name/description): skip during discovery, log warning
   - Reference file path traversal (../): return tool_error("Path outside skill directory")
   - Reference file not found: return tool_error("File not found: ...")
   Tasks:
   - Add src/tools/skill.py with make_skill_spec(catalog, loaded_skills)
   - Implement load mode: parse body, append to loaded_skills, list files in skill dir
   - Implement read mode: resolve path, validate containment, return raw content
   - Register in create_default_registry() when catalog is non-empty
   - Add tests covering each edge case above
   Deliverable: Skill tool loads skills into shared state and reads references safely
   Verify: uv run pytest tests/test_tool_skill.py — all edge cases pass
```

### Example 3: Database Migration

```
1. Create migration infrastructure
   Approach:
   - Use existing knex migration framework (already configured in src/db/)
   - ⚠️ Target table has ~10M rows — column addition must not lock table
   - Strategy: add nullable column first, backfill in batches, then add NOT NULL constraint
   Tasks:
   - Create migration file for adding nullable column
   - Create backfill script in scripts/backfill-user-status.ts
   - Create follow-up migration for NOT NULL constraint
   Deliverable: Migration adds column without table locks
   Verify: Run migration against test database copy; confirm no lock wait timeouts

2. Update application code for new column
   Approach:
   - Update User model in src/models/user.ts
   - New column has default value 'active' — existing code paths don't need changes
   - Only new feature code reads/writes the column
   Tasks:
   - Add status field to User model and types
   - Update user creation to set status explicitly
   - Add status filter to user listing endpoint
   Deliverable: Application reads and writes the new status column
   Verify: Run test suite; create user via API, confirm status field in response
```

## Bad Examples

### Too Vague — No Approach

```
1. Build the rate limiter
   Tasks:
   - Implement rate limiting
   - Add tests
   Deliverable: Rate limiting works
   Verify: Test it
```

*Why bad: No approach — implementor must decide everything. Tasks are vague. Deliverable
and verify are not specific.*

### Approach Duplicates Tasks

```
1. Add Redis storage
   Approach:
   - Add Redis client configuration
   - Create rate limit key schema
   - Add TTL management
   Tasks:
   - Add Redis client configuration
   - Create rate limit key schema
   - Add TTL management
   Deliverable: Redis stores counters
   Verify: Run tests
```

*Why bad: Approach and Tasks say the same thing. Approach should explain how/why
(which client, what key format, why TTL). Tasks should list what to build.*

### Unresolved Decisions

```
1. Add request validation
   Approach:
   - Choose a validation library
   - Decide on validation strategy
   Tasks:
   - Research validation options
   - Implement validation
   Deliverable: Requests are validated
   Verify: Send invalid request, confirm rejection
```

*Why bad: "Choose a validation library" is an unresolved decision — this should have been
resolved during planning. The implementor should never need to research options.*

## Tips

### Breaking Down Large Work

If a milestone feels too large, ask:
- Can this be split into infrastructure + implementation?
- Can this be split into core functionality + enhancements?
- Can this be split into happy path + error handling?

### Writing Good Deliverables

Good deliverables answer: "what observable behaviour changes?"

Avoid: "Code is written", "Feature is complete", "Everything works"
Prefer: "API returns 429 when rate limit exceeded", "Users can log in via GitHub"

### Writing Good Verify Steps

Good verify steps answer: "how do I prove this works?"

Avoid: "Test it", "Check it works"
Prefer: "Run test suite", "Send POST to /api/users with invalid email, confirm 400 response"

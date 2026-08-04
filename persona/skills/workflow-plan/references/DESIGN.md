# Technical Design Guide

## Purpose

Resolve technical decisions so the implementor never needs to choose a dependency, decide where
code lives, or establish a new pattern. Focus on choices and rationale — describe the system
only enough to make decisions clear.

## Principles

- Resolve decisions, don't just describe the system
- Reference existing codebase patterns rather than re-describing them
- Call out deviations from established patterns and why
- Include only sections relevant to the work — skip what doesn't apply
- Provide enough rationale that a reviewer can evaluate each choice

Each section below ends with a **Done when** line — a falsifiable bar for the *prose you
wrote*, checked at authoring time. It is distinct from a milestone's `Verify` (which checks
the implemented deliverable at runtime). If a section can't meet its Done-when bar, it isn't
finished — resolve the gap rather than shipping vague design.

## Sections

Work through these sections in order. Skip sections that aren't relevant to the work.

---

### Overview

High-level description of what will be built and the overall approach. Always include this.

**Content:** 1-3 short paragraphs covering what's being built, the overall strategy, and
any important context.

**Example:**
```
Build rate limiting for the API to prevent abuse and ensure fair usage. Uses Redis for
distributed counters with a token bucket algorithm. Implemented as Express middleware
integrated into the existing request pipeline.
```

**Done when:** a reader who knows nothing about the feature can state what's being built and
the overall strategy in one breath — no unexplained jargon, no "etc."

---

### Technical Stack

Technology choices: new dependencies, frameworks, and tools.

**Resolve:**
- New dependencies and why they were chosen over alternatives
- Version constraints if they matter
- Existing dependencies to reuse (avoid introducing duplicates)

**Example:**
```
- Use ioredis for Redis client (already in project, no new dependency)
- Token bucket algorithm for rate limiting (smoother than fixed windows, allows brief bursts)
- Add zod for config validation (lightweight, TypeScript-native)
```

**Done when:** every new dependency names why it beat the alternative, and every choice says
whether it's a new dependency or an existing one reused.

---

### Architecture

High-level system structure and how components interact. Most useful for new services or
significant additions to existing systems.

**Resolve:**
- Component/service boundaries
- How components interact (request paths, event flows)
- What lives where (which service owns what)

**Example:**
```
- Web API (Node.js) handles HTTP requests
- Redis stores rate limit counters (shared across API instances)
- PostgreSQL stores user data and sessions
- Request flow: Client → Load Balancer → API → Rate Limit → Auth → Business Logic
```

**Done when:** every component boundary names what it owns, and every interaction names how
one component reaches another (request path, event, shared store).

---

### Components

Major components and their responsibilities. Useful when the work introduces or modifies
multiple distinct pieces.

**Resolve for each component:**
- Responsibility: what it does and what it owns
- Key interfaces: endpoints, events, or APIs it exposes
- State ownership: what data it manages

**Example:**
```
**Rate Limiter**
- Responsibility: enforce per-key request rate limits
- Interface: Express middleware, applied to all API routes
- State: request counters in Redis (TTL-based expiry)

**Auth Service** (existing, modified)
- Change: rate limiting runs before auth to protect against unauthenticated floods
- No changes to auth logic itself
```

**Done when:** each component states its responsibility, its interface, and what state it
owns — and existing components called out what changes vs. stays the same.

---

### Data Model

Core entities, relationships, and storage decisions. Include when the work involves new
data structures or changes to existing ones.

**Resolve:**
- New entities or changes to existing ones
- Key fields, identifiers, and constraints
- Storage location and lifecycle (retention, expiry)

**Example:**
```
**Rate Limit Counter** (Redis)
- Key: ratelimit:{api_key}:{minute_bucket}
- Value: integer counter
- TTL: matches rate limit window (60 seconds)
- No persistence needed — counters are ephemeral

**User** (PostgreSQL, existing)
- Add: rate_limit_tier (enum: standard, premium, unlimited)
- Default: standard (100 req/min)
- Migration: add nullable column, backfill, then add NOT NULL
```

**Done when:** every entity/field has a type and a required-or-optional flag, and every store
names its lifecycle (persistence, retention, or expiry).

---

### Data Flow

How data moves through the system for key scenarios. Include when the flow isn't obvious
from the architecture or when there are important failure paths.

**Resolve:**
- Happy path for key scenarios
- Important failure/error paths
- Async flows if applicable

**Example:**
```
**Request Flow (Happy Path)**
1. Request arrives at API
2. Rate limit middleware extracts API key from header
3. Increment counter in Redis (INCR + EXPIRE)
4. Counter within limit → add rate limit headers, pass to next middleware
5. Auth middleware validates token
6. Route handler processes request


**Request Flow (Rate Limited)**
1-3. Same as above
4. Counter exceeds limit → return 429 with Retry-After header
5. Request does not reach auth or route handler

**Redis Failure**
1-2. Same as above
3. Redis connection fails → log error at warn level
4. Fail open: allow request through without rate limit headers
```

**Done when:** every key scenario has a happy path *and* its failure paths spelled out — no
flow ends at "then it works".

---

### Error Handling & Edge Cases

Cross-cutting failure strategy for the work as a whole. Per-milestone edge cases live in the
plan's milestones; this section resolves the *design-level* decisions those milestones must
honour. Include when the work introduces external calls, multi-step operations, or shared
mutable state.

**Resolve:**
- Partial failure: for each external call or multi-step operation, what happens on failure —
  retried, rolled back, or left in an intermediate state?
- Idempotency: which operations must be safely retryable, and how is that guaranteed
  (idempotency key, natural idempotency, dedup check)?
- Known edge cases: empty/null inputs, concurrent requests, boundary values — and the
  intended behaviour for each

**Example:**
```
- Payment capture timeout: retry up to 3 times with exponential backoff, then mark the
  order failed and surface the error — never leave the order in a pending state
- Capture is idempotent via the order's capture-id (dedup on retry)
- Concurrent captures on the same order: first wins, others return the existing result
- Empty cart at capture: reject before calling the payment provider
```

**Done when:** an implementer hitting an unhandled case could correctly guess the intended
behaviour from this section. "Handle errors gracefully" fails that test; a named
trigger → named response passes it.

---

### External Integrations

How the system interacts with external services. Include when the work involves new
integrations or changes to existing ones.

**Resolve for each integration:**
- Purpose and interaction pattern (sync/async, request/response, events)
- Key constraints (timeouts, retries, rate limits, auth)
- Failure handling

**Example:**
```
**GitHub OAuth**
- Purpose: user authentication
- Pattern: synchronous OAuth 2.0 flow
- Constraints: 5000 req/hour rate limit, 10s timeout on token exchange
- Failure: retry once on 5xx, surface error to user on persistent failure
- Credentials: stored in AWS Secrets Manager
```

**Done when:** each integration names its failure handling and its constraints (timeouts,
retries, auth) — not just the happy-path call.

---

### Code Structure

Where new code lives and how it's organised.

**Resolve:**
- New modules, directories, or files to create
- Which existing modules to extend
- Patterns to follow (reference specific existing code as examples)

**Example — existing project:**
```
- New middleware: src/middleware/rate-limit.ts (follows auth.ts pattern)
- Redis config: extend src/config/index.ts with rate limit settings
- Types: add RateLimitConfig to src/types/middleware.ts
```

**Example — greenfield:**
```
- Project structure:
  src/
    middleware/    # Request pipeline middleware
    routes/       # Route handlers grouped by domain
    services/     # Business logic
    config/       # Configuration and environment
    types/        # TypeScript type definitions
- Each route module exports a Router, registered in src/app.ts
- Services are plain functions, not classes
```

**Done when:** every new module names where it lives, and every "follow the existing pattern"
points at a specific file/example the implementor can open.

---

### Patterns and Conventions

How code should be written. Most important for greenfield work or when establishing new
patterns in an existing codebase.

**Resolve:**
- Error handling approach
- Logging conventions
- Configuration approach
- Testing patterns
- Naming conventions if not already established

**Example:**
```
- Error handling: throw typed errors (AppError subclasses), catch in error middleware
- Logging: use existing pino logger, structured JSON, include request ID
- Config: environment variables validated at startup with zod schema
- Tests: colocate unit tests as *.test.ts, integration tests in tests/integration/
```

**Done when:** each convention is concrete enough to copy without asking a follow-up question
— or explicitly defers to an already-established codebase convention.

---

### Infrastructure and Deployment

Infrastructure changes, environment requirements, and deployment considerations.

**Resolve:**
- New infrastructure resources needed
- Environment variables or secrets to add
- CI/CD changes
- Database migrations

**Example:**
```
- Add Redis ElastiCache cluster (Terraform in infrastructure/modules/redis/)
- New env vars: REDIS_URL, RATE_LIMIT_MAX (default 100), RATE_LIMIT_WINDOW_SECONDS (default 60)
- Add Redis health check to existing /health endpoint
- No database migrations needed
```

**Done when:** every new resource, env var, and secret is named explicitly, and "none" is
stated where a category doesn't apply (rather than left silent).

---

### Non-Functional Concerns

Security, observability, performance, and reliability considerations that affect implementation.

**Resolve only what's relevant** — each concern states the *intent* to satisfy, not a topic
to think about. A non-functional requirement without a number or a named mechanism isn't
verifiable ("should be fast" tells an implementer nothing; "p95 under 200ms at 500 req/s"
does):
- Security: what's the new attack surface or sensitive data, and how is it defended? Name the
  input to validate, the auth boundary, or the secret and where it lives
- Observability: if this breaks silently, what surfaces it? Name the specific log or signal
  that would reveal a regression — not "we'll monitor it"
- Performance: what's the target and under what conditions? A latency/throughput number at a
  stated load or dataset size — not "fast enough"
- Reliability: what failure is being guarded against, and what's the response? A named
  mechanism (retry policy, circuit breaker, fail-open) tied to a named failure

**Example:**
```
- Security: rate limiter reads the API key from an untrusted header — validate format before
  use; the limit config is not secret, no new secrets introduced
- Observability: log all rate limit denials at warn level (API key, count, limit). A spike in
  denials or a drop to zero traffic is the signal a misconfigured limit would produce
- Performance: added Redis round-trip must keep request p95 under 200ms at 500 req/s
- Reliability: on Redis failure, fail open and log at warn — never block requests due to a
  rate limiter outage
```

**Done when:** every concern names a concrete mechanism or threshold, not a placeholder like
"handle appropriately" or "notify the team".

---

### Key Decisions

Important choices and their rationale. Use for decisions that aren't captured naturally
in other sections, or to summarise the most significant choices in one place.

**Example:**
```
- Token bucket over fixed window: smoother rate limiting, allows brief bursts
- Fail open on Redis failure: availability over strict rate enforcement
- Rate limit before auth: protects against unauthenticated floods
```

**Anti-generic-rationale test:** read each rejection reason in isolation. If it would apply
to almost any project ("simpler", "more scalable", "industry standard"), it's too generic —
tie it to a concrete constraint of *this* system.

**Done when:** each decision names the option chosen *and* the option rejected, and every
rationale survives the anti-generic test.

---

### Risks and Open Questions

Uncertainties, trade-offs, and items to resolve. Include when there are known risks or
assumptions that should be surfaced.

**Example:**
```
Risks:
- Redis single point of failure (mitigated by ElastiCache multi-AZ)
- Rate limit counters lost on Redis restart (acceptable — counters reset)

Assumptions:
- API keys are unique per client (no shared keys)
- Current Redis cluster has capacity for rate limit keys

Open Questions:
- Should rate limits be configurable per API key or global only?
```

**Done when:** every open question has an owner or a point at which it must be resolved, and
every risk states its mitigation or why it's acceptable — no bare "might be a problem".

---

## Section Selection Guide

Choose sections based on the type of work:

| Work Type          | Typical Sections                                                    |
|--------------------|---------------------------------------------------------------------|
| Small feature      | Overview, Code Structure, Data Flow                                 |
| New service        | Most or all sections                                                |
| Infrastructure     | Overview, Technical Stack, Architecture, Infrastructure, Key Decisions |
| Integration        | Overview, External Integrations, Data Flow, Error Handling, Non-Functional |
| Refactoring        | Overview, Code Structure, Components, Key Decisions, Risks          |
| Data model change  | Overview, Data Model, Data Flow, Code Structure                     |

## Greenfield vs Existing Codebase

**Existing codebase:** Many decisions are already made. Reference existing patterns, call out
new dependencies, note deviations. Design section will be shorter.

**Greenfield:** More decisions to resolve — project structure, core dependencies, conventions,
patterns. Design section will be longer. This is expected and necessary.

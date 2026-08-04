# Execution Guide

Complete walkthrough of implementing a rate limiting feature.

## Setup

Plan exists at:
```
./plans/001-rate-limiting.md
```

Contains: Objective, Requirements, Technical Design, 3 Milestones.

After loading, write the progress ledger `./plans/001-rate-limiting-progress.md`:
```
- [ ] Milestone 1: Set up Redis rate limit storage
- [ ] Milestone 2: Implement rate limiting middleware
- [ ] Milestone 3: Add rate limit response headers
```

Milestone 1 from the plan:
```
1. Set up Redis rate limit storage
   Approach:
   - Use ioredis client (already in project, see src/config/redis.ts)
   - Key format: ratelimit:{api_key}:{minute_bucket}
   - TTL matches rate limit window — counters self-expire
   - ⚠️ Use separate Redis DB to isolate from cache
   Tasks:
   - Add rate limit config to src/config/index.ts (RATE_LIMIT_MAX, RATE_LIMIT_WINDOW_SECONDS)
   - Create rate limit storage module in src/services/rate-limit-store.ts
   - Add unit tests for counter increment and expiry logic
   Deliverable: Rate limit counters increment and expire correctly in Redis
   Verify: Run rate limit store tests — counters increment, expire after window
```

## Step 1: Understand the Context

Read the Approach: it says to use ioredis (already in project), references `src/config/redis.ts`
for the existing pattern, and warns about using a separate Redis DB.

Investigate:
- Read `src/config/redis.ts` — understand existing Redis client setup
- Read `src/config/index.ts` — understand how config values are structured
- Scan `src/services/` — understand module patterns and conventions

Now you know: project uses separate Redis DBs per concern, config uses typed objects
validated at startup, services export plain functions.

## Step 2: Work Through Tasks

**Task 1: Add rate limit config**

Add to `src/config/index.ts` following existing config pattern:
```typescript
rateLimiting: {
  max: env.RATE_LIMIT_MAX ?? 100,
  windowSeconds: env.RATE_LIMIT_WINDOW_SECONDS ?? 60,
}
```

**Task 2: Create storage module**

Create `src/services/rate-limit-store.ts` following existing service patterns:
```typescript
const KEY_PREFIX = 'ratelimit:';

export async function incrementCounter(apiKey: string): Promise<number> {
  const key = `${KEY_PREFIX}${apiKey}:${getMinuteBucket()}`;
  const count = await redis.incr(key);
  if (count === 1) {
    await redis.expire(key, config.rateLimiting.windowSeconds);
  }
  return count;
}
```

**Task 3: Add tests (test-first)**

Write the tests before the implementation is finalised — start from the milestone's
test seam, watch them fail (red), then make them pass (green). Create
`tests/services/rate-limit-store.test.ts` following existing test patterns:
```typescript
describe('rate-limit-store', () => {
  it('increments counter for API key', async () => { /* ... */ });
  it('expires counter after window', async () => { /* ... */ });
  it('uses separate counter per minute bucket', async () => { /* ... */ });
});
```

## Step 3: Verify

**Code quality:** spawn qa-runner → formatting, linting, tests all pass ✓

**Milestone-specific:** the plan says "Run rate limit store tests — counters increment,
expire after window":
```bash
npm test tests/services/rate-limit-store.test.ts
```
All pass ✓. Deliverable confirmed.

## Step 4: Commit

```bash
git add -A
git commit -m "feat: set up Redis rate limit storage"
```

Then update the ledger — mark Milestone 1 `[x]` in `001-rate-limiting-progress.md`.

## Step 5: Report

```
Milestone 1 complete: Rate limit counters increment and expire correctly in Redis.

Next: Milestone 2 — Implement rate limiting middleware

Proceed?
```

## Remaining Milestones

Repeat steps 1-5 for Milestones 2 and 3.

## Completion

Report:
```
Implementation complete. All milestones delivered:

✓ Redis rate limit storage with TTL
✓ Middleware enforcing 100 req/min limit
✓ Response headers (X-RateLimit-Remaining, Retry-After)

Would you like to enter Review Mode?
```

Confirm every milestone in the ledger is `[x]`, then move the plan and its
`-progress.md` ledger into a `done/` subdirectory (e.g. `plans/done/`) so a stale
ledger can't trigger a resume.

---

## Task Granularity

**Too coarse:**
```
- Implement rate limiting
```

**Too fine:**
```
- Import redis module
- Create client variable
- Set host property
```

**Appropriate (what the plan provides):**
```
- Add rate limit config to src/config/index.ts
- Create rate limit storage module in src/services/rate-limit-store.ts
- Add unit tests for counter increment and expiry logic
```

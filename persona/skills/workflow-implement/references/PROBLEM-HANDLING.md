# Problem Handling Guide

## When to Stop and Ask

### Unclear Requirements or Design

**Indicators:**
- Requirements are ambiguous or contradictory
- Multiple valid interpretations exist
- Plan doesn't specify how to handle edge cases

**Action:**
Stop immediately and ask user with specific options if possible.

**Example:**
```
Plan specifies "rate limit per API key" but doesn't clarify:
- Should rate limits apply to unauthenticated requests?
- Should different endpoints have different limits?

Options:
A) Apply global rate limit to all requests
B) Only rate limit authenticated requests
C) Different limits per endpoint group

Which approach?
```

### Discovered Work Not in Plan

**Indicators:**
- Finding additional requirements during implementation
- Discovering related systems not mentioned in plan

**Action:**
Complete current milestone, then report to user.

**Do NOT implement unplanned work without approval.**

**Example:**
```
Milestone 2 complete.

Discovered issue: API includes GraphQL endpoint not in plan.
GraphQL uses different auth and may need separate rate limiting.

Options:
A) Apply same rate limiting to GraphQL
B) Create separate plan for GraphQL
C) Exclude GraphQL from this implementation

Which approach?
```

### Plan Appears Flawed

**Indicators:**
- Plan approach won't work as designed
- Requirements conflict with technical constraints
- Design has fundamental flaw

**Action:**
Stop, explain issue, propose concrete amendments, show diff, wait for approval.

**Example:**
```
Issue: Plan specifies Redis TTL for rate limits, but this loses 
partial progress when keys expire.

Proposed change to plan Technical Design:

- Current: "Use Redis TTL for automatic cleanup"
+ Proposed: "Use sliding window with sorted sets for accurate limiting"

This requires updating Milestone 1 to implement sorted set storage.

Approve this change?
```

**Never modify the plan without explicit approval.**

## Escalation Template

When blocked, provide:

1. **Context** - Current milestone/task
2. **Problem** - Specific issue encountered
3. **Question** - Decision needed or guidance requested
4. **Options** - Concrete choices if applicable

**Example:**
```
Context: Milestone 2 - Implementing rate limiting middleware

Problem: Plan doesn't specify how to handle WebSocket connections

Question: Should WebSocket connections be rate limited?

Options:
A) Apply same rate limit to WebSocket handshake
B) Separate rate limit for WebSocket messages
C) Exclude WebSockets from rate limiting

Which approach?
```

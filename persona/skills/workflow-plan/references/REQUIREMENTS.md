# Requirements Writing Guide

## Format

Requirements are written as concise bullet points with acceptance criteria:

```
- MUST / SHOULD / MAY <clear requirement describing behavior or constraint>
  - AC: <acceptance criteria describing how the requirement is verified>
  - AC: <additional testable conditions if needed>
  - Why: <optional context or rationale if something may not be obvious>
```

## RFC 2119 Keywords

- **MUST** — Mandatory behavior required for correctness or compliance
- **SHOULD** — Strongly recommended behavior that may be omitted only with a valid reason
- **MAY** — Optional capability that is permitted but not required

**Default to MUST unless there is a clear reason to use SHOULD or MAY.**

## Writing Rules

1. The top bullet must be a complete requirement that stands on its own
2. Requirements must describe observable system behavior or constraints
3. Avoid implementation details unless they are true constraints
4. Every MUST requirement should include at least one AC
5. Acceptance criteria must be testable and measurable
6. Use "Why" only when additional context prevents misunderstanding
7. Avoid vague words unless tied to measurable criteria:
   - secure, fast, robust, properly, user-friendly, scalable

## Good Examples

### Integration Requirement
```
- MUST support user authentication via OAuth2 using the GitHub provider
  - AC: Users are redirected to GitHub's OAuth authorization endpoint
  - AC: Successful authorization creates or links a local user account
```
*Why good: Specifies a clear integration and observable behavior.*

### Constraint Requirement
```
- MUST enforce a rate limit of 100 requests per minute per IP address
  - AC: Requests exceeding the limit return HTTP 429 Too Many Requests
  - AC: The response includes a Retry-After header
```
*Why good: Defines a measurable constraint and expected system response.*

### Recommendation Requirement
```
- SHOULD include a correlation ID in all HTTP responses
  - AC: Responses include header X-Correlation-ID
```
*Why good: Strong operational recommendation that improves debugging but may not be mandatory.*

### Data Requirement
```
- MUST persist user preferences across sessions
  - AC: User theme selection is restored on next login
  - AC: Preferences survive application restarts
```
*Why good: Clear behavior with testable outcomes.*

### Error Handling Requirement
```
- MUST handle database connection failures gracefully
  - AC: Failed connections return HTTP 503 Service Unavailable
  - AC: Application logs connection errors with timestamp and error details
  - AC: Application retries connection up to 3 times with exponential backoff
```
*Why good: Specifies observable error behavior and recovery strategy.*

### Security Requirement
```
- MUST validate all user input before processing
  - AC: Requests with SQL injection patterns are rejected with HTTP 400
  - AC: Requests with XSS patterns are sanitized before storage
  - AC: File uploads are scanned and rejected if malicious
  - Why: Prevents common injection attacks
```
*Why good: Specific validation behaviors with measurable outcomes.*

## Bad Examples

### Vague Requirement
```
- MUST validate all inputs
```
*Why bad: Too vague; does not specify which inputs or how validation behaves.*

**Better:**
```
- MUST validate email addresses before account creation
  - AC: Invalid email formats return HTTP 400 with error message
  - AC: Valid formats match RFC 5322 specification
```

### Non-testable Requirement
```
- SHOULD be secure
```
*Why bad: "Secure" is subjective without specific controls or measurable criteria.*

**Better:**
```
- MUST encrypt sensitive data at rest
  - AC: User passwords are hashed using bcrypt with cost factor 12
  - AC: API keys are encrypted using AES-256
```

### Implementation Detail
```
- MUST use try-catch blocks for error handling
```
*Why bad: Specifies a coding technique instead of required system behavior.*

**Better:**
```
- MUST return appropriate error responses for all failure scenarios
  - AC: Validation errors return HTTP 400 with field-level error details
  - AC: Server errors return HTTP 500 without exposing internal details
```

### Obvious Statement
```
- MUST have a database
```
*Why bad: States infrastructure without describing behavior or constraints.*

**Better:**
```
- MUST persist user data with 99.9% durability
  - AC: Data survives single node failures
  - AC: Automated backups run daily with 30-day retention
```

### Ambiguous Requirement
```
- SHOULD respond quickly
```
*Why bad: "Quickly" is subjective and not measurable.*

**Better:**
```
- SHOULD respond to API requests within 200ms at p95
  - AC: 95% of requests complete within 200ms under normal load
  - AC: Response time metrics are tracked and alerted
```

## Refinement and Iteration

### Handling Conflicts or Contradictions

When requirements conflict:
- Identify the specific contradiction
- Present both requirements to the user
- Explain why they conflict
- Ask the user to clarify priority or intent
- Update requirements to resolve the conflict

**Never resolve conflicts by assumption - surface them for user decision.**

### When to Probe Deeper

Dig deeper when:
- A requirement is vague or subjective
- Acceptance criteria aren't measurable
- Edge cases or error scenarios are unclear
- Dependencies or interactions aren't specified
- The user uses ambiguous terms

**If you can't picture how to test it, it's not clear enough.**

## Discovery Tips

### Think About Failure Scenarios
- What happens when external services are unavailable?
- How should the system handle invalid input?
- What are the edge cases and boundary conditions?

### Consider Non-Functional Aspects
- Security: Authentication, authorization, input validation
- Performance: Response times, throughput expectations
- Reliability: Error handling, retries, fallbacks
- Observability: Logging, metrics, tracing

### Ask Clarifying Questions
- ONE focused question at a time
- Target the most critical gaps first
- Offer options when the user may be unsure
- Build on previous answers iteratively

### Validate Understanding
- Summarize what you've learned
- Confirm your interpretation matches user intent
- Highlight any assumptions you're making
- Check if anything important was missed

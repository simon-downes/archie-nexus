# 039 — Spec: Jira tooling

## Objective

Provide the agent with safe, useful access to Jira Cloud through an `exec`-only `jira` namespace. The implementation will cover the capabilities currently provided by agent-kit, while using Nexus’s async tool architecture, mounted typed credential store, shared `tools` policy, bounded outputs, and service skill guidance.

Jira reads are available by default when credentials are configured. Jira writes remain disabled unless explicitly enabled by policy, and configured project scopes apply independently to reads and writes.

## Context

Nexus already has a typed `JiraCredential` containing `email`, `token`, and `cloud_id`, and the agent container mounts the Nexus home directory at `ARCHIE_HOME_DIR`, including `credentials.yaml`. Jira is therefore already supported by auth persistence but has no agent client, tools, skill, or policy configuration.

The prior agent-kit integration uses Jira Cloud REST API v3 through `https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3` with Basic authentication. It supports projects, project metadata/statuses, structured and raw-JQL issue search, issue details, issue creation/update, transitions, comments, user search, and attachments. Nexus will cover the useful capabilities but will not expose generic HTTP methods or raw JQL.

The project plan is `038-project-saas-tooling.md`; this spec must honour its locked decisions, especially the generic `tools` policy shape, safe defaults, credential-store access, exec-first exposure, bounded results, and provider-specific scope semantics.

## Requirements

Each requirement below is observable and includes acceptance criteria. Detailed implementation choices are in Technical Design; milestone verification provides the executable test sequence.

### Execution and public surface

- Jira MUST be available only through the `exec` service namespace for the initial implementation.
  - AC: Model-authored exec code can call `await jira.get_issue(...)` and the call reaches the Jira tool.
  - AC: Jira functions do not appear as native `ToolSpec` entries or in permanent exec documentation.
- Jira public functions MUST use the shared service-tool registration contract with `exec_enabled=True`, `exec_docs=False`, `native=False`, and `namespace="jira"`.
  - AC: Registration exposes the functions under `jira.<function>` and preserves existing flat-tool behavior.
  - AC: Duplicate namespace/function registration fails without overwriting an existing function.
- The Jira skill MUST document the complete public surface, safe sequencing, policy behavior, limits, and limitations without credentials or raw HTTP guidance.
  - AC: `persona/skills/jira/SKILL.md` is discovered from `ARCHIE_PERSONA_DIR/skills` and lists every public function.

### Credentials and external client

- Jira MUST authenticate using the existing typed credential store and Jira Cloud REST API v3.
  - AC: The client reads `JiraCredential` through `get_credential("jira")`, uses `cloud_id` to construct the Atlassian API base URL, and uses Basic auth over HTTPS.
  - AC: Jira credentials remain `set_env=False` and never occur in tool parameters, results, skill content, or errors.
- Missing, incomplete, invalid, or unreadable Jira credentials MUST produce a sanitized configuration error before any provider request.
  - AC: Whitespace-only email/token and invalid cloud IDs are rejected locally.
  - AC: Store paths, raw validation text, tokens, and credential objects are absent from the model-visible error.
- Jira requests MUST use bounded async HTTP lifecycle and no automatic retries.
  - AC: `httpx.AsyncClient` uses 10-second connect/write/pool, 30-second read, and a 60-second multi-request deadline.
  - AC: Clients close on success and failure; mutation requests are never automatically retried.

### Policy and scope

- Nexus MUST provide the generic `tools.<provider>.read/write` policy with read-enabled/write-disabled defaults and opaque provider scopes.
  - AC: Missing Jira policy resolves to read enabled and write disabled; missing/empty scope means unrestricted.
  - AC: Malformed common policy fails validation; unknown provider entries do not prevent unrelated providers from loading.
- Jira MUST validate and enforce independent read and write policies for project-key scopes.
  - AC: Disabled standalone reads/writes make no provider request.
  - AC: Direct out-of-scope resources return a policy-denied error, not not-found, before provider access.
  - AC: Scoped issue searches include the allowlist in generated JQL; raw JQL is not accepted.
- Write operations MAY perform only documented internal reads needed by that write.
  - AC: Assignee lookup, transition discovery, and required post-mutation issue reads use write policy/scope and are unavailable as general read access when standalone reads are disabled.
  - AC: A failed required post-mutation read raises `JiraMutationIndeterminateError`.
- Tool policy MUST be an immutable session-start snapshot.
  - AC: The agent passes the snapshot through the exec runner context; changing `config.yaml` does not affect an existing session and is observed only by a new session.

### Public capabilities

- Jira MUST expose the complete scoped capability surface: `list_projects`, `get_project`, `list_issues`, `get_issue`, `search_users`, `create_issue`, `update_issue`, `transition_issue`, `add_comment`, and `attach_file`.
  - AC: Each function is async, credential-free, returns JSON-serializable bounded data, and is reachable under `jira`.
  - AC: Agent-kit capabilities are mapped in the capability table; raw HTTP methods and raw JQL are explicitly excluded.
- Project and issue reads MUST return bounded, policy-scoped data with `get_project` combining metadata, issue types, and statuses and `get_issue` including bounded comments.
  - AC: Project/comment/user collections use offset pagination; issue search uses `nextPageToken`; limits are honored across pages.
  - AC: Metadata/status partial failure is an error, not a partial response; blocked direct resources are not reported as missing.
- Jira mutations MUST require write enablement and permitted project scope.
  - AC: Create, update, transition, comment, and attachment calls denied by policy make no mutation request.
  - AC: `create_issue(status=...)` performs the documented post-create follow-up transition; ambiguous assignees prevent creation, while unavailable or ambiguous follow-up transitions produce an indeterminate error containing the created key. Standalone `transition_issue` rejects unavailable or ambiguous transitions before mutation.
- Attachment uploads MUST be workspace-confined and bounded.
  - AC: Only the fixed extension allowlist and inclusive 10 MiB maximum are accepted.
  - AC: Race-safe `O_NOFOLLOW` opening prevents symlink escape; output contains metadata only with nullable `content_url`.

### Capability mapping

| Agent-kit capability | Nexus function | Notes |
|---|---|---|
| `get_projects` | `list_projects` | bounded, policy-scoped summaries |
| `get_project` + `get_statuses` | `get_project` | metadata, issue types, and statuses combined |
| `search_issues` | `list_issues` | structured filters; no raw JQL |
| `get_issue` | `get_issue` | includes bounded comments |
| `search_users` | `search_users` | bounded candidates; no silent fallback |
| `create_issue` | `create_issue` | optional post-create status transition |
| `update_issue` | `update_issue` | explicit clear semantics |
| `transition_issue` | `transition_issue` | write-gated |
| `create_comment` | `add_comment` | write-gated |
| `attach_file` | `attach_file` | workspace-confined, high-risk write |
| generic `get/post/put` | excluded | no arbitrary API access |

### Data, errors, and safety

- Jira ADF descriptions/comments MUST be converted to bounded plain text and input text MUST be converted to minimal ADF.
  - AC: Formatting helpers handle missing/malformed ADF without returning raw provider payloads.
- Public results MUST remain valid JSON and at most 16,000 characters after service-level bounding.
  - AC: Text is reduced first, optional fields then collections; required identifiers are retained or `JiraResultTooLargeError` is raised.
- Jira errors MUST preserve configuration, policy, authentication, provider authorization, not-found, rate-limit, transport, validation, and mutation-indeterminate distinctions.
  - AC: 401, 403, 404, 429, pre-transmission connection failure, post-request uncertainty, and malformed responses produce their documented distinct errors without secrets.
- Mutations MUST not claim idempotency after an indeterminate outcome.
  - AC: No automatic retry occurs; the error instructs verification with `get_issue` and identifies the operation/key when safe.

## Technical Design

### Overview

Use the existing async agent-tool architecture and `httpx` to provide a credential-free, exec-only Jira namespace. Shared policy/configuration and exec registration are implemented once in M1; Jira-specific client, scope, formatting, and safety behavior remain in the agent service package.

### Technical Stack

- Reuse existing `httpx>=0.27`; no Jira SDK or new dependency.
- Reuse existing `msgspec` configuration/credential models, YAML store, pytest, and pytest-asyncio.
- Use async `httpx.AsyncClient`, not agent-kit’s synchronous client.

### Architecture

- Agent startup owns the immutable non-secret policy snapshot.
- Exec runner context carries that snapshot to decorated Jira functions.
- Jira functions enforce policy, load credentials from the mounted store, call the async client, and return bounded JSON-serializable values.
- The provider boundary is Jira Cloud REST API v3; no arbitrary HTTP surface is exposed.

### Components and locations

- `shared/src/archie_shared/schemas.py` — add typed top-level `tools` configuration and defaults.
- `shared/src/archie_shared/...` — add reusable policy model/resolution helpers where the common shape belongs; keep Jira project-key semantics in the Jira layer.
- `agent/src/archie_agent/exec/tools/__init__.py` — extend decorator metadata and service registration while preserving existing flat tools.
- `agent/src/archie_agent/exec/runner.py` and `agent/src/archie_agent/exec/tool.py` — pass the immutable policy snapshot into exec invocations and filter permanent exec documentation.
- `agent/src/archie_agent/tools.py` and `agent/src/archie_agent/exec/native.py` — preserve provider-neutral registry/native filtering while excluding `native=False` Jira functions.
- `agent/src/archie_agent/exec/tools/jira/` — Jira public tool package and service-specific helpers.
- `agent/src/archie_agent/exec/tools/jira/client.py` — async Jira REST client, request/error mapping, ADF conversion, pagination, and bounded response shaping.
- `agent/src/archie_agent/exec/tools/jira/policy.py` (or equivalent) — resolve common read/write policy and Jira project scopes without duplicating configuration defaults.
- `persona/skills/jira/SKILL.md` — model-facing usage guidance.
- `tests/` — shared config/policy tests, Jira client/tool tests, registration tests, and skill discovery/content tests as appropriate.

Use existing `httpx` rather than introducing a Jira SDK. Imports of optional/heavy service code should remain lazy where consistent with current tool patterns. The client must be async and must not be a copy of agent-kit’s synchronous implementation.

### Data Flow

#### Policy resolution and data flow

1. The agent loads `config.yaml` into `NexusConfig` through the existing `load_nexus_config()` startup path from the mounted `ARCHIE_HOME_DIR`. When the agent invokes the exec runner, it passes the agent-owned immutable non-secret tool-policy snapshot through an explicit runner context parameter; the runner does not reread the file.
2. A Jira tool resolves the effective `tools.jira` read/write policy, applying defaults for missing entries and validating Jira scope as a list of normalized project keys.
3. The tool classifies the operation as read, write, or write-with-internal-read and checks policy before credential/provider access.
4. Direct project/issue tools check project keys before constructing provider requests; collection tools apply collection-specific scope behavior defined below.
5. The client loads the typed Jira credential from the mounted credentials store and creates a short-lived async HTTP client.
6. Scoped list/search requests include the allowed project set in generated Jira query construction; project listing filters/accumulates only allowed projects across provider pages.
7. Results are normalized, bounded, and returned as JSON-serializable values; the runner serializes them and applies the existing global output cap. Secrets and raw provider payloads are excluded.

The design must not project Jira credentials into environment variables. If the existing runtime does not currently make non-secret `tools` policy available to the agent, that propagation is a shared configuration concern and must not be solved by putting secrets in the policy payload.

### Data Model

- `ToolPolicy`: required `read` and `write` policy blocks with boolean `enabled` and opaque optional `scope`; stored in `NexusConfig`, session lifetime.
- Jira project scope: normalized list of 0–100 project keys, empty meaning unrestricted.
- Public result objects: JSON-serializable dictionaries/lists, bounded per the Requirements section; not persisted.
- `JiraMutationIndeterminateError` and `JiraResultTooLargeError`: model-visible typed tool errors, request lifetime only.

### Scope enforcement

Jira project scope is a list of project keys, normalized case-insensitively for comparison while preserving canonical uppercase output. For `list_projects`, Jira pages are accumulated and only allowed project keys are returned until the requested limit or provider exhaustion; hidden-project counts are never reported. For `list_issues`, generated JQL uses the configured allowlist rather than post-response filtering. Direct issue/project operations require the issue/project key to be in scope. `search_users` is allowed under the read gate but is not project-scoped because Jira user search has no project context; its results are bounded candidates only and are never returned as project authorization evidence. This ensures requested limits and pagination operate on permitted results and avoids exposing raw JQL as a bypass.

Project keys accepted by tools use Jira’s Cloud project-key grammar: 1–10 total ASCII characters, beginning with an ASCII letter and followed by ASCII letters, digits, or underscores; issue keys use that project key followed by `-` and a positive integer. `get_project` accepts keys only, not IDs, to keep local scope checks unambiguous.

For direct issue keys, extract and normalize the project key before contacting Jira. For project and issue mutations, check the relevant write policy before any provider call. Read and write scopes are independent, so a project may be readable but not writable.

### Execution namespace contract

The shared decorator stores `exec_enabled`, `exec_docs`, `native`, and optional `namespace` metadata; explicit decorator metadata is authoritative over package/module location. Registration keeps a flat map for existing tools and a namespace map for service tools. The runner receives the agent-owned immutable policy snapshot through an explicit invocation context, makes it available to decorated functions, and injects existing flat functions unchanged plus a namespace object whose attributes are the public service functions. Native generation includes only functions with `native=True`; permanent exec documentation includes only functions with `exec_docs=True`. `exec_enabled=False` functions are absent from runner injection. Duplicate flat names, namespace names, or namespace/function pairs fail registration during import. The Jira implementation returns JSON-serializable values; no JSON-string conversion is performed by the service functions.

### Error Handling and Edge Cases

Provider errors are mapped without raw response leakage. No mutation is retried. A post-transmission timeout, upload uncertainty, follow-up transition failure, or required post-mutation read failure is indeterminate; the tool raises `JiraMutationIndeterminateError` and does not claim final state. Policy-denied direct requests are rejected before provider access. Attachment paths are opened race-safely after policy and workspace checks.

### External Integration

Jira Cloud REST API v3 uses `https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3`, Basic auth, async request/response calls, endpoint-specific pagination (`nextPageToken` for issue search and offset pagination for projects/comments/users), 10/30-second transport timeouts, a 60-second multi-request deadline, no retries, and explicit 429 handling.

### Code Structure

The shared schema/runner/registry changes live in the existing `shared/src/archie_shared/schemas.py` and `agent/src/archie_agent/exec/{tools/__init__.py,runner.py,tool.py}`. Jira public modules live under `agent/src/archie_agent/exec/tools/jira/`; the skill is `persona/skills/jira/SKILL.md`; tests remain under `tests/` following current tool/config patterns.

### Patterns and Conventions

Use existing decorator registration, typed shared config, `ToolError` subclasses, lazy service imports, async context managers, mocked HTTP at the decorated-function seam, and Google-style docstrings. Explicit decorator namespace metadata is authoritative over module location; existing flat tools remain unchanged.

### Infrastructure and Deployment

No new infrastructure, environment variables, secret transport, Docker mounts, or dependencies are required. Jira credentials remain in the existing mounted `credentials.yaml`; policy is non-secret config passed through the existing exec context.

### Non-Functional Concerns

- Security: enforce project policy before provider access, keep credentials out of arguments/results/errors, confine attachments to `/workspace`, use `O_NOFOLLOW`, and cap files at 10 MiB.
- Reliability: use no automatic mutation retries and surface indeterminate outcomes with verification guidance.
- Performance: cap collections at 100 items, text at 8,000 characters, requests at a 60-second multi-request deadline, and final results at 16,000 characters.
- Observability: tool errors and runner audit records may identify operation names and bounded non-secret arguments, but must not contain credentials or file contents.

### Key Decisions

- Reuse `httpx` rather than add a Jira SDK because it is already an agent dependency and matches the existing async web-tool pattern.
- Use generated structured JQL rather than expose raw JQL because project scope must not be bypassable and model inputs should remain bounded.
- Include bounded comments in issue details; there is no separate comment-read function.

### Risks and Open Questions

- Jira permissions may exceed local policy; local policy limits decorated-tool access but not arbitrary model-authored code in the trusted container (accepted, separate security work).
- Indeterminate mutations may duplicate state if callers retry without verification (mitigated by explicit error guidance and no automatic retry).
- Jira Cloud response shapes may vary (mitigated by bounded normalization and malformed-response errors).
- The shared namespace/config changes affect existing tools (mitigated by M1 compatibility tests).

### Capability grouping

Milestones are capability-oriented vertical slices. Shared foundation work is delivered with the first observable capability and each later milestone adds public behavior, tests, skill guidance, and error/safety handling rather than creating isolated horizontal layers.

## Milestones

### M1 — Jira foundation, shared execution/config seams, and project discovery

#### Approach

Establish the shared config/policy seam, Jira credential loading, async client lifecycle, provider error taxonomy, tool registration, and complete Jira skill while delivering `list_projects` and `get_project`. `get_project` includes project metadata, issue types, and statuses; do not create a separate status function.

The test seam is the decorated `jira` function boundary with mocked `httpx` and temporary credential/config stores. M1 also owns compatibility tests at the registry, runner namespace, native-spec, and permanent-description boundaries.

#### Wiring

`NexusConfig.tools` resolves defaults and Jira scope. Agent tool functions obtain the effective policy, load `JiraCredential` from `ARCHIE_HOME_DIR`, construct the client, call the relevant REST endpoint, normalize the response, and return bounded text. The Jira package is imported by the global exec-tool loader.

#### Edge Cases

- no credential or partial credential → configuration error, no HTTP request;
- read disabled → policy error, no HTTP request;
- project listing with scope → only allowed project summaries are returned;
- direct out-of-scope project → policy-denied error without Jira request;
- Jira 401/403/404/429 → distinct mapped errors;
- malformed project/status payload → safe provider response error.

#### Tasks

1. Add and validate generic `tools` config models, defaults, unknown-provider handling, malformed-policy errors, and session-snapshot loader tests.
2. Extend the decorator/registry, runner injection, native filtering, permanent exec documentation filtering, namespace collision checks, and compatibility tests for existing flat tools.
3. Add the agent-side policy resolution path without adding Jira credential env mappings.
4. Implement Jira credential validation and async client lifecycle/error mapping.
5. Implement ADF/status/project normalization needed by project responses.
6. Implement paginated `list_projects` and combined metadata/status `get_project` with scope handling and all-or-nothing response semantics.
7. Register the Jira namespace and add the complete Jira skill content.
8. Add focused shared, client, tool, registration, namespace, config-lifecycle, and skill tests.

#### Deliverable

A configured session can call `jira.list_projects()` and `jira.get_project()` through `exec`, using stored Jira credentials, with safe defaults and distinct policy/provider errors.

#### Verify

Run focused shared config, Jira client/tool, registration, namespace, native-spec, permanent-description, and skill tests. In a session with mocked Jira responses, call both functions through `exec` and verify that missing credentials, disabled reads, and out-of-scope projects make no provider request. Change the config file after runner startup and verify the running invocation retains its session-start policy; start a new invocation and verify the new policy is observed.

### M2 — Structured issue search

#### Approach

Implement `list_issues` with structured parameters only. Generate JQL from escaped filter values and add the configured read-scope project clause. Use Jira page tokens and bounded page sizes until the requested visible limit is reached. Test at the decorated `jira.list_issues` boundary with mocked async HTTP responses and generated-request assertions.

#### Wiring

The public function validates filters and resolves read policy; the Jira query builder intersects caller project selection with the configured scope; the client paginates Jira page tokens; the formatter returns only bounded issue summaries.

#### Edge Cases

- no filters → bounded recent issue search within read scope;
- caller project outside scope → policy denial;
- empty configured scope → no project clause;
- invalid limit/date/filter values → local validation error;
- no results → stable empty result;
- rate limit/network/malformed response → mapped bounded error.

#### Tasks

1. Define the public structured search signature and validation rules.
2. Implement safe JQL generation, including scope intersection and value escaping.
3. Implement page-token pagination and explicit limit enforcement.
4. Normalize narrow issue summaries and bounded fields.
5. Update the skill with search examples and no-raw-JQL guidance.
6. Add tests for filters, scope, pagination, limits, escaping, and failures.

#### Deliverable

The model can search Jira issues through structured filters without raw JQL and cannot use search parameters to bypass read project scope.

#### Verify

Mock multiple Jira search pages and assert generated JQL contains the configured scope, requested limits are honored, and an out-of-scope project is rejected before HTTP.

### M3 — Issue details

#### Approach

Implement `get_issue(issue_key)` with local key/project validation, bounded full issue formatting, and plain-text ADF description conversion. Comments are intentionally excluded and have their own tool. Test at the decorated `jira.get_issue` boundary with mocked async HTTP responses and assertions on local no-request policy failures.

#### Wiring

The tool extracts a project key from the issue key and resolves read policy before calling the client; the client fetches the issue and comments; the formatter converts ADF and emits the bounded detail shape with comments.

#### Edge Cases

- malformed issue key → local validation error;
- issue project outside read scope → policy denial, not not-found;
- in-scope Jira 404 → not-found;
- description absent or malformed ADF → safe empty/plain representation;
- sensitive/unbounded fields → omit or truncate.

#### Tasks

1. Add issue-key/project extraction and direct scope checks.
2. Implement issue detail request and normalized response.
3. Implement robust bounded ADF-to-text conversion.
4. Map 404 separately from policy and authorization errors.
5. Add skill examples for fetching an issue before requesting comments.
6. Add tests for valid, invalid, out-of-scope, not-found, malformed, and bounded responses.

#### Deliverable

`jira.get_issue(issue_key)` returns safe issue details with bounded comments and accurately distinguishes policy denial from Jira not-found.

#### Verify

Mock direct issue requests and assert no request occurs for blocked project keys; assert an in-scope 404 produces the not-found error and a 403 produces provider authorization error.

### M4 — Comments and user lookup

#### Approach

Implement `search_users(query)` as a read-gated, bounded lookup returning only fields needed for later assignee selection. Comments are returned by `get_issue` using the same issue scope checks and ADF formatting. Test both at the decorated function boundary with mocked async HTTP responses and bounded-output assertions.

#### Wiring

`search_users` validates the query, calls the user endpoint, filters account types, and returns bounded candidates used by later mutation tools. `get_issue` fetches and formats at most 50 comments.

#### Edge Cases

- empty comments → stable empty result;
- malformed comment body → bounded safe text;
- broad/empty user query → reject queries shorter than 1 character and enforce the 200-character query and 50-result limits;
- user results ambiguous → return candidates, never choose silently.

#### Tasks

1. Implement scoped comment retrieval and bounded normalization.
2. Implement bounded Atlassian user search with account type filtering.
3. Define ambiguity behavior for assignee resolution.
4. Update skill guidance for composing bounded issue details and comments from `get_issue`.
5. Add client/tool tests for scope, ADF, limits, and user ambiguity.

#### Deliverable

The model can explicitly fetch comments separately and can discover bounded candidate users without silent identity selection.

#### Verify

Call both functions in a single mocked `exec` program and assert separate outputs, bounded fields, and correct policy/error behavior.

### M5 — Issue creation

#### Approach

Implement `create_issue` as the first write capability. Require write enablement and a permitted project before the provider request. Convert descriptions to minimal ADF and resolve assignees only from explicit/unambiguous user results. When `status` is supplied, create the issue, discover/match the requested transition, apply it, and fetch the resulting issue; a failed transition or required read raises `JiraMutationIndeterminateError` with the created key when known. Test at the decorated function boundary with mocked async HTTP sequences and assert denied calls perform no provider request.

#### Wiring

The tool resolves write policy for the requested project, validates fields, resolves an optional assignee through the client, builds the create payload, submits it, and returns creation metadata when no status is supplied. With `status`, it uses the created issue key for transition discovery/mutation and returns the final bounded issue; uncertainty in the follow-up sequence raises `JiraMutationIndeterminateError`.

#### Edge Cases

- write disabled → policy error without HTTP;
- project outside write scope → policy denial without HTTP;
- empty summary or invalid issue type → local/provider validation error;
- ambiguous assignee → no mutation;
- Jira rejects fields → bounded validation error;
- successful creation → return key/id metadata only.

#### Tasks

1. Define create inputs and required local validation.
2. Implement write policy/project checks.
3. Implement ADF input conversion and field payload construction.
4. Implement explicit assignee resolution behavior.
5. Return bounded creation metadata and update skill safety guidance.
6. Add tests proving denied writes never reach Jira and successful payloads are correct.

#### Deliverable

Issue creation works only when explicitly enabled and scoped, with no silent assignee selection or credential exposure.

#### Verify

Mock create requests for enabled/disabled/in-scope/out-of-scope cases and assert exact payload, no-request denial, bounded metadata, and the status sequence (create → transition discovery → transition → get issue), including indeterminate follow-up failures.

### M6 — Issue updates and transitions

#### Approach

Implement `update_issue` and `transition_issue`. Both pre-check the issue project against write policy. Updates must use explicit optional-field semantics rather than truthiness that makes clearing behavior ambiguous. Transitions must discover and exactly match an available transition before mutating. Test at the decorated function boundary with mocked multi-request HTTP sequences and mutation-payload assertions.

#### Wiring

The tool extracts the issue project and resolves write policy; updates build an explicit field patch and resolve an assignee through the exact/unique matching algorithm when supplied, while `clear_assignee` sends Jira’s null value. Transitions first fetch available transitions, resolve an exact target, then mutate and fetch the resulting issue.

#### Edge Cases

- blocked issue → policy denial without fetching or mutating Jira;
- issue not found within scope → not-found;
- no fields supplied → local validation error;
- target transition unavailable/ambiguous → no mutation;
- transition succeeds but follow-up read fails → raise `JiraMutationIndeterminateError` with the issue key and verification guidance; never return a purported final state.

#### Tasks

1. Define update field presence/clearing semantics.
2. Implement scoped update payloads and resulting issue normalization.
3. Implement transition discovery, exact matching, and mutation.
4. Ensure 401/403/404/429 distinction for writes.
5. Update skill with read-before-write and transition discovery examples.
6. Add tests for field semantics, scope, transition matching, and no-request failures.

#### Deliverable

The model can safely update and transition only in-scope issues when Jira writes are enabled.

#### Verify

Mock update/transition sequences and assert policy checks, exact payloads, transition discovery, and that invalid/ambiguous transitions never issue a mutation.

### M7 — Add comments

#### Approach

Implement `add_comment(issue_key, body)` as a separately gated write capability. Require a non-empty body of at most 8,000 characters, check write policy from the issue key before the request, convert to ADF, and return a bounded comment summary. Test at the decorated function boundary with mocked async HTTP and exact ADF payload assertions.

#### Wiring

The tool validates the body and issue project, resolves write policy, converts text to ADF, submits the comment, and formats the returned comment metadata/body.

#### Edge Cases

- empty/oversized body → local validation error;
- blocked issue or disabled writes → no request;
- Jira 404/403 → correctly classified provider result;
- successful comment → return metadata/body bounded to the output policy.

#### Tasks

1. Add comment input validation and write policy checks.
2. Implement ADF comment payload and normalized response.
3. Add skill guidance requiring explicit user intent for externally visible comments.
4. Add focused mutation and error tests.

#### Deliverable

The model can add an explicitly requested Jira comment only under the write policy.

#### Verify

Mock enabled and denied calls, inspect the ADF payload, and verify no credentials or unbounded body appears in errors/results.

### M8 — Attach files

#### Approach

Implement `attach_file` as a separately documented high-risk write. First validate write enablement and the issue project from the issue key, then validate workspace confinement, symlink/path behavior, the 10 MiB file size, and the fixed extension allowlist before opening or uploading multipart content. Test at the decorated function boundary with temporary workspace fixtures and mocked async multipart HTTP.

#### Wiring

The tool first resolves write policy from the issue key, then opens the validated path race-safely using a workspace directory descriptor, relative components, and `O_NOFOLLOW`; it checks size on the opened descriptor, uploads the descriptor, and formats attachment metadata. The client owns the upload request and closes the file/client deterministically.

#### Edge Cases

- path outside workspace/traversal/symlink escape → local path policy error;
- missing/directory/oversized/disallowed file → local attachment error;
- blocked issue/write disabled → no file read or upload;
- Jira upload failure → bounded provider error without file contents;
- successful upload → metadata only.

#### Tasks

1. Implement the fixed inclusive 10 MiB size and extension allowlist in code and skill.
2. Implement workspace path validation and file checks.
3. Implement scoped multipart upload with `X-Atlassian-Token: no-check` as required by Jira.
4. Return bounded attachment metadata only.
5. Add tests for path, symlink, size/type, policy, upload, and error behavior.
6. Update the skill with explicit disclosure warnings and safe examples.

#### Deliverable

File attachments are available only as an explicitly gated, workspace-confined, bounded capability and cannot silently disclose arbitrary local files.

#### Verify

Use temporary workspace fixtures and mocked multipart responses to verify every rejection occurs before file upload and successful output contains metadata but no contents.

## Acceptance criteria

- All public Jira capabilities in the capability table are available through `exec` under `jira` and absent from native tools.
- The Jira skill is discoverable from the existing `ARCHIE_PERSONA_DIR/skills` root and accurately documents the complete public surface.
- Jira credentials are read from the mounted typed store and never projected as environment variables or accepted as arguments.
- Existing flat tools retain their current exec/native behavior; Jira functions are callable only as `jira.<function>`, excluded from native specs, and absent from permanent exec documentation.
- Policy is a session-start snapshot; config changes do not affect an existing session and are observed after a new session starts.
- Mutation indeterminate errors distinguish pre-transmission transport failures from possible post-request acceptance and include verification guidance without claiming success/failure.
- Missing policy defaults to read enabled/write disabled; configured read/write project scopes are independent and enforced.
- Raw JQL and arbitrary Jira HTTP access are unavailable.
- Search scope is applied in generated JQL before pagination, so limits are meaningful and hidden projects are not returned.
- Direct blocked resources return policy-denied errors rather than not-found errors.
- Agent-kit capabilities are covered or explicitly documented as changed/excluded: project discovery, issue search, issue details, comments, user lookup, create/update, transitions, comment creation, and attachments.
- Tests cover happy paths, registration, configuration defaults, credential failures, policy gates, scope, pagination, ADF conversion, provider error distinctions, mutation safety, and attachment safety.
- `uv run pytest` and `uv run ruff check .` pass for the completed implementation.
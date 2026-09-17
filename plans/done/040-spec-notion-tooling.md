# 040 — Spec: Notion tooling

## Objective

Provide the agent with safe, useful access to Notion through an `exec`-only `notion` namespace. The implementation will cover the useful Notion capabilities currently provided by agent-kit—search, page/database reads, database queries, comments, page creation, page updates, and comments—using Nexus’s async tool architecture, mounted typed OAuth credentials, shared `tools` policy, bounded results, and service skill guidance.

Notion reads are enabled by default when credentials are configured. Writes remain disabled unless explicitly enabled by policy. Page and database scope entries are subtree roots: access is allowed to the configured root and any descendant page, database, or database item when Notion metadata proves the relationship.

## Context

Nexus already registers `notion` as an OAuth provider using the shared `OAuthCredential` shape and discovers the Notion MCP server at `https://mcp.notion.com`. The agent container mounts the Nexus home directory, so the agent can read the typed credential store without a new secret transport. Nexus has no Notion client, tools, skill, or provider-specific policy resolver. The shared policy snapshot and namespaced registry seams are currently exercised by Jira history but must be treated as shared compatibility work: Notion MUST consume the generic invocation snapshot rather than introduce provider-specific environment or file rereads.

The prior agent-kit integration communicates with Notion through the hosted MCP proxy and supports workspace search, page/database fetching, saved-view queries, comments, page creation, page property updates, and page comments. It uses async MCP calls but exposes them through a synchronous CLI. Nexus will use the official async MCP client directly from decorated async tools and will not reproduce the CLI.

Agent-kit’s scope behavior is a useful capability reference but is not copied without adjustment. In particular, its search is unrestricted, its query scope check can occur after rows are fetched, its results are not comprehensively bounded, and its response parsing relies on fragile text markers. Nexus must enforce the local policy before returning data, validate direct resource scope as early as possible, fail closed when ancestry cannot be established, and bound all provider interactions and model-visible results.

This spec is promoted from project `038-project-saas-tooling.md` and honours D1–D11, including the generic `tools` policy shape, safe defaults, credential-store ownership, exec-first exposure, immutable session policy snapshots, bounded results, and the documented limitation that policy does not constrain arbitrary code already available inside the trusted session container.

## Requirements

Each requirement is observable and includes acceptance criteria. Technical design and milestones define implementation details and verification commands.

### Execution and public surface

- Notion MUST initially be available only through the `exec` service namespace.
  - AC: Model-authored exec code can call `await notion.search(...)` and reach the Notion tool.
  - AC: Notion functions do not appear as native `ToolSpec` entries or permanent exec documentation entries.
- Public functions MUST use the shared registration contract with `exec_enabled=True`, `exec_docs=False`, `native=False`, and `namespace="notion"`.
  - AC: Registration exposes functions as `notion.<function>` and preserves existing flat tools.
  - AC: Duplicate namespace/function registration fails without overwriting an existing function.
- The Notion package `__init__.py` MUST import only its intended public modules/functions; MCP helpers and response parsers remain undecorated.
- `persona/skills/notion/SKILL.md` MUST document every public function, inputs, outputs, policy, limits, safe sequencing, and unsupported capabilities without credentials, raw MCP calls, or raw provider payloads.

### Credentials and MCP client

- Notion MUST authenticate with the existing typed `OAuthCredential` loaded through `get_credential("notion")`.
  - AC: `access_token` is read from the mounted credential store and is never a tool argument, result, skill value, or model-visible error.
  - AC: Whitespace-only or missing access tokens produce a sanitized `NotionConfigurationError` before a provider call.
- The client MUST use the official Python MCP SDK and the hosted Notion Streamable HTTP endpoint derived from the registered provider server URL (`https://mcp.notion.com` → `/mcp`).
  - AC: The adapter explicitly constructs or validates the `/mcp` resource endpoint; it does not send MCP calls to the OAuth discovery base URL.
  - AC: The MCP client is async and owns session initialization and cleanup for each tool invocation or documented shared call sequence.
  - AC: The implementation does not add a synchronous provider client or arbitrary HTTP methods.
- The adapter MUST pin and test one concrete official MCP SDK API contract before implementation is considered ready.
  - AC: The plan’s dependency milestone records the selected SDK version/range, transport constructor, session initialization sequence, content-block shape, and exception classes used by the adapter.
  - AC: Unsupported SDK/server tool schemas fail with a sanitized `NotionResponseError` or `NotionConfigurationError`, never with a silent unrestricted fallback.
- The implementation MUST support the current OAuth credential lifecycle already owned by Nexus.
  - AC: It uses the stored access token and does not implement a second OAuth/token store.
  - AC: Expired/invalid credentials map to a sanitized authentication/configuration error; token refresh remains the existing auth/orchestrator responsibility.
- MCP calls MUST have bounded lifecycle and no automatic mutation retries.
  - AC: Each public invocation creates a monotonic deadline 60 seconds after entry and passes the remaining budget through every adapter call; per-request connect/read/write timeouts are also bounded.
  - AC: Connection, initialization, tool calls, and multi-call operations stop when the shared deadline expires.
  - AC: Sessions close on success and failure.
  - AC: A mutation whose transmission or outcome is uncertain raises an indeterminate mutation error and is not repeated automatically.

### Policy and scope

- Nexus MUST use the common `tools.notion.read/write` policy with read-enabled/write-disabled defaults and opaque provider scope.
  - AC: Missing Notion policy resolves to reads enabled and writes disabled.
  - AC: Missing or empty scope means unrestricted access.
  - AC: Malformed common policy fails validation; unknown provider entries do not block Notion or unrelated providers.
- Notion scope MUST be a strict provider-specific object:
  ```yaml
  pages: [<Notion page IDs>]
  databases: [<Notion database/data-source IDs>]
  ```
  - AC: `pages` and `databases` are lists of normalized Notion IDs, each bounded to 100 entries.
  - AC: Unknown scope fields, malformed IDs, non-list values, and excessive lists fail when Notion policy resolves, before credential loading or an MCP request.
- Configured page and database IDs MUST be subtree roots.
  - AC: A root itself is allowed.
  - AC: A descendant page, nested database/data source, or database item is allowed when returned Notion metadata proves that it descends from a configured root.
  - AC: A resource with no reliable ancestry metadata is not treated as in scope.
  - AC: A database scope authorizes that database/data source and descendants/items, not unrelated pages.
- Read and write scopes MUST be independent.
  - AC: A resource can be readable but not writable, or writable only where the write root permits it.
  - AC: Standalone read/write gates are checked before credentials are loaded wherever local validation can decide the result.
- Provider preflight reads needed to establish ancestry MUST be explicitly documented.
  - AC: For a descendant resource, Notion metadata may be fetched to decide scope, but no unauthorized content/result is returned to the model.
  - AC: A scope denial does not reveal hidden titles, IDs, counts, or provider error details.
  - AC: Writes use the write policy for all internal reads required to establish the parent/target scope; read enablement cannot grant write-internal access.

### Public capabilities

Notion MUST expose these functions with the following fixed initial signatures. Optional arguments use `None` to mean omitted; limits reject booleans and are inclusive.

```python
async def search(query: str, *, limit: int = 10, resource_type: str | None = None) -> list[dict]
async def get_page(id_or_url: str, *, include_properties: bool = False) -> dict
async def get_database(id_or_url: str, *, include_views: bool = True) -> dict
async def query_database(id_or_url: str, *, view: str | None = None, filters: list[str] | None = None, sort: str | None = None, columns: list[str] | None = None, limit: int = 50) -> list[dict]
async def get_comments(id_or_url: str, *, limit: int = 50) -> list[dict]
async def create_page(parent_id_or_url: str, *, title: str | None = None, properties: dict[str, str] | None = None, content: str | None = None) -> dict
async def update_page(id_or_url: str, *, properties: dict[str, str] | None = None, replacements: list[dict[str, str | bool]] | None = None) -> dict
async def add_comment(id_or_url: str, body: str) -> dict
```

`resource_type` is `page` or `database`; `filters` contains bounded `Property=Value`, `Property!=Value`, or `Property~=Value` expressions; `sort` is one bounded `Property:asc` or `Property:desc` expression; properties are non-empty string keys and values; replacements require non-empty `old`, may contain `new`, and default `replace_all` to false. Each function returns the stable bounded projection described by its function section and the skill.

Notion MUST expose these functions:

| Capability | Function | Classification |
|---|---|---|
| Workspace/content search | `search` | read |
| Page content and metadata | `get_page` | read |
| Database/data-source schema and views | `get_database` | read |
| Saved-view/database rows | `query_database` | read |
| Page comments | `get_comments` | read |
| Create child page | `create_page` | write |
| Update page properties/content | `update_page` | write |
| Add page comment | `add_comment` | write |

- Every function MUST be async, credential-free, JSON-serializable, and bounded.
- Raw MCP tool invocation, arbitrary Notion API methods, page deletion, database/schema mutation, view creation/update, file upload, and unrestricted block mutation are out of scope for this spec.
- `search` MUST accept a bounded query, optional page/database type filter, and a maximum result limit.
  - AC: With unrestricted scope, it performs one bounded global search and returns at most `limit` results.
  - AC: With any configured scope, it still performs one bounded global search, then returns only results whose metadata proves membership in an allowed root.
  - AC: Scoped search is allowed to return fewer than `limit`; it does not perform per-root searches, unbounded retries, or N+1 metadata fetches.
  - AC: Search results are deduplicated by stable resource ID and hidden-result counts are not disclosed.
  - AC: Results lacking sufficient scope metadata are omitted when scoped search is active.
- `get_page` MUST accept a page ID or supported Notion page URL and return bounded page content, metadata, and optionally properties.
  - AC: Direct roots are authorized locally; descendants are authorized only after ancestry metadata is established.
  - AC: Content, rich text, properties, icons, covers, and timestamps are normalized to a stable bounded projection; signed file URLs and unnecessary sensitive fields are omitted or treated as ephemeral metadata.
- `get_database` MUST return a bounded database/data-source schema and available saved-view metadata.
  - AC: Scope is checked before schema is returned.
  - AC: Partial or malformed schema/view metadata is a provider response error, not an unbounded raw response.
- `query_database` MUST query one explicitly selected or deterministically selected saved view/data source and support bounded local projection.
  - AC: Database scope is established before the row query is made whenever the database ID itself is supplied.
  - AC: The result limit is enforced before return and provider query limits are bounded.
  - AC: Optional view selection, simple property filters, sort, and column projection are validated and bounded.
  - AC: The implementation does not fetch an unbounded complete view merely to apply a local limit.
- `get_comments` MUST return bounded comments for an in-scope page and MUST not expose comments for a blocked page.
- `create_page`, `update_page`, and `add_comment` MUST require enabled write policy and a permitted root/target.
  - AC: Denied writes make no mutation call.
  - AC: Page creation checks the parent scope before creation; created descendants are governed by the parent’s write scope.
  - AC: Page updates and comments check target scope before mutation.
  - AC: Externally visible writes require explicit user intent as documented by the skill.
- Page writes MUST have deliberately bounded initial semantics:
  - page creation supports a parent, optional title, bounded simple properties, and bounded plain/markdown content;
  - page updates support bounded property replacement/clear semantics and bounded content replacement operations only where the MCP contract supports deterministic matching;
  - comments support bounded plain/markdown text;
  - native arbitrary property JSON, deletion/archive, schema mutation, and unrestricted block operations are not exposed.
- Mutation results MUST be verified when the provider returns a reliable resource representation; failed required verification after a possible mutation raises `NotionMutationIndeterminateError`.

### Data, errors, and safety

- Results MUST be stable JSON projections, not raw MCP content blocks or provider payloads.
  - AC: Rich text is normalized to bounded plain/markdown text.
  - AC: Collections, text, properties, view metadata, comments, and rows have explicit per-field limits.
  - AC: Provider-level bounding occurs before the existing 16,000-character exec result cap; required identifiers are retained or `NotionResultTooLargeError` is raised.
- Input validation MUST reject booleans where integer limits are expected, empty required strings, malformed IDs/URLs, excessive text/property/filter sizes, and unsupported property/filter forms before provider access.
- Errors MUST preserve distinct sanitized categories where MCP supports the distinction:
  - `NotionConfigurationError`;
  - `NotionPolicyError` and `NotionWritePolicyError`;
  - `NotionAuthenticationError`;
  - `NotionAuthorizationError`;
  - `NotionNotFoundError`;
  - `NotionRateLimitError`;
  - `NotionValidationError`;
  - `NotionTransportError`;
  - `NotionResponseError`;
  - `NotionMutationIndeterminateError`;
  - `NotionResultTooLargeError`.
- The existing exec error path MUST serialize `ToolError` subclasses using type and sanitized message only; traceback text MUST remain internal/non-model-visible for all provider failures.
- Errors MUST not contain access tokens, credential paths, raw MCP payloads, signed URLs where unnecessary, hidden resource identifiers, or provider stack traces.
  - AC: The exec error-envelope path is updated so `NotionConfigurationError`, policy errors, and mapped provider errors are serialized without `traceback` text; internal traceback logging, if retained, is non-model-visible and redacted.
- No mutation MUST be automatically retried after a request may have been transmitted.

## Technical Design

### Technical stack

- Use the official `mcp` Python SDK as a new agent dependency, pinned to a compatible major/minor range after confirming the SDK’s async Streamable HTTP API.
- Reuse `msgspec`, the shared credential store, pytest, pytest-asyncio, and existing async lifecycle conventions.
- Do not add a Notion REST SDK or duplicate OAuth implementation.

### Architecture and locations

- `shared/src/archie_shared/tool_policy.py` and existing config modules — retain the generic policy model and session snapshot; no Notion-specific scope logic belongs here.
- `agent/src/archie_agent/exec/tools/notion/` — public functions and provider-specific helpers.
- `notion/client.py` — lazy MCP SDK import, authenticated session lifecycle, bounded calls, timeout/deadline, and MCP error mapping.
- `notion/policy.py` — resolve the common policy snapshot and validate page/database root scope; normalize IDs and ancestry metadata.
- `notion/formatting.py` — parse MCP content blocks, normalize page/database/comment/row projections, extract stable IDs/path/ancestor metadata, and apply bounds.
- `notion/search.py` — global search request, result normalization, deduplication, and scoped metadata filtering.
- `notion/reads.py` — page/database/comments/query read operations.
- `notion/writes.py` — create/update/comment operations, write preflight, verification, and indeterminate outcomes.
- `agent/src/archie_agent/exec/tools/notion/__init__.py` — import only public Notion modules for registration.
- `agent/src/archie_agent/exec/tools/__init__.py` (or the existing central loader) — import the Notion package alongside Jira so registration actually occurs.
- `persona/skills/notion/SKILL.md` — complete model-facing contract and safe examples.
- `agent/pyproject.toml` — add the official MCP dependency; no environment projection is added.
- `tests/` — shared compatibility, Notion policy, MCP client, formatting, public-tool, lifecycle, and skill tests.

Use explicit decorator metadata as in Jira. Imports of the MCP SDK and service implementation should remain lazy enough that sessions without Notion credentials do not fail at general agent startup.

### Data flow

1. Agent startup loads `NexusConfig` and creates the detached non-secret policy snapshot.
2. The exec runner installs that snapshot before injecting the Notion namespace; it never rereads `config.yaml`.
3. A Notion function validates local arguments and resolves the Notion provider policy.
4. The function classifies itself as read, write, or write-with-internal-read and checks the relevant gate/scope. The runner’s generic invocation context is the sole policy source; Notion MUST NOT reread `config.yaml` or depend on an undocumented provider-specific environment variable.
5. If descendant scope must be established, the function first resolves the local policy and then loads the credential/creates the authenticated MCP session; it performs only the documented metadata preflight through that session. The preflight result is discarded on denial and content is not returned before authorization.
6. The runner MUST install the snapshot through a shared provider-neutral context API (for example, a context variable or explicit invocation context accessor) consumed by Notion and Jira policy resolvers; provider-specific setters and config/environment fallbacks are compatibility shims only and MUST NOT be required by new providers.
7. The function invokes only the fixed MCP operations required by its public contract.
8. MCP content blocks are parsed, normalized, scope-filtered, bounded, and returned as JSON-serializable values.
9. A required post-mutation verification failure raises an indeterminate error rather than claiming final state.

### Scope model and search behavior

Scope is represented as normalized Notion IDs in `pages` and `databases`. Empty lists in both fields mean unrestricted. A root matches itself and any descendant page, database/data source, or database item when the provider response supplies a trustworthy path/ancestor chain. Resource type must be respected: a database root authorizes its data source/items and descendants; a page root authorizes descendants of that page, including nested databases/items.

For direct reads and writes, the implementation should reject a resource locally when it is not itself a configured root. For descendants, it may fetch provider metadata to establish ancestry, but it must not return the fetched content until authorization succeeds. If the response lacks sufficient structured path/ancestor information, the operation fails closed with a policy error or sanitized scope-validation error.

For `search`, the implementation deliberately uses one bounded global search regardless of the number or type of configured roots. It filters the returned results locally using stable IDs, resource type, and path/ancestor metadata. It deduplicates by stable ID and returns at most the requested limit. A scoped search may return fewer results even when more matching content exists because this version does not perform per-scope searches or pagination retries. This is a known optimization boundary that can be revisited if search quality becomes problematic.

Search results are ordered by the provider’s returned relevance/order. The implementation MUST NOT claim newest-first ordering unless the provider result supplies and the function explicitly uses a reliable edit timestamp. Missing scope metadata is not evidence of authorization and is omitted under scoped policy.

### MCP operation adapter

The adapter should support the currently documented operations needed by this spec: `notion-search`, `notion-fetch`, `notion-query-data-sources` or the compatible saved-view query operation, `notion-get-comments`, `notion-create-pages`, `notion-update-page`, and `notion-create-comment`. The exact SDK call shape and current tool schemas must be verified against the official SDK/documentation during implementation and covered by adapter tests.

The implementation MUST inspect the connected server/tool capability response where needed to select the supported search/query operation, but must not silently downgrade to an unrestricted or less-safe operation. Unsupported workspace-plan/tool states produce a distinct sanitized provider/configuration error. Tool names and arguments are isolated in the adapter so provider schema changes do not spread through public functions.

### Result bounds

Use explicit limits, subject to adjustment only in the implementation if the same safety guarantees remain:

- search: requested `1..50`, one provider batch capped at 50;
- collection fields/comments/views: maximum 50 items;
- database rows: requested `1..100`, provider limit capped at 100;
- identifiers: normalized to canonical ID strings;
- text fields: at most 8,000 characters each;
- properties per page/row: at most 50, with bounded property values;
- overall provider-specific serialized result: at most 16,000 characters.

The public skill documents these as maxima. A global exec cap remains a backstop, not the primary service-level bound.

### Compatibility and security

No secrets are projected into environment variables for Notion. The shared OAuth provider may remain configured/authenticated without Notion tools being enabled or credentials being present. Registration and credential-provider discovery remain independent.

Local SaaS policy limits decorated Notion functions only. It does not prevent arbitrary code in the trusted session container from accessing the network, filesystem, subprocesses, or installed libraries; the skill and project documentation must retain this limitation.

## Capability mapping

| Agent-kit capability | Nexus function | Notes |
|---|---|---|
| `notion search` | `search` | one bounded global search; scoped results filtered locally |
| `notion page` | `get_page` | ID/URL input; bounded content and optional properties |
| `notion db` | `get_database` | schema and bounded view metadata |
| `notion query` | `query_database` | bounded saved-view/data-source rows and local projection |
| `notion comments` | `get_comments` | bounded page comments |
| `notion create-page` | `create_page` | write-gated child-page creation |
| `notion update-page` | `update_page` | write-gated bounded property/content updates |
| `notion comment` | `add_comment` | write-gated comment creation |
| arbitrary MCP/API calls | excluded | no generic provider surface |
| file upload, database/view/schema mutation | excluded | separate future scope |

## Milestones

### M1 — Notion foundation, shared seams, MCP adapter, and search

#### Approach

Extend the existing Jira-proven shared registry/config/session-policy seams only as needed, add the official MCP dependency, implement Notion credential validation and async MCP lifecycle, and deliver the first observable capability: bounded `notion.search`.

The test seam is the decorated function boundary with an injected/mock MCP session or transport adapter, temporary credential/config stores, and explicit runner namespace tests. MCP SDK-specific construction is tested separately at the adapter boundary so public tool tests do not depend on network or SDK internals.

#### Wiring

The runner provides the immutable policy snapshot. `notion.search` resolves read policy, creates one authenticated MCP session, performs one bounded global search, normalizes/deduplicates results, applies configured scope filtering, and returns a bounded projection. With no configured roots it preserves provider ordering; with roots it omits results lacking proof of membership.

#### Edge Cases

- missing/partial/invalid OAuth credential → sanitized configuration error, no MCP session;
- read disabled → policy error, no credential/provider access;
- malformed scope → provider policy validation error before credentials;
- unrestricted search → one call, at most 50 results;
- scoped search with out-of-scope or metadata-poor results → omit silently;
- duplicate IDs → one result;
- provider auth/rate-limit/transport/tool-unavailable error → distinct sanitized error;
- config changed after startup → current session retains the original snapshot.

#### Tasks

1. Add the official async MCP SDK dependency at the concrete selected version/range recorded in the implementation change, and confirm/test the supported Streamable HTTP transport constructor, initialization sequence, content-block shape, and exception classes before implementing public adapter calls.
2. Implement the provider-neutral runner policy-snapshot context with an explicit accessor and migration compatibility for Jira/flat tools, update the exec error envelope to hide tracebacks from model-visible `ToolError` results, then implement Notion credential loading, token validation, lazy client/session lifecycle, shared deadlines, cleanup, and error mapping.
3. Implement strict Notion scope parsing for page/database roots and resolution from the invocation snapshot.
4. Implement MCP content parsing and bounded stable search-result projection.
5. Implement one-call global search, stable-ID deduplication, and scoped path/ancestor filtering.
6. Register the Notion namespace and preserve flat/native/permanent-documentation compatibility.
7. Add initial `persona/skills/notion/SKILL.md` covering search and global rules.
8. Add shared, registration, credential, MCP adapter, policy, search, snapshot, and skill tests.

#### Deliverable

A configured session can call `notion.search(query, limit, type)` through `exec`, with safe defaults, bounded output, and fail-closed scoped filtering.

#### Verify

Run focused config/policy, exec registration, Notion adapter, search, and skill tests. Mock a search response containing duplicate, in-scope, out-of-scope, and metadata-poor results; assert only permitted stable projections are returned and the provider is called once. Change `config.yaml` after runner startup and verify the existing session retains its policy while a new session observes the change.

### M2 — Page and database reads

#### Approach

Implement `get_page` and `get_database` using the shared MCP adapter. Normalize IDs from direct IDs and supported URLs, establish direct/root/descendant scope before returning content, and produce bounded projections rather than raw MCP text. Use provider metadata preflight only where required to prove ancestry, with fail-closed behavior.

#### Wiring

The read tools validate identifiers and resolve read scope. The adapter fetches the resource, the formatter extracts type/ID/path/ancestors and content, and the policy layer authorizes the resource before the projection is returned. Database output includes schema and bounded saved-view metadata.

#### Edge Cases

- malformed URL/ID → local validation error;
- root resource → local scope match;
- descendant resource → metadata proves root ancestry, then return;
- absent/ambiguous ancestry → deny or response error without content;
- provider 401/403/404/429 → distinct mapped errors;
- large/truncated page → bounded content with explicit truncation metadata, never raw overflow;
- malformed page/database payload → response error.

#### Tasks

1. Implement canonical ID/URL parsing and resource-type validation.
2. Implement structured path/ancestor extraction and page/database root matching.
3. Implement bounded page projection with optional properties and safe metadata fields.
4. Implement bounded database/schema/view projection.
5. Add read policy/preflight ordering and error behavior.
6. Extend the skill with page/database signatures, schemas, and scope examples.
7. Add tests for roots, descendants, cross-type scope rejection, malformed metadata, bounds, and provider failures.

#### Deliverable

`notion.get_page` and `notion.get_database` return bounded, scope-authorized projections and never expose blocked or metadata-unverifiable resources.

#### Verify

Mock root, descendant, unrelated, and metadata-poor fetch responses. Assert blocked resources produce no model-visible content, root/descendant resources return stable projections, and malformed/provider errors retain their documented categories.

### M3 — Database queries and comments

#### Approach

Implement `query_database` and `get_comments` after database/page read authorization exists. Establish database scope before querying rows when possible. Use the currently supported bounded data-source/view query operation through the adapter, validate optional view/filter/sort/column parameters, and enforce provider and service limits before return.

Local convenience filters remain deliberately narrow and typed around normalized row properties. Do not expose raw Notion filter JSON or arbitrary query strings. The implementation must not download an unbounded complete view just to apply a local limit.

#### Wiring

`query_database` authorizes the database, resolves a selected/default view through the adapter, requests bounded rows, applies bounded local projection, and returns rows plus only safe view metadata. `get_comments` authorizes the page, fetches comments, normalizes rich text, and bounds the collection.

#### Edge Cases

- database outside scope → policy error before row query;
- unavailable/ambiguous view → validation/provider error without raw view payload;
- unsupported filter/property type → local validation error;
- provider returns more than the cap → truncate deterministically and mark truncation where useful;
- empty rows/comments → stable empty lists;
- query/comment response malformed → response error;
- comments on blocked page → no comment request when local scope can decide, otherwise no returned comments.

#### Tasks

1. Implement bounded database/data-source/view selection and adapter compatibility checks.
2. Implement query arguments, property filters, sort, columns, and limit validation.
3. Implement bounded row normalization and projection.
4. Implement page comment retrieval and rich-text normalization.
5. Add rate-limit, transport, malformed-response, and result-size handling.
6. Update the skill with query/filter limitations and comment sequencing.
7. Add tests for pre-query scope enforcement, limits, filters, views, columns, comments, and failures.

#### Deliverable

The model can query permitted Notion databases and read permitted page comments through stable, bounded functions without raw query/API access.

#### Verify

Mock a database schema followed by a bounded row response and assert the scope check precedes the row call. Assert invalid views/filters and blocked databases make no row request, and all returned rows/comments satisfy size and field bounds.

### M4 — Page creation, updates, and comments

#### Approach

Add the write surface only after read policy, scope, result formatting, and MCP error mapping are established. Require write enablement and write-root authorization before each mutation. Keep input semantics narrow and explicit; do not expose generic Notion property JSON or arbitrary block operations.

Use write-scoped internal metadata reads when the provider must establish a descendant target or verify the resulting resource. A possible post-transmission failure is indeterminate; no automatic retry is allowed.

#### Wiring

`create_page` authorizes the parent, validates title/properties/content, submits one bounded creation request, and returns a bounded created-page projection or an indeterminate error. `update_page` authorizes the page, validates a bounded property/content update, mutates, and verifies where supported. `add_comment` authorizes the page, submits bounded rich text, and returns a bounded comment projection.

#### Edge Cases

- writes disabled or target outside write scope → policy error and zero mutation calls;
- read enabled but write disabled → internal write preflight cannot proceed;
- invalid/empty title, property, content, or comment → local validation error;
- ambiguous provider resource type → validation/policy error before mutation;
- 401/403/429 → mapped provider error with no retry;
- post-transmission transport failure or failed verification → mutation-indeterminate error;
- oversized result → result-size error retaining only safe identifiers when available.

#### Tasks

1. Define bounded public write signatures and explicit property/content semantics.
2. Implement write policy and parent/target root authorization, including write-scoped internal reads.
3. Implement page creation and bounded created-page projection.
4. Implement page property/content updates with deterministic validation and verification.
5. Implement comment creation and bounded comment projection.
6. Add mutation uncertainty classification and no-retry behavior.
7. Complete the Notion skill with confirmation, write sequencing, error, and retry guidance.
8. Add comprehensive decorated-boundary tests for denied writes, successful writes, provider errors, verification, and indeterminate outcomes.

#### Deliverable

The model can safely create/update pages and add comments under explicitly enabled write roots, with no secret exposure, no unbounded payloads, and honest uncertain-mutation errors.

#### Verify

Mock complete MCP call sequences for successful and denied writes. Assert policy failures occur before mutation, write-scope internal reads use write policy, successful operations return bounded projections, and post-transmission failures never claim success or trigger automatic retries.

## Acceptance criteria

The Notion implementation is complete when:

- `NexusConfig` accepts strict common `tools.notion` policy blocks and Notion-specific page/database root scopes with safe defaults.
- Invalid recognized-provider scope fails before credential/provider access; unknown providers remain forward-compatible.
- Policy is a session-start immutable snapshot covered by compatibility tests.
- `notion` functions are exec-only, namespaced, absent from native/permanent documentation surfaces, and do not alter existing flat tools.
- The official async MCP client uses the existing typed OAuth credential store and bounded lifecycle without a parallel auth mechanism.
- Search performs one bounded global call and locally filters scoped results, deduplicating stable IDs and omitting metadata-poor results; the plan explicitly permits fewer than the requested limit.
- Page, database, query, comment, and write functions enforce root/descendant scope and return bounded stable projections rather than raw MCP payloads.
- Read, write, and write-with-internal-read evaluation order is documented and covered by tests.
- Writes are disabled by default, require explicit write scope, have no automatic retries, and distinguish indeterminate outcomes.
- The Notion skill documents the complete public contract, limits, scope behavior, safe sequencing, unsupported operations, and error guidance without credentials.

## Status

Spec status: planned; implementation is not started.

Parent project: `038-project-saas-tooling.md` — M4 Notion tooling.

## Not yet specified

The following are intentionally left to implementation-time verification because the hosted MCP server and official SDK may evolve:

- Exact current response schemas and tool names for saved-view/data-source queries, including plan-dependent capability reporting.
- Whether the connected workspace exposes structured path/ancestor metadata for every supported resource type. M2 is gated on confirming a metadata-bearing preflight contract; if it cannot be confirmed, descendant-scoped operations are not promoted and the affected capability remains unsupported rather than weakening fail-closed scope enforcement.
- The exact subset of Notion property types and content replacement operations that can be safely represented by the narrow public contract.
- Whether a future revision should use per-root search, provider-side location filters, or another strategy if one global scoped search returns too few useful results.

These items may refine adapter internals and supported input shapes but MUST NOT weaken the locked policy, credential, exec exposure, bounding, or no-secret requirements above.

## Open decisions deferred to future work

- Per-root search/merge optimization if global scoped search quality is insufficient.
- Search result ordering beyond the provider’s returned order.
- File upload and attachment capabilities.
- Database schema, view, folder, archive, and deletion mutations.
- Broader native Notion property and block content support.
- Additional Notion MCP tools exposed by future provider versions.

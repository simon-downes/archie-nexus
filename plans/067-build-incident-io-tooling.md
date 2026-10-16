# 067 — incident.io read-only tooling and skill

Status: reviewing
Branch: build-it/067-incident-io-tooling (base: main @ a1d97e6)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/067-incident-io-tooling

## Objective

Give the agent read-only access to incident.io through an exec-only `incident_io` namespace and a
matching `persona/skills/incident-io/SKILL.md`, following the existing Jira/Slack tool patterns.
When done, model code can list and read incidents with their timeline, updates, participants,
alerts, actions, follow-ups and post-mortems, and can read ownership, on-call, catalog and
reference data. The skill documents the full public surface, limits and errors.

## Context

The user asked: "let's build the incident.io tool and skill; follow the same structure/patterns as
the existing tools (google, slack, jira, etc); credentials should already be available — they
provide read-only access; use them for testing as needed but with suitable care."

The design scope is the brain entry `projects/archie/datadog-incident-operations-tools.md`
(incident.io section): read-only wrapper, no writes of any kind (declare, update, timeline,
escalations, follow-ups, status pages, catalog, config).

Codebase facts:

- Commit a1d97e6 added the `incident_io` static provider (`api_key` field) and
  `IncidentIoCredential` in `shared/src/archie_shared/credentials/models.py`. API host is
  `https://api.incident.io/`, Bearer auth (`docs/auth-credentials.md`).
- Closest pattern: `agent/src/archie_agent/exec/tools/jira/` (static credential, httpx client,
  sanitized typed errors, `policy.py` using `archie_shared.tool_policy.resolve_provider_policy`,
  `@tool(native=False, exec_docs=False, namespace="jira")`). Slack's
  `agent/src/archie_agent/exec/tools/slack/errors.py` shows a shared error base class and
  `retry_after_seconds` on rate limits.
- Tool packages are registered by import in `agent/src/archie_agent/exec/tools/__init__.py`
  `get_all_tools()`; the runner groups `_namespace` functions into a `SimpleNamespace` per name
  (`agent/src/archie_agent/exec/runner.py:92-102`).
- `tools:` config is an open provider map (`shared/src/archie_shared/schemas.py:115`), so
  `tools.incident_io` needs no schema change. Missing policy = read enabled, write disabled.
- Exec results are truncated at 16,000 characters (`agent/src/archie_agent/exec/tool.py:28`).

Live API facts (checked with the read-only key, field names and status codes only):

- `/v1/identity` 200; roles are viewer-only; rate limit is 1200 requests per 60 s.
- OpenAPI spec: `https://api.incident.io/v1/openapiV3.json`. Account has ~40 incidents, alerts,
  actions, follow-ups, 5 schedules, escalation paths, 29 catalog types, teams, post-mortems;
  no status pages.
- List endpoints paginate with `page_size` + `after` and return `pagination_meta.after`.
  `page_size` above the server maximum is silently clamped (251 returned 200).
- `/v2/incidents` filters use deepObject query keys such as `status_category[one_of]=live`,
  `created_at[gte]=2026-01-01`, `severity[one_of]=<severity ID>`. Incident date filters accept
  only `YYYY-MM-DD` (a datetime is rejected with `invalid_value`); alert, action and follow-up
  date filters accept full datetimes. An unknown enum value returns
  200 with an empty list, and a severity *name* matches nothing — filters need IDs, so the tool
  must validate enums locally and resolve names to IDs.
- `GET /v2/incidents/{id}` accepts the ULID or the bare reference number (`123`); the full
  reference `INC-123` returns 404.
- Error body: `{"type", "status", "request_id", "errors": [{"code", "message"}]}`.
- `/v1/postmortem_documents/{id}/content` returns `{"markdown": "..."}`.
- `/v2/schedule_entries` takes `schedule_id`, `entry_window_start`, `entry_window_end` and returns
  `{"scheduled", "overrides", "final"}` lists.
- Enums: status category `triage declined merged canceled live learning closed paused`; alert
  status `firing resolved`; action/follow-up status `outstanding completed deleted not_doing`;
  incident mode `standard retrospective test tutorial`; `sort_by` `created_at_newest_first`,
  `created_at_oldest_first`.

## Baseline

- Check commands: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`
- Setup: `uv sync`
- Setup leftovers: none (`.venv` is ignored)
- Failing before this work: none (1328 passed; ruff clean)
- Not included: `persona/skills/aws-agent-access/`, `plans/060-spec-container-aws-profiles.md`
  (untracked in the user's checkout, unrelated)

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Scope of the first build | Full read-only surface: incidents (+timeline, updates, participants, linked alerts), alerts, actions, follow-ups, post-mortems, teams, users, catalog, schedules/on-call, escalation paths, reference data | Core only (~6 functions); everything readable incl. status pages/workflows/transcripts | Covers the design doc's use cases; account has no status pages | user Q1 |
| D2 | Writes | None; no write functions and no write policy check | Policy-gated writes like Jira | Design doc excludes incident.io writes; key is viewer-only | self |
| D3 | Namespace and package name | `incident_io` namespace, package `agent/src/archie_agent/exec/tools/incident_io/`, skill dir `persona/skills/incident-io/` with `name: incident-io` | `incidentio`; skill named `incident_io` | Namespace must be a Python identifier and matches the credential provider name; skill names in the catalog are hyphenated (`build-it`, `create-skill`, `workflow-plan`) | self |
| D4 | HTTP client | Reuse `httpx` with a single-attempt async client modelled on `jira/client.py` | incident.io SDK; automatic retries | No new dependency; no-retry is the established Jira/Slack contract | self |
| D5 | Read policy scope | Support `read.enabled` only; reject a non-empty `read.scope` with a policy error | Team-ID scope filtering | incident.io objects carry team IDs inconsistently (incidents have `team_ids`, alerts do not), so a scope could not be enforced on every function; failing closed avoids a false sense of restriction | self |
| D6 | Filter interface | Structured keyword filters; enums validated locally; severity/type/status accepted by name or ID and resolved to IDs via reference endpoints | Raw deepObject filter dicts; IDs only | Matches Jira's "no raw query" rule; live check showed unknown values silently return nothing, which would mislead the model | self |
| D7 | Incident identifier input | Accept ULID, `INC-123`, or `123`; normalise `INC-123` → `123` | Pass through unchanged | Live check: API 404s on `INC-123`, but users and Slack channels use that form | self |
| D8 | Output shape | Normalised, bounded dicts per resource (Jira `formatting.py` style); text fields truncated at 8,000 chars; lists capped | Raw provider payloads | Skill rules forbid raw payloads; 16,000-char exec limit | self |
| D9 | Pagination | Each list function takes `limit` (1–100, default 25) and follows `after` cursors until `limit` or exhaustion; no cursor exposed | Expose `after` cursor to the model | Same contract as `jira.list_issues`; 100 × compact record fits typical use | self |
| D10 | Rate-limit error | `IncidentIoRateLimitError` with `retry_after_seconds` parsed from `Retry-After` only (non-negative integer seconds, else None) | Also derive from `X-RateLimit-Reset`; plain message only | Slack pattern (`parse_retry_after`); the reset header is an epoch timestamp that would need clock math for little gain | self |
| D11 | On-call "who is on call now" | `incident_io.on_call(schedule, start=None, end=None)` returning the `final` entries, schedule by ID or exact name | Return all three entry lists | `final` already applies overrides; fewer tokens | self |
| D13 | Error names for local vs provider validation | `IncidentIoInputError` (local, before any request) and `IncidentIoRequestRejectedError` (provider 400/422) | Jira-style `...ValidationError` for provider rejects | Two near-synonyms would confuse model code and the skill | self |
| D12 | Module layout | `client.py` (credential, transport, errors), `policy.py`, `formatting.py`, `validation.py`, then `incidents.py`, `alerts.py`, `work.py` (actions, follow-ups, post-mortems), `directory.py` (teams, users, catalog), `on_call.py`, `reference.py` | One large module | Mirrors Jira/Slack's per-area modules | self |

## Requirements

- MUST expose these async functions only under the `incident_io` exec namespace, registered with
  `@tool(native=False, exec_docs=False, namespace="incident_io")`:
  `list_incidents`, `get_incident`, `incident_timeline`, `incident_updates`,
  `list_alerts`, `get_alert`, `list_actions`, `list_follow_ups`, `list_postmortems`,
  `get_postmortem`, `list_teams`, `list_users`, `list_catalog_types`, `list_catalog_entries`,
  `list_schedules`, `on_call`, `list_escalation_paths`, `reference`.
  - AC: `get_all_tools()` contains `incident_io.<name>` for each of the 18 functions, each with
    `_native is False` and `_exec_docs is False`; no other `incident_io.*` keys exist.
  - AC: exec code `await incident_io.reference("severities")` resolves through the runner namespace
    (runner namespace test with a mocked client).
- MUST NOT issue any HTTP method other than GET to incident.io.
  - AC: a test asserts every request made by the client across the tool suite uses `GET`; the
    client exposes no method parameter.
- MUST load the API key only via `get_credential("incident_io")` and send it as a Bearer token to
  `https://api.incident.io`; the key never appears in arguments, results, errors or the skill.
  - AC: missing, empty or unreadable credential raises `IncidentIoConfigurationError` before any
    request is made.
  - AC: an error raised for a 401 response does not contain the key string.
- MUST honour `tools.incident_io.read.enabled`; disabled reads raise `IncidentIoPolicyError`
  before any request. A non-empty `read.scope` raises `IncidentIoPolicyError` (unsupported).
  The policy is documented in the skill and in one sentence of `docs/config.md`.
  - AC: tests for disabled read, unsupported scope, and missing policy (read enabled).
  - AC: `docs/config.md` mentions `tools.incident_io` reads-only policy.
- MUST map provider failures to sanitized typed errors: 401 → `IncidentIoAuthenticationError`,
  403 → `IncidentIoAuthorizationError`, 404 → `IncidentIoNotFoundError`, 429 →
  `IncidentIoRateLimitError` (with `retry_after_seconds` int or None), 400/422 →
  `IncidentIoRequestRejectedError`, other ≥400 or malformed JSON → `IncidentIoResponseError`,
  network failure or the 60 s overall deadline → `IncidentIoTransportError`. No automatic retries.
  - AC: a fake client that sleeps past a shortened deadline raises `IncidentIoTransportError`.
  - AC: client tests for each status, including `Retry-After: 30` → `retry_after_seconds == 30`.
- MUST validate inputs locally before any request, by argument kind:
  - `limit`: an int (not bool) in 1–100.
  - Enum filters (`status_category`, `mode`, alert/action/follow-up `status`, `sort`): a value or
    non-empty list of values from the documented sets.
  - Timestamps: ISO-8601 date or datetime strings parsed with `datetime.fromisoformat`; naive
    values are treated as UTC. `list_incidents` date filters accept `YYYY-MM-DD` only (API limit)
    and are inclusive.
  - Resource IDs (`get_alert`, `get_postmortem`, `incident`/`team` filter args, catalog type ID,
    alert source ID): 1–100 chars matching `[A-Za-z0-9_-]`.
  - Name-or-ID args (`severity`, `incident_type`, `status`, `schedule`, `catalog_type`) and free
    text (`email`, `slack_user_id`, `identifier`): non-empty after trimming, at most 200 chars.
  - AC: each kind's invalid input raises `IncidentIoInputError` with no request made.
  - AC: `list_incidents(severity="SEV 1")` passes validation and is resolved by name.
- MUST accept incident IDs as a ULID, `INC-<n>` or `<n>`, sending `<n>` for the reference forms.
  - AC: `get_incident("inc-42")` requests `/v2/incidents/42`.
- MUST support `list_incidents` filters: `status_category` (list of enum values), `severity`,
  `incident_type`, `status` (each a name or ID, or a list of them), `created_after`,
  `created_before`, `updated_after`, `updated_before`, `mode` (default API behaviour excludes
  test/tutorial), `sort` (`newest`/`oldest`), `limit`.
  - AC: a severity name is resolved to its ID via `/v1/severities` and sent as
    `severity[one_of]=<id>`; an unknown name raises `IncidentIoInputError` listing valid names.
  - AC: `status_category=["live"]` sends `status_category[one_of]=live`.
  - AC: `mode` is a value or list from `standard retrospective test tutorial`; when omitted, no
    `mode` param is sent (the API then excludes test and tutorial incidents).
- MUST follow `after` cursors until `limit` results or no cursor, never sending `page_size` > 100.
  - AC: a two-page mocked response with `limit=30` returns 30 items and makes 2 requests.
- MUST return normalised bounded records: no raw provider objects; text truncated at 8,000
  chars; nested lists (role assignments, custom fields, attributes) capped at 50 entries.
  - AC: formatting tests for incident summary/detail, alert, action, follow-up, post-mortem,
    schedule entry, team, user and catalog entry.
  - AC: `get_incident` detail includes `participants` (`active`/`passive` lists of users) and
    `alert_ids` (from linked incident alerts), in addition to the summary fields.
- MUST give `on_call(schedule, start=None, end=None)` a default window of now → now + 1 minute
  when both are omitted; when only `start` is given, `end` = `start` + 1 minute; `end` must be
  after `start` and the window at most 31 days.
  - AC: tests for the default window, `end` ≤ `start` → input error, and > 31 days → input error.
- MUST provide `persona/skills/incident-io/SKILL.md` documenting every public function, inputs,
  output shapes, limits, policy and errors, with no credentials, raw URLs, or `ARCHIE_HOME_DIR`.
  - AC: a skill contract test like `tests/test_slack_skill.py` checks the 18 function names, the
    error class names, `read-only`, and the absence of `api.incident.io` and `ARCHIE_HOME_DIR`.
  - AC: `discover_skills` finds a skill named `incident-io`.
- MUST work against the live account for every function (manual verification, read-only).
  - AC: a live smoke script run once in M4 calls each function with `limit<=3`, chaining IDs for
    `get_incident`, `get_alert`, `get_postmortem`, `on_call` and `list_catalog_entries` from the
    preceding list calls, and prints only function name, result type, count and keys. Pass = all
    18 calls return without raising. Its output is recorded in M4's Outcome as the evidence.

Out of scope: any write (incident create/update, timeline, escalations, follow-ups, status pages,
catalog, config); status pages, workflows, call transcripts, pay configs, policies; read scoping;
cross-provider (Datadog) composition; incremental sync.

## Design

### Overview

A new `incident_io` tool package mirrors `jira/`: one credential/transport module, a policy gate,
validation helpers, normalising formatters, and per-area modules of `@tool` functions. Every
function validates input, checks read policy, opens a short-lived client, issues one or more GETs,
and returns normalised bounded data. A skill documents the surface.

### Stack

- `httpx` (reused, already a dependency of `agent`). No SDK.

### Code structure

All new, under `agent/src/archie_agent/exec/tools/incident_io/`:

- `__init__.py` — imports and `__all__` for the 18 functions (pattern: `jira/__init__.py`).
- `client.py` — errors (base `IncidentIoError(ToolError)` and subclasses), `_credential()`,
  `IncidentIoClient.get(path, params)` and `paginate(path, key, params, limit)`, `incident_io_client()`
  async context manager with a 60 s overall timeout (pattern: `jira/client.py`, rate limit from
  `slack/client.py` `parse_retry_after`).
- `policy.py` — `require_read()` using `resolve_provider_policy(current_policy_snapshot(),
  "incident_io")` (pattern: `jira/policy.py`, without the env fallback that plan 065 flagged as dead).
- `validation.py` — `limit`, `enum_list`, `timestamp`, `identifier`, `incident_id` helpers.
- `formatting.py` — `text()`, `user()`, and per-resource normalisers.
- `reference.py` — `reference(kind)` for `severities`, `incident_types`, `incident_statuses`,
  `incident_roles`, `custom_fields`, plus internal `resolve_ids(client, kind, values)`.
- `incidents.py` — `list_incidents`, `get_incident`, `incident_timeline`, `incident_updates`.
  `get_incident` includes participants (`/v2/incident_participants`) and linked alert IDs
  (`/v2/incident_alerts`).
- `alerts.py` — `list_alerts(status, source, created_after, created_before, limit)`, `get_alert`.
- `work.py` — `list_actions`, `list_follow_ups` (filters: `incident`, `status`, `created_after`,
  `updated_after`, `team` for follow-ups), `list_postmortems(incident, limit)`,
  `get_postmortem(id)` with markdown content truncated at 8,000 chars.
- `directory.py` — `list_teams`, `list_users(email, slack_user_id, include_inactive)`,
  `list_catalog_types`, `list_catalog_entries(catalog_type, identifier, limit)`.
- `on_call.py` — `list_schedules`, `on_call(schedule, start, end)`, `list_escalation_paths`.

Edited: `agent/src/archie_agent/exec/tools/__init__.py` (import `incident_io`).

New: `persona/skills/incident-io/SKILL.md` (pattern: `persona/skills/jira/SKILL.md`).

Tests (new): `tests/test_incident_io_client.py`, `tests/test_incident_io_incidents.py`,
`tests/test_incident_io_reads.py`, `tests/test_incident_io_skill.py`.

Docs (edited): `docs/config.md` gets one sentence listing `incident_io` read policy (reads only,
scope unsupported).

### Error handling

- Configuration/policy errors and `IncidentIoInputError` (local validation): raised before any
  request; correct the input, do not retry unchanged.
- `IncidentIoRequestRejectedError` (provider 400/422): the API rejected otherwise-valid input;
  correct and retry.
- 401/403: authentication/authorization errors; surface.
- 404: not-found error; surface.
- 429: rate-limit error with `retry_after_seconds`; no automatic retry.
- Network errors and the 60 s overall deadline (`TimeoutError` from `asyncio.timeout`):
  `IncidentIoTransportError`; reads are idempotent, so the model may retry once.
- Malformed JSON or missing expected list key: `IncidentIoResponseError`.

### Non-functional

- Security: GET-only client; key only in the Authorization header; errors carry fixed messages
  plus at most the provider's `errors[].code` values (no message bodies, no request echo).
- Bounds: `limit` ≤ 100 per call; page size ≤ 100; text ≤ 8,000 chars; nested lists ≤ 50; overall
  call timeout 60 s; request timeout 30 s.

### Config / infra

- Optional policy: `tools.incident_io.read.enabled` (default true). No new credential or env var.

## Milestones

### [x] M1 — Tracer: `incident_io.list_incidents` and `get_incident` end to end

- **Approach:** create `client.py`, `policy.py`, `validation.py`, `formatting.py` (incident
  summary/detail only), `reference.py` (complete: internal `resolve_ids` and public `reference`
  for all five kinds `severities`, `incident_types`, `incident_statuses`, `incident_roles`,
  `custom_fields`) and
  `incidents.py` (`list_incidents`, `get_incident`). Register the package in
  `exec/tools/__init__.py`. Follow `jira/client.py` and `jira/search.py`.
  ⚠️ Build deepObject params as flat keys (`severity[one_of]`); httpx encodes list values as
  repeated keys — confirm against the live API.
- **Test seam:** public tool functions with `incident_io_client` patched to a fake client; client
  tests use `httpx.MockTransport`, including the GET-only assertion (the transport handler fails
  on any non-GET) and the deadline → transport error test.
- **Edge cases:** severity name unknown → `IncidentIoInputError`; `INC-7` → `/v2/incidents/7`;
  401 → authentication error without key text; 429 with `Retry-After` → `retry_after_seconds`;
  policy disabled → no request.
- **Deliverable:** model code can list and read incidents.
- **Verify:** `uv run pytest tests/test_incident_io_client.py tests/test_incident_io_incidents.py`
  passes; a one-off live call `list_incidents(limit=2)` and `get_incident(<reference>)` returns
  normalised records (report keys only).
- **Outcome:** Built `client.py`, `policy.py`, `validation.py`, `formatting.py`, `reference.py`
  (all five kinds) and `incidents.py`; registered in `exec/tools/__init__.py`. 53 tests pass.
  Live (`/tmp/incident_io_m1_live.py`, keys/counts only): `reference("severities")` 3 items;
  `list_incidents(limit=2)` 2 summaries; all-severity filter 40 vs single severity 19 (confirms
  repeated-key encoding of `[one_of]` lists); `status_category="closed"` returned only closed;
  `get_incident` by reference and by ID agree, with 7 active / 4 passive participants. The live
  run found that incident date filters reject datetimes (see Deviations). Per-endpoint page-size
  maxima found during live checks: alerts and incident alerts 50, escalation paths 25, others 100;
  `paginate` takes `max_page_size`.

### [x] M2 — Incident context: timeline, updates, alerts

- **Approach:** add `incident_timeline`, `incident_updates` to `incidents.py`; `alerts.py` with
  `list_alerts`, `get_alert`; extend formatters. Linked alerts come from `/v2/incident_alerts`.
- **Test seam:** public tool functions with a fake client.
- **Edge cases:** pagination across 2 pages; alert `status` outside `firing/resolved` → input error.
- **Deliverable:** model code can reconstruct an incident's history and linked alerts.
- **Verify:** `uv run pytest tests/test_incident_io_incidents.py tests/test_incident_io_reads.py`
  passes.
- **Outcome:** Added `incident_timeline`, `incident_updates` (both resolve `INC-<n>` to the ULID
  that the child endpoints need) and `alerts.py` (`list_alerts`, `get_alert` with attributes and
  linked incident IDs; page size 50). Added `paginate(keep=...)` for local filtering, needed by
  M3's follow-up/action status filters. 65 incident.io tests pass; ruff clean. Live
  (`/tmp/incident_io_m2_live.py`): timeline 4 items, updates 3, alerts 16 (all `firing`, matching
  a direct API count of 16 firing / 0 resolved), `get_alert` with 3 attributes.

### [x] M3 — Work items, directory, on-call and reference data

- **Approach:** `work.py`, `directory.py`, `on_call.py`. Schedule and
  catalog-type arguments accept an ID or exact name (case-insensitive), resolved by listing.
- **Test seam:** public tool functions with a fake client.
- **Edge cases:** ambiguous schedule name → input error listing matches; `on_call` without start
  and end uses now → now + 1 minute; `end` before `start` → input error.
- **Deliverable:** all 18 functions callable.
- **Verify:** `uv run pytest tests/test_incident_io_reads.py` passes, including a registration
  test for exactly the 18 `incident_io.*` keys and a runner-namespace test that runs exec code
  `await incident_io.reference("severities")` through `runner._build_namespace` with a fake client.
- **Outcome:** Added `work.py`, `directory.py`, `on_call.py` and formatters; all 18 functions
  register (registration test). Runner test drives `runner.run()` with a `context.json` policy and
  `incident_io.reference("severities")` (exercises `_build_namespace`). 89 incident.io tests pass;
  ruff clean. Live (`/tmp/incident_io_m3_live.py`, counts/keys only): actions 3; outstanding
  follow-ups 3 (all `outstanding`); post-mortems 2, content hit the 8,000-char cap with
  `content_truncated: true`; teams 3; users 3 and lookup by email 1; catalog types 29, entries
  by type name 2; schedules 3; `on_call` by name now 1 entry, by ID over a week 7; escalation
  paths 2; reference kinds 3/11/5/11.

### [x] M4 — Skill, docs, and live verification

- **Approach:** write `persona/skills/incident-io/SKILL.md` following `persona/skills/jira/SKILL.md`
  (global rules, choose-the-right-function table, inputs, output schemas, functions, errors,
  examples); add `tests/test_incident_io_skill.py` like `tests/test_slack_skill.py`; add the
  `docs/config.md` sentence. Run a live smoke script outside the repo (`/tmp`) that calls each
  function once with `limit<=3` and prints only function name, result type, count and keys.
- **Test seam:** skill contract test; `discover_skills` on `persona/skills`.
- **Deliverable:** documented, discoverable skill; live-verified surface.
- **Verify:** `uv run pytest tests/test_incident_io_skill.py` passes; the live smoke script
  reports 18/18 calls returned without raising (output pasted into Outcome).
- **Outcome:** Wrote `persona/skills/incident-io/SKILL.md` and `tests/test_incident_io_skill.py`
  (checks every registered `incident_io.*` function name, all error classes, no `api.incident.io`
  / `ARCHIE_HOME_DIR` / `Bearer` / `/v2/`, and discovery as `incident-io`); added the
  `docs/config.md` sentence. The skill test uses `_scan_directory` on the repo skills dir because
  `discover_skills()` takes no path. Live smoke `/tmp/incident_io_smoke.py` output (shapes only):

  ```
  list_incidents list 3 · get_incident dict 24 · incident_timeline list 1 · incident_updates list 1
  list_alerts list 3 · get_alert dict 13 · list_actions list 3 · list_follow_ups list 3
  list_postmortems list 3 · get_postmortem dict 11 · list_teams list 3 · list_users list 3
  list_catalog_types list 29 · list_catalog_entries list 3 · list_schedules list 3
  on_call dict 4 · list_escalation_paths list 2 · reference list 3
  18/18 calls returned without raising
  ```

## Deviations

- M3: action and follow-up `status` filters are applied locally (`paginate(keep=...)`, full pages,
  at most 20 pages) because the v3 endpoints have no status filter. A rare status may return
  fewer than `limit` results; the skill documents this.

- M1: `list_incidents` `created_*`/`updated_*` filters accept `YYYY-MM-DD` only, not datetimes.
  The live API rejects datetimes on incident filters (`invalid_value`). The tool rejects a datetime
  locally rather than silently truncating it. Other endpoints keep full datetime filters.

## Risks

- Filter encoding for multi-value deepObject params (`severity[one_of]` repeated) is assumed from
  the OpenAPI style; M1 confirms it live.
- incident.io may add or rename fields; formatters use `.get()` and tolerate missing fields.

## Review

- Plan review: 1 pass, 0 ❌, 5 ⚠️, 7 ℹ️. All 12 accepted and fixed: smoke pass bar and ID
  chaining defined; runner-namespace and GET-only ACs placed in M3/M1; docs sentence traced to the
  policy requirement; `on_call` window moved to Requirements; validation rule per argument kind;
  `mode` semantics stated; `get_incident` detail fields named; D10 narrowed to `Retry-After`; D3
  examples fixed; stray docs line removed; deadline mapped to `IncidentIoTransportError`;
  provider 400/422 renamed `IncidentIoRequestRejectedError` (D13); M1 delivers all reference
  kinds. No second pass (no ❌).

## Outcome

PR: pending
CI: pending

## Follow-ups

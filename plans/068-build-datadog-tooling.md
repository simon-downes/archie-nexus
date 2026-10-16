# 068 — Datadog tooling and skill

Status: building
Branch: build-it/068-datadog-tooling (base: main @ 89cf27a)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/068-datadog-tooling

## Objective

Give the agent Datadog access through an exec-only `datadog` namespace and a matching
`persona/skills/datadog/SKILL.md`, following the existing Jira and incident.io tool patterns.
When done, model code can query metrics, logs, spans, events, monitors, dashboards and the
software catalog for incident investigation and service health checks, and can create and
update monitors tagged `managedby:archie` when write policy allows. Dashboards are read-only.
No delete operation exists.

## Context

The user asked: "let's build the datadog tool and skill. follow the existing patterns/structures
like for the slack, notion, jira, etc tools and skills." They noted that app-key scopes can't
restrict deletions, "but that's why we expose a tool and not the raw credentials to the model".

The design scope is the brain entry `projects/archie/datadog-incident-operations-tools.md`
(Datadog section): read-heavy wrapper; bounded monitor and dashboard writes behind an ownership
boundary; no deletes; no SLOs; service checks and event posting deferred. Its open decisions
(ownership tag, naming convention, raw query language, limits) are resolved in Decisions below;
dashboard writes were dropped (D3).

Codebase facts:

- Commit a1d97e6 added the `datadog` static provider (`api_key`, `app_key`) with non-secret
  `site` defaulting to `app.datadoghq.eu` (`shared/src/archie_shared/credentials/providers.py`),
  and `DatadogCredential` (`shared/src/archie_shared/credentials/models.py`). The API host is
  derived from the site (`https://api.datadoghq.eu`; `docs/auth-credentials.md`).
- Closest patterns: `agent/src/archie_agent/exec/tools/jira/` (static credential, httpx client,
  policy with read/write gates, guarded mutations with `...MutationIndeterminateError`) and the
  unmerged incident.io build on branch `build-it/067-incident-io-tooling` (PR #5, open):
  `incident_io/client.py` (error base class, `parse_retry_after`, 60 s deadline),
  `validation.py`, `policy.py`, and its skill and skill contract test.
- Tool packages are registered by import in `agent/src/archie_agent/exec/tools/__init__.py`
  `get_all_tools()`. `tools:` config is an open provider map, so `tools.datadog` needs no schema
  change. Missing policy = read enabled, write disabled
  (`shared/src/archie_shared/tool_policy.py`).
- Exec results are truncated at 16,000 characters.

Live API facts (checked with the user's key on `api.datadoghq.eu`, status codes and shapes only):

- 200: `/api/v1/validate`, `/api/v1/monitor`, `/api/v1/monitor/search` (774 monitors),
  `/api/v1/dashboard`, `/api/v1/metrics`, `/api/v2/catalog/entity`, `/api/v2/team`,
  `/api/v1/logs/config/indexes`, `POST /api/v2/logs/events/search`,
  `POST /api/v2/spans/events/search`, `/api/v2/events`, `POST /api/v2/query/timeseries`,
  `/api/v1/query`. The v2 timeseries query works even though `timeseries_query` is not in the
  key's listed scopes.
- 403: `/api/v2/downtime` (key lacks `monitors_downtime`).
- Rate limits are per endpoint and tight on some: spans search 5/60 s, logs search 10/10 s,
  metrics list 10/30 s, dashboards 60/60 s.

## Baseline

- Check commands: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`
- Setup: `uv sync`
- Setup leftovers: none (`.venv` is ignored)
- Failing before this work: none (1329 passed; ruff clean)
- Not included: `persona/skills/aws-agent-access/`, `plans/060-spec-container-aws-profiles.md`
  (untracked in the user's checkout, unrelated)

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Write ownership boundary | Tag only. The tool adds the tag on create and refuses to update a monitor that lacks it | Tag plus `[Archie] ` name prefix; prefix only | User: "just use the tag, don't prefix things" | user Q1 |
| D2 | Ownership tag spelling | `managedby:archie` | `managed_by:archie` | 124 of the first 200 monitors already carry `managedby:pulumi`; one tag key lets `managedby:*` filters cover both | user Q2 |
| D3 | Dashboard writes | None in this build; dashboards are read-only | `team:archie` team tag; description marker | Datadog dashboard `tags` accept only `team:<name>` (OpenAPI v1 `Dashboard.tags`), no `archie` team exists, and a description marker is forgeable | user Q2 (same question as D2) |
| D4 | Monitor mute and downtime | Not built | Downtime create/cancel | `/api/v2/downtime` returns 403 (key lacks `monitors_downtime`) | self |
| D5 | Delete prevention mechanism | Client sends only allowlisted `(method, path pattern)` pairs; DELETE has no entry; anything else raises before sending | Separate read and write client methods without an allowlist | App-key scopes cannot block deletes, so the wrapper is the only boundary; one allowlist is testable in one place | self |
| D6 | Policy scope | `read.enabled` and `write.enabled` only; any non-empty `scope` raises `DatadogPolicyError` | Tag or service scopes | Logs, spans and metrics have no common ownership field to enforce a scope; failing closed avoids a false restriction (incident.io D5) | self |
| D7 | Query language | Accept Datadog query strings (metric queries, log/span/event search syntax, monitor search), max 2,000 chars | Structured filters only | Datadog reads are query-string driven and metric queries have no structured equivalent; this is a read DSL, not raw HTTP | self |
| D8 | API host | Read `site` from `effective_provider("datadog", load_nexus_config().auth.providers.get("datadog"))` inside the exec subprocess. Strip one leading `app.`; the rest must be one of `datadoghq.com`, `us3.datadoghq.com`, `us5.datadoghq.com`, `datadoghq.eu`, `ap1.datadoghq.com`, `ap2.datadoghq.com`, `ddog-gov.com`; host = `https://api.<rest>`. A config load failure raises `DatadogConfigurationError` (fail closed) | Hard-code `api.datadoghq.eu`; pass site through the runner context | The provider already makes `site` configurable; the allowlist stops keys going to an arbitrary host; the runner context carries only policy (`exec/runner.py`), and reading config in-process avoids changing it | self |
| D9 | Time inputs | ISO-8601 datetime (naive = UTC) or relative `<n>m`/`<n>h`/`<n>d` meaning "that long ago"; default `start` 1 h ago, `end` now; window at most 31 days | Epoch numbers; Datadog `now-15m` strings | One form works for both v1 (epoch seconds) and v2 (epoch ms) endpoints after local conversion | self |
| D10 | Rate-limit retry hint | `retry_after_seconds` from `X-RateLimit-Reset` (non-negative integer seconds), else None | `Retry-After` | Datadog sends `X-RateLimit-Reset` as seconds until reset (observed 2, 3, 33) | self |
| D11 | Provider error detail | First provider error string, truncated to 300 chars, in `DatadogRequestRejectedError` | Fixed message only | Messages like `Cannot parse query: 'status:(('` are needed to fix a query and carry no secrets | self |
| D12 | Metric query endpoint | `GET /api/v1/query`; an `error` field in a 200 body raises `DatadogRequestRejectedError` | `POST /api/v2/query/timeseries` | v1 takes the familiar metric query string directly; live check showed v1 reports bad queries as 200 + `error` | self |
| D13 | Monitor options | `options` limited to an allowlist (`thresholds`, `notify_no_data`, `no_data_timeframe`, `renotify_interval`, `evaluation_delay`, `new_group_delay`, `require_full_window`, `include_tags`, `notify_audit`, `escalation_message`, `timeout_h`) | Pass-through dict | Blocks fields such as `restricted_roles` that change who can see or edit a monitor | self |
| D14 | Module layout | Mirror incident.io: `client.py`, `policy.py`, `validation.py`, `formatting.py`, then `metrics.py`, `logs.py`, `traces.py`, `events.py`, `monitors.py`, `dashboards.py`, `catalog.py` | One module | Jira and incident.io split by API area with per-area tests; this keeps the write code (`monitors.py`) separate from the read modules for review | self |
| D15 | Metric search endpoint | `GET /api/v1/search?q=metrics:<query>`, results cut to `limit` locally | `GET /api/v2/metrics` (lists by tag filters, no name search) | Live check: v1 search returns `results.metrics` names for a substring query | self |
| D16 | Metric point truncation | Keep the last 300 points of each series | First 300; downsampling | During an incident the most recent points matter most; downsampling hides spikes | self |
| D17 | Base branch | Stay on `main` @ 89cf27a; copy incident.io patterns from branch `build-it/067-incident-io-tooling` | Stack on the 067 branch | 067 is an independent open PR; stacking would tie this PR's merge to it. The two conflicts are one import line and one docs sentence | self |
| D18 | `update_monitor` body | Send only the given fields; merge `options` over the current options read by the ownership GET | Send only given fields including a bare `options`; send the full merged definition | Datadog replaces `options` as a whole, so a partial `options` would silently drop `notify_no_data` etc.; sending only changed top-level fields avoids rewriting queries the caller didn't touch | self |
| D19 | Write failures after sending | 5xx, malformed body, transport error after transmission, or deadline → `DatadogMutationIndeterminateError`; 4xx → `DatadogRequestRejectedError` | Surface the raw typed error | A 5xx or bad body after POST/PUT may hide a change; Jira `_mutate_issue` treats these as indeterminate | self |

## Requirements

- MUST expose exactly these async functions under the `datadog` exec namespace with
  `@tool(native=False, exec_docs=False, namespace="datadog")`: `query_metrics`, `search_metrics`,
  `search_logs`, `search_spans`, `list_events`, `search_monitors`, `get_monitor`,
  `validate_monitor`, `create_monitor`, `update_monitor`, `list_dashboards`, `get_dashboard`,
  `list_catalog_entities`, `list_teams`.
  - AC: `get_all_tools()` has `datadog.<name>` for each of the 14, all `_native is False` and
    `_exec_docs is False`, and no other `datadog.*` keys.
  - AC: a runner test runs exec code `await datadog.search_monitors("status:alert")` through
    `runner.run()` with a fake client.
- MUST NOT send any request outside the client allowlist; no DELETE is possible.
  - AC: `DatadogClient.request("DELETE", "/api/v1/monitor/1")` raises `DatadogPolicyError` and
    the mock transport sees no request; same for `PUT /api/v1/dashboard/x`.
  - AC: a test drives every public function against a recording transport and asserts each
    `(method, path)` matches the allowlist and no method is DELETE.
- MUST load keys only via `get_credential("datadog", DatadogCredential)` and send them only as
  `DD-API-KEY` / `DD-APPLICATION-KEY` headers to the derived host; keys never appear in
  arguments, results, errors or the skill.
  - AC: missing/empty keys, or a site outside the allowlist, raise `DatadogConfigurationError`
    before any request.
  - AC: `app.datadoghq.eu` and `datadoghq.eu` → `https://api.datadoghq.eu`; `us5.datadoghq.com` →
    `https://api.us5.datadoghq.com`; `evil.example` and `app.evil.example` → configuration
    error; a `load_nexus_config` failure → configuration error.
  - AC: the error raised for a 401 does not contain either key.
- MUST gate reads on `tools.datadog.read.enabled` (default true) and writes on
  `tools.datadog.write.enabled` (default false), raising `DatadogPolicyError` before any request;
  a non-empty scope in either block raises `DatadogPolicyError`.
  - AC: tests for read disabled, write default-disabled, write enabled, and scope set.
  - AC: `docs/config.md` names the `tools.datadog` policy in one sentence.
- MUST map failures to typed errors (all subclasses of `DatadogError(ToolError)`): 401
  `DatadogAuthenticationError`, 403 `DatadogAuthorizationError`, 404 `DatadogNotFoundError`, 429
  `DatadogRateLimitError` with `retry_after_seconds`, 400/422 and v1 query `error` bodies
  `DatadogRequestRejectedError`, other ≥400 or malformed JSON `DatadogResponseError`, network
  failure or the 60 s deadline `DatadogTransportError`. No automatic retries.
  - AC: client tests for each status; `X-RateLimit-Reset: 7` → `retry_after_seconds == 7`; a 400
    body `{"errors": ["invalid_argument(Cannot parse query: 'x')"]}` appears (≤300 chars) in the
    message; a slow fake past a shortened deadline → `DatadogTransportError`.
- MUST validate inputs locally before any request, raising `DatadogInputError`: `limit` int (not
  bool) in 1–100 for every function; required query strings (`query_metrics`, `search_metrics`,
  `search_logs`, `search_spans`, monitor `query`) non-empty and ≤2,000 chars; optional query
  arguments (`list_events`, `search_monitors`, `list_dashboards`, `list_catalog_entities`,
  `list_teams`) are `None` or ≤2,000 chars, and `None`/empty means the filter is not sent;
  catalog `kind` matches `[a-z0-9_.-]{1,64}`; times per D9 with
  `end` after `start` and window ≤31 days; monitor IDs positive ints; dashboard IDs matching
  `[a-z0-9]{3}-[a-z0-9]{3}-[a-z0-9]{3}`; `sort` in `newest`/`oldest`; monitor `type` in the
  OpenAPI v1 `MonitorType` enum (`composite`, `event alert`, `log alert`, `metric alert`,
  `process alert`, `query alert`, `rum alert`, `service check`, `synthetics alert`,
  `trace-analytics alert`, `slo alert`, `event-v2 alert`, `audit alert`, `ci-pipelines alert`,
  `ci-tests alert`, `error-tracking alert`, `database-monitoring alert`,
  `network-performance alert`, `cost alert`, `data-quality alert`, `network-path alert`,
  `data-jobs alert`, `llm-observability alert`); `priority` 1–5; `tags` ≤50 strings of ≤200 chars; `options` keys per D13.
  - AC: one test per input kind showing the error and no request.
- MUST return normalised bounded records, never raw payloads: text fields ≤4,000 chars (log
  messages ≤2,000), tag lists ≤50, metric series ≤20 with the last ≤300 points each, dashboard widgets
  ≤100; truncation is flagged with `truncated: true`.
  - AC: formatting tests for metric series, log, span, event, monitor summary/detail, dashboard
    summary/detail, catalog entity and team.
- MUST, for `create_monitor`, add `managedby:archie` to the tags and return the created monitor;
  for `update_monitor`, GET the monitor first and raise `DatadogOwnershipError` with no PUT when
  its tags lack `managedby:archie`, and keep the tag on the PUT even if the caller's `tags` omit it.
  - AC: create payload tags contain `managedby:archie`; update of an untagged monitor makes one
    GET and no PUT; update with `tags=["team:x"]` sends `["team:x", "managedby:archie"]`.
- MUST raise `DatadogMutationIndeterminateError` when a create/update request may have been
  transmitted but its outcome is unknown (Jira `_mutate_issue` pattern).
  - AC: a 500 or malformed body on POST → indeterminate error; a 400 → rejected error.
  - AC: `update_monitor(id, options={"thresholds": {...}})` sends the current options with
    `thresholds` replaced; `update_monitor(id)` → input error with no request.
  - AC: a transport `ReadError` on PUT → indeterminate error; a `ConnectError` → transport error;
    the overall deadline expiring during the POST or PUT → indeterminate error.
- MUST give `validate_monitor` the same inputs as `create_monitor` (including adding the ownership tag), call
  `POST /api/v1/monitor/validate`, require only read policy, and return `{"valid": true}` or raise
  `DatadogRequestRejectedError`.
  - AC: unit test with a mocked 400; live call with `type="metric alert"`,
    `query="avg(last_5m):avg:system.cpu.user{*} > 90"`, `name="x"`, `message="x"` returns
    `{"valid": true}`.
- MUST provide `persona/skills/datadog/SKILL.md` documenting every function, inputs, outputs,
  limits, rate limits that matter (spans 5/60 s, logs 10/10 s), policy, ownership rule and errors,
  with no credentials, `api.datadoghq`, `DD-API-KEY` or `ARCHIE_HOME_DIR`.
  - AC: a skill contract test like the incident.io one checks all 14 function names and every
    error class, and the absence of those strings; `_scan_directory` finds a skill named `datadog`.
- MUST work against the live account for every read function and `validate_monitor`, and refuse
  a live `update_monitor` on an untagged monitor before any write.
  - AC: a smoke script in `/tmp` prints only function name, result type, count and keys; pass =
    12/12 read-side calls return (`get_monitor` and `get_dashboard` use the first ID from
    `search_monitors(limit=1)` and `list_dashboards(limit=1)`; time windows are the last 15
    minutes), and `update_monitor(<first ID from search_monitors("tag:managedby:pulumi", limit=1)>, name="x")` raises
    `DatadogOwnershipError` (write policy enabled only inside the script's snapshot).

Out of scope: dashboard writes, deletes of any kind, downtimes and muting, SLOs, service checks,
event posting, notebooks, incident.io composition, read/write scopes.

## Design

### Overview

A new `datadog` exec tool package mirrors the incident.io package: one client that holds
credentials, the derived host and a request allowlist; a policy gate; local validation;
normalising formatters; and per-area modules of `@tool` functions. Reads query metrics, logs,
spans, events, monitors, dashboards and the catalog. The only writes are monitor create and
update, guarded by write policy and the `managedby:archie` tag.

### Stack

- `httpx` (reused). No Datadog SDK: it would add a dependency and expose every endpoint,
  including deletes, which the allowlist exists to prevent.

### Code structure

All new under `agent/src/archie_agent/exec/tools/datadog/`:

- `__init__.py`: imports and `__all__` for the 14 functions (pattern: `jira/__init__.py`).
- `client.py`: `DatadogError` and subclasses, including `DatadogOwnershipError` and
  `DatadogMutationIndeterminateError`; `_credential()`; `api_host(site)`; the `_ALLOWED`
  `(method, compiled path regex)` table; `DatadogClient.request(method, path, params=, json=)`;
  `datadog_client()` async context manager with a 60 s deadline (pattern: incident.io
  `client.py` on branch `build-it/067-incident-io-tooling`; Jira `client.py` for the
  `transmitted` flag on transport errors).
- `policy.py`: `require_read()`, `require_write()` via `resolve_provider_policy(None, "datadog")`.
- `validation.py`: `limit`, `query`, `time_window`, `monitor_id`, `dashboard_id`, `tags`,
  `monitor_options`, `choice`.
- `formatting.py`: `text()` and per-resource normalisers.
- `paging.py`: v2 cursor helpers (`search_pages` for POST search bodies, `list_pages` for GET
  lists; both follow `meta.page.after`, at most 5 pages).
- `metrics.py`: `query_metrics(query, start="1h", end=None)`, `search_metrics(query, limit=50)`.
- `logs.py`: `search_logs(query, start="1h", end=None, limit=20, sort="newest")`. Record:
  `id`, `timestamp`, `service`, `host`, `status`, `message` (≤2,000), `tags` (≤50), `attributes`
  (top-level keys only, ≤30 keys, scalar values ≤200 chars, nested values replaced by `"…"`).
- `traces.py`: `search_spans(query, start="1h", end=None, limit=20, sort="newest")`, one request.
  Record: `trace_id`, `span_id`, `parent_id`, `service`, `resource`, `operation`, `env`, `start`,
  `end`, `duration_ms`, `status`, `error` (from `custom.error.message`/`type`, ≤500); `custom` is
  not returned.
- `events.py`: `list_events(query=None, start="1h", end=None, limit=20)`. Record: `id`,
  `timestamp`, `title`, `message` (≤2,000), `source`, `tags` (≤50).
- `monitors.py`: `search_monitors(query=None, limit=20)`, `get_monitor(monitor_id)`,
  `validate_monitor(...)`, `create_monitor(name, type, query, message, tags=None, priority=None,
  options=None)`, `update_monitor(monitor_id, name=None, query=None, message=None, tags=None,
  priority=None, options=None)`. `update_monitor` sends a PUT with only the given fields, and
  `options` is merged over the monitor's current options from the ownership GET (D18). It
  returns the monitor from a follow-up GET. With no fields given, it raises `DatadogInputError`.
- `client.py` `_ALLOWED` (method, path): `GET /api/v1/query`, `GET /api/v1/search`,
  `POST /api/v2/logs/events/search`, `POST /api/v2/spans/events/search`, `GET /api/v2/events`,
  `GET /api/v1/monitor/search`, `GET /api/v1/monitor/<int>`, `POST /api/v1/monitor/validate`,
  `POST /api/v1/monitor`, `PUT /api/v1/monitor/<int>`, `GET /api/v1/dashboard`,
  `GET /api/v1/dashboard/<id>`, `GET /api/v2/catalog/entity`, `GET /api/v2/team`.
- `dashboards.py`: `list_dashboards(query=None, limit=50)` (local case-insensitive title
  filter), `get_dashboard(dashboard_id)` (top-level widgets only, ≤100; each `id`, `type`,
  `title`, `queries` (≤10), and for groups `child_count`). Monitor record: `id`, `name`, `type`,
  `status`, `query`, `message` (detail only), `tags`, `priority`, `managed`, `created`, `modified`.
- `catalog.py`: `list_catalog_entities(kind=None, query=None, limit=50)` sends
  `filter[kind]` and `filter[name]`; `list_teams(query=None, limit=50)` sends `filter[keyword]`.
  Entity record: `id`, `kind`, `name`, `display_name`, `owner`, `tags`; team: `id`, `handle`,
  `name`, `member_count`.

Edited: `agent/src/archie_agent/exec/tools/__init__.py` (import `datadog`), `docs/config.md`.
New: `persona/skills/datadog/SKILL.md`; tests `tests/datadog_fakes.py`,
`tests/test_datadog_client.py`, `tests/test_datadog_reads.py`, `tests/test_datadog_monitors.py`,
`tests/test_datadog_skill.py`.

### Error handling

- Configuration, policy and input errors: raised before any request. Ownership errors: raised
  after the ownership GET and before any write. Fix the cause; do not retry unchanged.
- `DatadogRequestRejectedError`: the provider rejected the query or definition; fix and retry.
- 401/403/404: surface.
- 429: `DatadogRateLimitError` with `retry_after_seconds`; no automatic retry.
- Transport/deadline on a read: `DatadogTransportError`; reads may be retried once.
- Transport after transmission, or the overall deadline expiring, on create/update:
  `DatadogMutationIndeterminateError` (the write wraps the client context and maps
  `DatadogTransportError` raised by the deadline). Verify with `get_monitor` or
  `search_monitors("tag:managedby:archie")` before retrying.

### Non-functional

- Security: request allowlist (D5); site allowlist (D8); keys only in headers; ownership tag on
  every write; write policy default off.
- Bounds: per-call limits ≤100 records; text and list caps above; request timeout 30 s; overall 60 s.

### Config / infra

- Optional policy `tools.datadog.read.enabled` (default true), `tools.datadog.write.enabled`
  (default false). Existing optional `auth.providers.datadog.site`. No new secrets.

## Milestones

### [x] M1 — Tracer: `datadog.query_metrics` end to end

- **Approach:** build `client.py` (allowlist with the v1 query and metric search entries first),
  `policy.py`, `validation.py`, `formatting.py` (metric series), `metrics.py`; register in
  `exec/tools/__init__.py`. ⚠️ Confirm that `effective_provider` and `load_nexus_config` work
  inside the exec subprocess; a config load failure is a configuration error.
- **Test seam:** public functions with `datadog_client` patched to a fake; client tests with
  `httpx.MockTransport`.
- **Edge cases:** v1 200 + `error` → rejected error; `start` after `end` → input error; DELETE →
  policy error with no request; unknown site → configuration error.
- **Deliverable:** model code can query metric timeseries.
- **Verify:** `uv run pytest tests/test_datadog_client.py tests/test_datadog_reads.py -k "client or metric"`
  passes; a one-off live `query_metrics("avg:system.cpu.user{*}", start="1h")` returns series
  (keys only).
- **Outcome:** Built `client.py` (full 14-entry allowlist, site → host, typed errors),
  `policy.py`, `validation.py`, `formatting.py` (metric series), `metrics.py`; registered in
  `exec/tools/__init__.py`; `tests/datadog_fakes.py` is a pytest plugin in `tests/conftest.py`
  (incident.io pattern). `uv run pytest tests/test_datadog_client.py tests/test_datadog_reads.py`:
  62 passed; ruff clean. Live, through `runner.run()` with an empty `context.json`:
  `query_metrics("avg:system.cpu.user{*}", start="1h")` 1 series / 180 points;
  `search_metrics("system.cpu", limit=3)` 3 names. This confirms that credentials and
  `load_nexus_config` site lookup work inside the exec subprocess. The policy fixture resets by
  value, because async tests run in a copied context and token resets fail.

### [x] M2 — Logs, spans and events

- **Approach:** `logs.py`, `traces.py`, `events.py`, their formatters and allowlist entries. Log
  search follows `meta.page.after` until `limit`; spans make one request (5/60 s limit).
- **Test seam:** public functions with a fake client.
- **Edge cases:** 400 with a parse error → message carried; 429 → retry hint.
- **Deliverable:** model code can search logs, spans and events.
- **Verify:** `uv run pytest tests/test_datadog_reads.py` passes.
- **Outcome:** Added `logs.py`, `traces.py`, `events.py`, `paging.py` and log/span/event
  formatters. Span `duration_ms` comes from `custom.duration` (ns); the event title and source come
  from the nested `attributes.attributes` (live shape). `uv run pytest tests/test_datadog_reads.py
  tests/test_datadog_client.py`: 75 passed; ruff clean. Live via `runner.run()`: 3 error logs (largest
  record 3,814 chars), 2 error spans with duration and error text, 3 events with a source, and
  `search_logs("status:((")` → `DatadogRequestRejectedError` carrying "Cannot parse query".

### [x] M3 — Monitor, dashboard and catalog reads

- **Approach:** `search_monitors`, `get_monitor`, `validate_monitor` in `monitors.py`;
  `dashboards.py`; `catalog.py`. Monitor records include `managed: bool` (has the ownership tag).
- **Test seam:** public functions with a fake client; registration test; runner test.
- **Deliverable:** all read functions callable.
- **Verify:** `uv run pytest tests/test_datadog_reads.py tests/test_datadog_monitors.py -k "not write"` passes;
  the registration test asserts the 12 read functions here and is tightened to exactly 14 in M4.
- **Outcome:** Added `monitors.py` (`search_monitors`, `get_monitor`, `validate_monitor`, plus the
  shared `definition()` builder for M4), `dashboards.py`, `catalog.py` and formatters. Monitor
  `status` comes from `overall_state` (GET) or `status` (search). Detail `options` show only the
  D13 allowlisted keys. `uv run pytest tests/test_datadog_reads.py tests/test_datadog_monitors.py
  tests/test_datadog_client.py`: 106 passed, including the 12-read registration test and the
  runner test `test_runner_exposes_datadog_namespace`; ruff clean. Live via `runner.run()`:
  2 alerting monitors; `get_monitor` detail with 3 options and `managed: False`;
  `validate_monitor` valid → `{"valid": True}`, invalid query → `DatadogRequestRejectedError`;
  2 dashboards, one with 38 widgets and 44 queries (10,360 chars); 2 catalog entities; 2 teams.

### [x] M4 — Guarded monitor writes

- **Approach:** `create_monitor`, `update_monitor` with `require_write`, the ownership GET
  before PUT, tag injection, and the indeterminate-error pattern from `jira/issues.py`
  `_mutate_issue`. Add the recording-transport test that covers every public function.
- **Test seam:** public functions with a fake client / recording transport.
- **Edge cases:** untagged monitor → `DatadogOwnershipError`, no PUT; write disabled → no
  request; `options` with `restricted_roles` → input error; a 5xx or malformed body after the
  POST/PUT was sent → `DatadogMutationIndeterminateError`; a 4xx → `DatadogRequestRejectedError`
  (Datadog rejected it, nothing changed).
- **Deliverable:** model code can create and update Archie-owned monitors only.
- **Verify:** `uv run pytest tests/test_datadog_monitors.py tests/test_datadog_client.py` passes.
- **Outcome:** Added `create_monitor` and `update_monitor`. A `_Write` helper records whether the
  write was handed to the transport, so the overall deadline expiring *after* the POST/PUT is
  indeterminate, while expiring during the ownership GET stays a plain transport error. Update
  sends only the given fields, merges `options` over the current ones, and returns the monitor
  from a follow-up GET; a failing follow-up GET is indeterminate. The registration test asserts
  exactly 14 functions. The allowlist sweep drives all 14 through the real `DatadogClient` with a
  recording transport and checks every `(method, path)` against `_ALLOWED` (methods seen: GET,
  POST, PUT; no DELETE). `uv run pytest tests/test_datadog_monitors.py tests/test_datadog_client.py
  tests/test_datadog_reads.py`: 124 passed; ruff clean. No live create/update was run, because it
  is externally visible; the live ownership refusal runs in M5.

### [ ] M5 — Skill, docs and live smoke

- **Approach:** write `persona/skills/datadog/SKILL.md` following
  `persona/skills/jira/SKILL.md` and the incident.io skill; add `tests/test_datadog_skill.py`;
  add the `docs/config.md` sentence; run the `/tmp` smoke script.
- **Test seam:** skill contract test; `_scan_directory`.
- **Deliverable:** documented, discoverable skill; live-verified read surface.
- **Verify:** `uv run pytest tests/test_datadog_skill.py` passes; the smoke script prints 12/12
  read-side calls returned and the ownership refusal.
- **Outcome:**

## Deviations

## Risks

- `get_dashboard` on a large dashboard can exceed the 16,000-character exec result limit
  (live: 38 widgets → 10,360 chars). The skill tells the agent to select fields in code before
  returning them.

- Merging after (or before) PR #5 conflicts in `exec/tools/__init__.py` (one import) and
  `docs/config.md` (one sentence); resolve by keeping both.
- An invalid `config.yaml` makes every Datadog call fail with a configuration error while other
  tools keep working on the last valid policy; intended (fail closed on the API host).
- The key may lack `monitors_write`; live create/update cannot be verified and are covered by unit tests only.
- `update_monitor` checks ownership with a GET and then sends a PUT; a concurrent tag removal in between is not detected.
- Query strings are passed to Datadog unchanged, so a broad log query can return sensitive log
  content. Output caps limit the volume; the skill tells the agent to keep queries narrow.

## Review

- Plan review: 1 pass, no ❌ left after fixes. Fixed: site allowlist named (D8), monitor type set
  listed, limit ranges 1–100, smoke calls named, last-300-points rule (D16), metric search
  endpoint (D15), D3 source label, D14 reason, catalog `kind` rule, base-branch conflict (D17,
  Risks), config-load failure behaviour, deadline during writes → indeterminate error, ownership
  error timing wording, optional query arguments.
- Plan review pass 2: no ❌. Fixed: uniform `start="1h", end=None` signatures and
  `list_events(query=None)`; update body rule (D18); write-failure mapping (D19); decision order;
  record fields incl. bounded log `attributes` and no span `custom`; catalog/team filter params;
  explicit `_ALLOWED` list; `validate_monitor` adds the tag; update return value and empty-call
  rule; top-level widget cap; registration test staging (M3 12 → M4 14); smoke inputs named.

## Outcome

PR: pending
CI: pending

## Follow-ups

- Dashboard writes once an ownership marker exists (for example a Datadog team `archie`).
- Monitor downtimes and muting (needs `monitors_downtime`).
- Combined Datadog and incident.io workflows.

# 096 — Datadog log configuration reads and projection fixes

Status: done
Branch: build-it/096-datadog-log-config (base: (detached) @ ec4b472)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/096-datadog-log-config

## Objective

The Datadog tool can search logs but can't show how they are processed, so agents guess at
parsing, masking, retention and log-based metrics, and nested attributes come back as `"…"`.
When done, the agent can read log pipelines, indexes, log-based metrics and Sensitive Data
Scanner config, read named nested attributes from logs and spans, aggregate spans, and see
every custom dashboard (the API omits preset ones) and the widgets inside dashboard groups.

## Context

The user asked which Datadog capabilities would be useful, "focusing on utility". A review of
76 past sessions that used the tool found ~27 with gaps. Ranked by sessions: nested
attributes hidden (~12), log pipelines/parsing unreadable (9), indexes/retention (4), Sensitive
Data Scanner (4), log-based metrics (3), plus span aggregation, dashboard group widgets and
enumeration (2), and rejected query syntax (~7). The user approved: "sounds good use build-it
skill to implement. make sure to read latest datadog api docs rather than relying on your
built-in knowledge".

Codebase facts (plugins/datadog/):
- `client.py` sends only `(method, path)` pairs in `_ALLOWED`; tests' `FakeClient` checks the
  same allowlist. Every tool validates input (`validation.py`) before `require_read()` and any
  request, and returns bounded, normalised records (`formatting.py`) with a `truncated` flag.
- `_scalar_attributes` replaces nested attribute values with `"…"` (logs); `formatting.span`
  drops custom span attributes except `error` and `duration`.
- `list_dashboards` and `discovery._dashboards` call `GET /api/v1/dashboard` with no params;
  the API's `count` defaults to 100, so only the first 100 dashboards are ever seen.
- `formatting.widget` lists only top-level widgets; group widgets get `child_count`.
- `tests/test_plugin_layout.py` requires each tool to appear as `` `datadog.<name>(`` in the
  skill; `test_datadog_skill.py` forbids `/api/v1`, `/api/v2` and key names in the skill.

Datadog API facts, from the current OpenAPI spec
(DataDog/datadog-api-client-python@a70f733, `.generator/schemas/v{1,2}/openapi.yaml`) and
docs.datadoghq.com (read 2026-11-19):
- `GET /api/v1/logs/config/pipelines` (array of LogsPipeline: id, name, type, is_enabled,
  is_read_only, filter.query, processors[], description, tags),
  `GET /api/v1/logs/config/pipelines/{pipeline_id}`, `GET /api/v1/logs/config/pipeline-order`
  (`pipeline_ids`). Permission `logs_read_config`. 22 processor types, each a dict with `type`,
  `name`, `is_enabled` and type-specific fields; `pipeline` processors nest `processors`.
- `GET /api/v1/logs/config/indexes` (`indexes[]`: name, filter.query, num_retention_days,
  num_flex_logs_retention_days, daily_limit, daily_limit_reset, daily_limit_warning_threshold_
  percentage, exclusion_filters[{name,is_enabled,filter{query,sample_rate,sample_attribute}}],
  is_rate_limited, tags) and `GET /api/v1/logs/config/index-order` (`index_names`).
  Permission `logs_read_config`.
- `GET /api/v2/logs/config/metrics` (data[]: id, attributes.compute{aggregation_type, path,
  include_percentiles}, filter.query, group_by[{path, tag_name}]). Permission
  `logs_read_config`.
- `GET /api/v2/sensitive-data-scanner/config`: `data.relationships.groups.data` gives group
  order; `included[]` holds `sensitive_data_scanner_group` (name, description, filter.query,
  is_enabled, product_list, samplings, relationships.rules.data) and
  `sensitive_data_scanner_rule` (name, pattern, namespaces, excluded_namespaces,
  text_replacement, included_keyword_configuration, is_enabled, priority, tags,
  relationships.standard_pattern). `GET .../config/standard-patterns` names standard patterns.
  Permission `data_scanner_read`.
- `POST /api/v2/spans/analytics/aggregate`: body `data.type=aggregate_request`,
  `data.attributes{filter,compute[],group_by[]}`; buckets in `data[].attributes{by, compute}`
  (`computes` is "not returned"); `meta.status` done|timeout, `meta.warnings`. No paging.
  50 requests/minute; at most 10,000 groups. Aggregations: count, cardinality, pc75–pc99, sum,
  min, max, avg, median.
- `GET /api/v1/dashboard`: `count` (default 100) and `start` offset.
- No public API lists log facets.
- Log search: special characters `- ! && || > >= < <= ( ) { } [ ] " * ? : \ #` and spaces need
  `\` escaping or a quoted value; `CIDR(@attr, block)` supports IPv4 and IPv6. Pipeline,
  index and log-metric filters can't use full-text search; a pipeline's filter runs before its
  processors. Monitor search uses UI facet values (`type:metric`, `type:log`), not API types.

## Baseline

- Check commands: `scripts/dev test`, `scripts/dev ruff check .`, `scripts/dev ruff format .`
- Setup: `scripts/dev sync`
- Setup leftovers: `.venv-archie` (gitignored)
- Failing before this work: 26 tests (list in /tmp/baseline_failures.txt):
  tests/test_question_tool.py (10), tests/test_ws_integration.py (13),
  tests/test_host_config.py (2), tests/test_local_api.py (1). Cause: no Kiro API key in the
  container and the integration harness. `scripts/dev test plugins` passes (829).
- Not included: none (built from origin/main; the user's checkout is on an unmerged branch)

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Scope | Pipelines, indexes, log-based metrics, Sensitive Data Scanner, `fields` projection on log/span search, `aggregate_spans`, dashboard paging and group children, skill query-syntax notes | Also facets, archives, custom destinations, APM retention filters, span-based metrics, notification rules | User approved the evidence-ranked list; facets have no public API; the rest had 0–1 sessions of demand | user (approval) |
| D2 | Pipeline function shape | `list_log_pipelines(query)` summaries in processing order + `get_log_pipeline(id)` full detail | One call returning every pipeline with all processors | Integration pipelines carry long grok rules; the list/get split matches `list_dashboards`/`get_dashboard` and keeps the common "which pipelines touch source:X" call small | self |
| D3 | Processor output | Generic bounded projection of each processor dict as Datadog returns it | Per-processor-type field mapping | 22 processor types and Datadog adds more; a generic projection keeps grok rules, remappers, lookup tables and new types without code changes | self |
| D4 | Order | List functions fetch the order endpoint and return `position` (1-based) in processing order | Return API order without position | Pipeline and index order decides which pipeline wins and which index a log lands in; agents need it to reason about parsing | self |
| D5 | Sensitive Data Scanner shape | `list_scanning_groups()` returns groups in scan order with their rules nested, standard pattern names resolved | Separate group and rule functions | One question ("what masks this attribute?") needs both; config is small; standard patterns are needed to name rules that have no `pattern` | self |
| D6 | Nested attribute access | `fields=[...]` argument on `search_logs`/`search_spans` returning `{path: bounded value}` keyed by the path as passed (trimmed); a cut value sets the record's `truncated`. Logs resolve against `item.attributes.attributes` only (reserved fields are already in the record) | Raise nesting depth for every attribute; return whole attribute tree | Explicit paths keep default output small and let the agent read a whole object (`@context`) when it asks for it | self |
| D7 | Span path resolution | Resolve against `item.attributes.custom`, then `item.attributes.attributes` (the nested object), first hit wins | Only `custom` | The spec documents both objects; `custom` is where `formatting.span` already finds `error` and `duration` | self |
| D13 | Path syntax | A path matches the existing `validation._FACET` regex; a leading `@` is dropped for lookup; at each dict level the longest key formed from the remaining segments is tried first (`http.url_details` as one key before `http` → `url_details`) | Plain segment walk | Datadog attribute keys can contain dots; longest-first finds both shapes | self |
| D14 | Log aggregations | Add `avg` and `median` to logs too (the spec's `LogsAggregationFunction` has them) | Spans only | Same shared builder and validation; a one-line set change | self |
| D8 | Aggregation code | Shared request builder for logs and spans aggregates; spans make one request (no paging exists) | Copy `aggregate_logs` body building | Both endpoints take the same compute/group_by shape; one builder avoids drift | self |
| D9 | Dashboard listing | Page `GET /api/v1/dashboard` with `count=100`/`start`, at most 20 pages, stopping as soon as `limit` matches are collected or a page has fewer than 100; shared by `list_dashboards` and `search`; `list_dashboards` limit max 1,000 | Keep a single request; 50 pages | The API defaults `count` to 100, so dashboards past the first 100 are invisible to both functions today; 20 sequential pages fit inside `search`'s 25 s per-source timeout and the 60 s call deadline at normal latency | self |
| D10 | Group widgets | Top-level cap stays 100 (`MAX_WIDGETS`); group records keep `child_count` and gain `widgets` (children, same shape); children are capped at 100 in total, filled in dashboard order; `widget_count` stays top-level | Flatten children into the top-level list; one shared 200 cap | Nesting keeps the group context and the existing top-level contract and tests stay valid | self |
| D11 | Where code lives | New `plugins/datadog/log_config.py` for the four config reads; normalisers in `formatting.py`; `aggregate_spans` in `traces.py` | One module per Datadog product | The four reads answer one question (how logs are processed) and share patterns | self |
| D12 | Permissions | No new handling: a missing `logs_read_config`/`data_scanner_read` permission surfaces as the existing `DatadogAuthorizationError`; the skill names the permission per function | Probe permissions up front | Existing error mapping already includes Datadog's message; probing costs a request per call | self |

## Requirements

- MUST add `GET` entries only to the client allowlist: `/api/v1/logs/config/pipelines`,
  `/api/v1/logs/config/pipelines/{id}` (id `[A-Za-z0-9_-]{1,64}`),
  `/api/v1/logs/config/pipeline-order`, `/api/v1/logs/config/indexes`,
  `/api/v1/logs/config/index-order`, `/api/v2/logs/config/metrics`,
  `/api/v2/sensitive-data-scanner/config`, `/api/v2/sensitive-data-scanner/config/standard-patterns`.
  - AC: `test_allowlist_has_no_delete` passes; PUT/POST/PATCH/DELETE on these paths raise
    `DatadogPolicyError` without sending (client test).
- MUST provide `datadog.list_log_pipelines(query=None)` returning pipelines in processing
  order with `position`, `id`, `name`, `enabled`, `read_only`, `filter`, `description`, `tags`,
  `processor_count` and a `processors` summary (`type`, `name`, `enabled`; nested pipelines
  with their own summary), filtered case-insensitively on name or filter query.
  - AC: fake order `[b, a]` returns `b` at position 1; `query="apache"` keeps a pipeline whose
    filter is `source:apache` and drops others; pipelines missing from the order come last with
    `position: null`.
- MUST provide `datadog.get_log_pipeline(pipeline_id)` returning the pipeline with every
  processor's configuration (grok `match_rules`/`support_rules`, samples, sources, targets,
  categories, nested pipelines), strings capped at 10,000 characters, lists at 100 items,
  nesting at depth 8, and `truncated` set when anything was cut.
  - AC: a grok processor's `grok.match_rules` round-trips; a 12,000-character string comes back
    as 10,000 with `truncated: true`; an invalid id (`a/b`) raises `DatadogInputError` with no
    request; HTTP 404 raises `DatadogNotFoundError`.
- MUST provide `datadog.list_log_indexes()` returning indexes in index order with `position`,
  `name`, `filter`, `retention_days`, `flex_retention_days`, `daily_limit`,
  `daily_limit_reset`, `daily_limit_warning_threshold_percentage`, `is_rate_limited`, `tags`
  and `exclusion_filters` (`name`, `enabled`, `query`, `sample_rate`, `sample_attribute`).
  - AC: fake responses map to those fields in index order; an index missing from
    `index_names` comes last with `position: null`.
- MUST provide `datadog.list_log_metrics(query=None)` returning log-based metrics with `id`,
  `aggregation`, `path`, `include_percentiles`, `filter` and `group_by` (`path`, `tag_name`),
  filtered case-insensitively on id or filter query.
  - AC: fake response normalised; `query` filters as stated.
- MUST provide `datadog.list_scanning_groups()` returning scanner groups in scan order with
  `position`, `id`, `name`, `description`, `enabled`, `filter`, `products`, `samplings` and
  `rules` (`id`, `name`, `description`, `enabled`, `pattern`, `standard_pattern`,
  `namespaces`, `excluded_namespaces`, `text_replacement`, `keywords`, `priority`, `tags`).
  It fetches standard patterns only when a rule references one.
  - AC: groups come back in `relationships.groups` order with their rules; a rule linked to a
    standard pattern shows that pattern's name; with no standard-pattern links only one request
    is made.
- MUST accept `fields` (list of 1–20 attribute paths matching `validation._FACET`, `@`
  optional) on `search_logs` and `search_spans`, adding `fields: {path: value}` to each record,
  values bounded (strings 2,000, lists 50, dict keys 50, depth 4); a missing path maps to
  `null`; a cut value sets `truncated`. Without `fields`, output is unchanged.
  - AC: `fields=["@http.client_ip", "@context"]` returns the nested IP and the bounded
    `context` object; a key that itself contains dots (`"http.url_details"`) resolves; an
    invalid path raises `DatadogInputError` before any request; spans resolve from `custom`.
- MUST provide `datadog.aggregate_spans(...)` with the same arguments and return shape as
  `aggregate_logs`, in one request; both accept `avg` and `median` as well as the current
  aggregations.
  - AC: request body has `data.type: aggregate_request` and the same compute/group_by as logs;
    buckets parse from `attributes.compute`; `meta.status: timeout` sets `partial`.
- MUST page the dashboard list (`count=100`, `start` offset, at most 20 requests, stopping
  once `limit` matches are found) for `list_dashboards` and the `dashboards` source of
  `search`; `list_dashboards` accepts `limit` up to 1,000.
  - AC: with fakes returning 100 then 30 dashboards, a title match in the second page is
    found by both functions, and the calls carry `start=0` then `start=100`; when the first
    page already holds `limit` matches only one request is made.
- MUST list widgets inside group widgets in `get_dashboard`, as `widgets` on the group record
  (children capped at 100 in total), keeping `child_count` and the top-level cap of 100, with
  `truncated` when any child is left out.
  - AC: a group with two children returns both children with their queries; the existing
    103-widget test still returns 100 top-level widgets.
- MUST document every new function and argument in the skill (with the permission each
  needs), and add query-syntax guidance: escaping/quoting special characters and IPv6 values,
  `CIDR()`, monitor search `type:` values, and how to read pipeline order and precedence.
  The README function list and module list include the new functions and `log_config.py`.
  - AC: `tests/test_plugin_layout.py` passes; a new test in `test_datadog_skill.py` asserts the
    skill contains `logs_read_config`, `data_scanner_read`, `CIDR(` and `type:metric`, and the
    README names every registered `datadog.*` function.

Out of scope: log facets (no API), archives, custom destinations, restriction queries, APM
retention filters, span-based metrics, notification rules, any write to log configuration,
adding `uk1.datadoghq.com` / `us2.ddog-gov.com` to `SITES` (Follow-ups).

## Design

### Overview

Read-only additions to the existing `datadog` exec namespace, following the existing
validate → `require_read()` → allowlisted request → bounded normaliser pattern. Log
configuration lives in a new module; projection and dashboard fixes change existing modules.

### Code structure

- `plugins/datadog/client.py`: new allowlist entries and a `PIPELINE_ID` pattern.
- `plugins/datadog/log_config.py` (new): `list_log_pipelines`, `get_log_pipeline`,
  `list_log_indexes`, `list_log_metrics`, `list_scanning_groups`. Follow `dashboards.py`.
- `plugins/datadog/formatting.py`: `bounded_value`/`Bounds` (generic bounded projection used for
  processors and `fields`), pipeline/index/log-metric/scanner normalisers, `field_values`,
  group children in `widget`/`dashboard_detail`, `span_bucket`.
- `plugins/datadog/validation.py`: `pipeline_id`, `fields`, span aggregation set.
- `plugins/datadog/paging.py`: `aggregate_attributes(...)` shared by logs and spans.
- `plugins/datadog/logs.py`, `traces.py`: `fields` argument; `aggregate_spans` in `traces.py`.
- `plugins/datadog/dashboards.py`: `dashboard_pages(client)` used by `list_dashboards` and
  `discovery._dashboards`.
- `plugins/datadog/__init__.py`, `README.md`, `skills/datadog/SKILL.md`.
- Tests: new `plugins/datadog/tests/test_datadog_log_config.py`; extend
  `test_datadog_reads.py`, `test_datadog_discovery.py`, `test_datadog_client.py`.

### Error handling

- Missing permission (403) → existing `DatadogAuthorizationError` with Datadog's detail;
  the skill says which permission each function needs.
- Malformed payload (not the documented container type) → `DatadogResponseError`; unknown
  extra keys are ignored or passed through the bounded projection.
- Standard-pattern lookup failure propagates (same permission as the config read).

### Non-functional

- Bounds: config strings 10,000 chars, lists 100, dict keys 200, depth 8; `fields` values
  strings 2,000, lists 50, dict keys 50, depth 4; summaries cap processors at 100; widgets
  100 top-level plus 100 children.
- Requests per call: pipelines 2 (list) or 1 (get); indexes 2; log metrics 1; scanner 1–2;
  spans aggregate 1 (Datadog limit 50/min); dashboards 1 per 100 dashboards, at most 20.

## Milestones

M2–M4 each group closely related reads that share one pattern and one test seam; splitting
them further would repeat the same skill/README/allowlist edits without reducing risk.

### [x] M1 — Read log pipelines

- **Approach:** add allowlist entries (all eight paths, so later milestones only add code),
  `validation.pipeline_id`, `formatting.bounded_value` and pipeline normalisers, and
  `log_config.list_log_pipelines`/`get_log_pipeline`. Export from `__init__.py`; document in
  the skill and README. ⚠️ The skill test forbids `/api/v1` text in the skill. ⚠️
  `test_datadog_monitors.py` `PUBLIC` and `test_every_public_function_stays_inside_the_allowlist`
  must list each new function (and `log_config` in its patched modules) in the milestone that
  adds it; `test_datadog_reads.py` `READS` likewise.
- **Test seam:** the tool functions with `dd_fake(..., "log_config")`; `client.allowed` for
  the allowlist.
- **Edge cases:** pipeline missing from order → last, `position: null`; non-list response →
  `DatadogResponseError`; nested `pipeline` processor → summarised and fully projected.
- **Deliverable:** the agent can list pipelines in order and read one pipeline's grok rules.
- **Verify:** `scripts/dev test plugins/datadog tests/test_plugin_layout.py -q` passes.
- **Outcome:** `log_config.py` with `list_log_pipelines`/`get_log_pipeline`; all eight GET
  paths allowlisted; `formatting.bounded_value` + `Bounds`/`CONFIG_BOUNDS`; skill gained "How
  logs are processed" (order, filter-before-processors, grok first-match, precedence,
  integration pipelines, preprocessing; from the pipelines, grok parser and parsing docs) and
  both function entries. 347 passed (`scripts/dev test plugins/datadog tests/test_plugin_layout.py`);
  ruff clean.

### [x] M2 — Read indexes, log-based metrics and scanner groups

- **Approach:** `list_log_indexes`, `list_log_metrics`, `list_scanning_groups` in
  `log_config.py` with normalisers in `formatting.py`; skill and README entries.
- **Test seam:** tool functions with `dd_fake`.
- **Edge cases:** scanner response with no standard-pattern links → one request; group listed
  in order but absent from `included` → skipped; rule whose group is not listed → ignored.
- **Deliverable:** the agent can read retention/exclusion filters, log-metric definitions and
  masking rules.
- **Verify:** `scripts/dev test plugins/datadog tests/test_plugin_layout.py -q` passes.
- **Outcome:** `list_log_indexes`, `list_log_metrics`, `list_scanning_groups` in `log_config.py`;
  `in_order` moved to `formatting.py` (shared by pipelines, indexes and scanner groups, and
  tolerant of unhashable order entries). Skill: index routing/exclusion/quota semantics from
  the indexes doc, scanner rule semantics from the spec. 360 passed; ruff clean.

### [x] M3 — Nested fields on log and span search, and span aggregation

- **Approach:** `validation.fields`; `formatting.field_values(source_dicts, paths)` with the
  dotted-key resolution; `fields` argument on `search_logs`/`search_spans`; move the
  compute/group_by body building from `aggregate_logs` into `paging.aggregate_attributes`;
  `aggregate_spans` in `traces.py`; `avg`/`median` in the shared aggregation set. Skill: replace
  the `search_logs` note that nested values show as `"…"` and must be queried instead, document
  `fields` on both searches, add `aggregate_spans` to "Rate limits that matter" (50/min) and to
  "Choose the right function".
- **Test seam:** `search_logs`, `search_spans`, `aggregate_logs`, `aggregate_spans` with
  `dd_fake`.
- **Edge cases:** path absent → `null`; value is a list of objects → bounded; `aggregate_logs`
  request body unchanged by the refactor (existing tests).
- **Deliverable:** agents read nested attributes and aggregate spans server-side.
- **Verify:** `scripts/dev test plugins/datadog tests/test_plugin_layout.py -q` passes.
- **Outcome:** `validation.fields`, `validation.aggregate` (an `Aggregate` dataclass shared by
  both aggregates), `paging.aggregate_attributes`/`aggregate_result`/`meta_warnings`,
  `formatting.field_values` (longest-dotted-key lookup, `FIELD_BOUNDS`) and `span_bucket`;
  `fields` on both searches; `aggregate_spans` in `traces.py`; `avg`/`median` for both.
  Existing `aggregate_logs` tests pass unchanged. Skill: `fields` docs replace the "query it
  instead" note, `aggregate_spans` entry, rate-limit and table rows. 375 passed; ruff clean.

### [x] M4 — Every dashboard, group widgets, and query-syntax guidance

- **Approach:** `dashboards.dashboard_pages`; use it in `list_dashboards` and
  `discovery._dashboards`; children in `formatting.dashboard_detail`; 100 top-level widgets
  plus 100 children (D10).
  Skill: query-syntax section and pipeline-reading guidance; replace "widgets inside groups are
  not listed"; say the list holds custom dashboards only; update "Choose the right function".
  Add the skill/README content test.
- **Test seam:** `list_dashboards`, `get_dashboard`, `search` with `dd_fake`.
- **Edge cases:** exactly 100 dashboards then an empty page → two requests; 20-page cap.
- **Deliverable:** dashboards beyond 100 are visible and grouped widgets show their queries.
- **Verify:** `scripts/dev test plugins/datadog tests/test_plugin_layout.py -q` passes, then
  the full checks show no new failures against the baseline list.
- **Outcome:** `dashboards.dashboard_pages` (async generator, closed with `aclosing`) used by
  `list_dashboards` (limit 1–1,000) and `discovery._dashboards`; group children in
  `dashboard_detail` with a 100-child budget. Skill: "Query syntax" section (log/span escaping,
  IPv6 quoting, `CIDR()`, reserved attributes, monitor `type:` names, from the log search
  syntax and monitor search docs), dashboard notes; new skill/README content tests.
  388 passed; ruff clean.

## Deviations

- Review (risk ⚠️): `dashboard_pages` now skips IDs it has already yielded, and with a
  `budget_seconds` it ends quietly after the first page when a later page fails or the budget
  is spent. `search`'s dashboards source uses a 20 s budget, so it keeps matches already found
  instead of landing in `skipped`. `list_dashboards` keeps raising on a failed page, because a
  plain list can't say it is incomplete. Adds behaviour to D9; no scope change.

## Risks

- Not verifiable against live Datadog from this container: the running agent uses
  `/opt/archie/plugins`, and the skill forbids calling Datadog outside `datadog.*`. Shapes come
  from the OpenAPI spec. The app key may lack `logs_read_config` or `data_scanner_read`.
- Whether integration (read-only) pipelines return their processors through the API is not
  stated in the docs; the code handles an empty `processors` list either way.
- JSON preprocessing and Auto-Processing are not exposed by the pipeline API; the skill says so.

## Review

- Code review: `general` and `risk` (risk ran: external input and a public tool interface),
  one round, no ❌. Fixed: risk ⚠️ dashboard paging losing earlier matches (see Deviations),
  risk ℹ️ dedupe by id; general ℹ️ duplicate filter helper (`_query_of` removed, pipelines
  filter on the normalised record), stale shared `limit` rule in the skill, discovery docstring
  and README wraps, three tests renamed to "propagates" (403/404 mapping is covered by
  `test_datadog_client.py`), stale M4 Approach text. Rejected in part: risk's suggestion that
  `list_dashboards` also return partial results on a later-page failure — its return value
  can't flag incompleteness, so raising is the honest outcome.
- Merge risk: Medium: the new functions parse response shapes taken from the OpenAPI spec
  and were not run against live Datadog, so a shape mismatch would surface as
  `DatadogResponseError` or missing fields on those reads; every new request is a GET on the
  allowlist and existing function outputs only gain fields.
- Plan review: 1 pass. Fixed: skill/README AC (new content test), monitors `PUBLIC`/allowlist tests named in M1, M2–M4 grouping explained, index order fallback, objective (custom dashboards only), log/span path resolution (D6, D7, D13), `avg`/`median` for logs (D14), `fields` key and `truncated`, README module list, dashboard paging cap 20 with early stop (D9), dict key caps, widget caps (D10), named skill edits. Rejected: none. No ❌ findings, so no second pass.

## Outcome

Delivered eight read functions or arguments: `list_log_pipelines`, `get_log_pipeline`,
`list_log_indexes`, `list_log_metrics`, `list_scanning_groups`, `aggregate_spans`, `fields` on
`search_logs`/`search_spans`, plus `avg`/`median` aggregations, dashboard paging (with group
widgets in `get_dashboard`) and skill guidance on pipeline processing and query syntax.

Verification: `scripts/dev test plugins/datadog tests/test_plugin_layout.py` 392 passed;
`scripts/dev ruff check .` clean; `ruff format --check` clean for the change (one pre-existing
diff in `persona/skills/python/assets/output.py`). Full `scripts/dev test`: 17 failed + 9
errors, the same 26 tests as the baseline, none new.

Not verified: behaviour against live Datadog (no way to run the worktree plugin with real
credentials from this session), and whether the app key has `logs_read_config` and
`data_scanner_read`.

PR: https://github.com/simon-downes/archie-nexus/pull/45
CI: none (no checks configured on the repository)

## Follow-ups

- Add `uk1.datadoghq.com` and `us2.ddog-gov.com` to `client.SITES` (both listed in the API docs).

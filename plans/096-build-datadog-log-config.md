# 096 — Datadog log configuration reads and projection fixes

Status: planning
Branch: build-it/096-datadog-log-config (base: (detached) @ ec4b472)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/096-datadog-log-config

## Objective

The Datadog tool can search logs but can't show how they are processed, so agents guess at
parsing, masking, retention and log-based metrics, and nested attributes come back as `"…"`.
When done, the agent can read log pipelines, indexes, log-based metrics and Sensitive Data
Scanner config, read named nested attributes from logs and spans, aggregate spans, and see
every dashboard and the widgets inside dashboard groups.

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
| D6 | Nested attribute access | `fields=[...]` argument on `search_logs`/`search_spans` returning `{path: bounded value}` | Raise nesting depth for every attribute; return whole attribute tree | Explicit paths keep default output small and let the agent read a whole object (`@context`) when it asks for it | self |
| D7 | Span path resolution | Resolve a path against span `custom`, then span `attributes` | Only `custom` | The spec documents both objects; `custom` is where `formatting.span` already finds `error` and `duration` | self |
| D8 | Aggregation code | Shared request builder for logs and spans aggregates; spans make one request (no paging exists) | Copy `aggregate_logs` body building | Both endpoints take the same compute/group_by shape; one builder avoids drift | self |
| D9 | Dashboard listing | Page `GET /api/v1/dashboard` with `count=100`/`start` up to 50 pages, shared by `list_dashboards` and `search`; `list_dashboards` limit max 1,000 | Keep a single request | The API defaults `count` to 100, so dashboards past the first 100 are invisible to both functions today | self |
| D10 | Group widgets | Group widget records carry `widgets` (children, same shape); at most 200 widgets listed across both levels | Flatten children into the top-level list | Nesting keeps the group context; the cap keeps large dashboards bounded | self |
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
  - AC: fake responses map to those fields in index order.
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
- MUST accept `fields` (list of 1–20 attribute paths, `@` optional) on `search_logs` and
  `search_spans`, adding `fields: {path: value}` to each record, values bounded (strings 2,000,
  lists 50, depth 4); a missing path maps to `null`. Without `fields`, output is unchanged.
  - AC: `fields=["@http.client_ip", "@context"]` returns the nested IP and the bounded
    `context` object; a key that itself contains dots (`"http.url_details"`) resolves; an
    invalid path raises `DatadogInputError` before any request; spans resolve from `custom`.
- MUST provide `datadog.aggregate_spans(...)` with the same arguments and return shape as
  `aggregate_logs`, aggregations including `avg` and `median`, in one request.
  - AC: request body has `data.type: aggregate_request` and the same compute/group_by as logs;
    buckets parse from `attributes.compute`; `meta.status: timeout` sets `partial`.
- MUST page the dashboard list (`count=100`, `start` offset, at most 50 requests) for
  `list_dashboards` and the `dashboards` source of `search`; `list_dashboards` accepts
  `limit` up to 1,000.
  - AC: with fakes returning 100 then 30 dashboards, a title match in the second page is
    found by both functions, and the calls carry `start=0` then `start=100`.
- MUST list widgets inside group widgets in `get_dashboard`, as `widgets` on the group record,
  with at most 200 widgets across both levels and `truncated` when any are left out.
  - AC: a group with two children returns both children with their queries.
- MUST document every new function and argument in the skill (with the permission each
  needs), and add query-syntax guidance: escaping/quoting special characters and IPv6 values,
  `CIDR()`, monitor search `type:` values, and how to read pipeline order and precedence.
  The README function list includes the new functions.
  - AC: `tests/test_plugin_layout.py` and `plugins/datadog/tests/test_datadog_skill.py` pass.

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
- `plugins/datadog/formatting.py`: `config_value` (generic bounded projection used for
  processors and `fields`), pipeline/index/log-metric/scanner normalisers, `field_values`,
  group children in `widget`/`dashboard_detail`, `span_bucket`.
- `plugins/datadog/validation.py`: `pipeline_id`, `fields`, span aggregation set.
- `plugins/datadog/paging.py`: `aggregate_request(...)` shared by logs and spans.
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

- Bounds: config strings 10,000 chars, lists 100, depth 8; `fields` values strings 2,000,
  lists 50, depth 4; summaries cap processors at 100; widgets 200.
- Requests per call: pipelines 2 (list) or 1 (get); indexes 2; log metrics 1; scanner 1–2;
  spans aggregate 1 (Datadog limit 50/min); dashboards 1 per 100 dashboards, at most 50.

## Milestones

### [ ] M1 — Read log pipelines

- **Approach:** add allowlist entries (all eight paths, so later milestones only add code),
  `validation.pipeline_id`, `formatting.config_value` and pipeline normalisers, and
  `log_config.list_log_pipelines`/`get_log_pipeline`. Export from `__init__.py`; document in
  the skill and README. ⚠️ The skill test forbids `/api/v1` text in the skill.
- **Test seam:** the tool functions with `dd_fake(..., "log_config")`; `client.allowed` for
  the allowlist.
- **Edge cases:** pipeline missing from order → last, `position: null`; non-list response →
  `DatadogResponseError`; nested `pipeline` processor → summarised and fully projected.
- **Deliverable:** the agent can list pipelines in order and read one pipeline's grok rules.
- **Verify:** `scripts/dev test plugins/datadog tests/test_plugin_layout.py -q` passes.
- **Outcome:**

### [ ] M2 — Read indexes, log-based metrics and scanner groups

- **Approach:** `list_log_indexes`, `list_log_metrics`, `list_scanning_groups` in
  `log_config.py` with normalisers in `formatting.py`; skill and README entries.
- **Test seam:** tool functions with `dd_fake`.
- **Edge cases:** scanner response with no standard-pattern links → one request; group listed
  in order but absent from `included` → skipped; rule whose group is not listed → ignored.
- **Deliverable:** the agent can read retention/exclusion filters, log-metric definitions and
  masking rules.
- **Verify:** `scripts/dev test plugins/datadog tests/test_plugin_layout.py -q` passes.
- **Outcome:**

### [ ] M3 — Nested fields on log and span search, and span aggregation

- **Approach:** `validation.fields`; `formatting.field_values(source_dicts, paths)` with the
  dotted-key resolution; `fields` argument on `search_logs`/`search_spans`; move the
  compute/group_by body building from `aggregate_logs` into `paging.aggregate_request`;
  `aggregate_spans` in `traces.py`. Skill entries.
- **Test seam:** `search_logs`, `search_spans`, `aggregate_logs`, `aggregate_spans` with
  `dd_fake`.
- **Edge cases:** path absent → `null`; value is a list of objects → bounded; `aggregate_logs`
  request body unchanged by the refactor (existing tests).
- **Deliverable:** agents read nested attributes and aggregate spans server-side.
- **Verify:** `scripts/dev test plugins/datadog tests/test_plugin_layout.py -q` passes.
- **Outcome:**

### [ ] M4 — Every dashboard, group widgets, and query-syntax guidance

- **Approach:** `dashboards.dashboard_pages`; use it in `list_dashboards` and
  `discovery._dashboards`; children in `formatting.widget`; widget cap 200 across levels.
  Skill: query-syntax section and pipeline-reading guidance; update "Choose the right function".
- **Test seam:** `list_dashboards`, `get_dashboard`, `search` with `dd_fake`.
- **Edge cases:** exactly 100 dashboards then an empty page → two requests; 50-page cap.
- **Deliverable:** dashboards beyond 100 are visible and grouped widgets show their queries.
- **Verify:** `scripts/dev test plugins/datadog tests/test_plugin_layout.py -q` passes, then
  the full checks show no new failures against the baseline list.
- **Outcome:**

## Deviations

## Risks

- Not verifiable against live Datadog from this container: the running agent uses
  `/opt/archie/plugins`, and the skill forbids calling Datadog outside `datadog.*`. Shapes come
  from the OpenAPI spec. The app key may lack `logs_read_config` or `data_scanner_read`.
- Whether integration (read-only) pipelines return their processors through the API is not
  stated in the docs; the code handles an empty `processors` list either way.
- JSON preprocessing and Auto-Processing are not exposed by the pipeline API; the skill says so.

## Review

## Outcome

PR: pending
CI: pending

## Follow-ups

- Add `uk1.datadoghq.com` and `us2.ddog-gov.com` to `client.SITES` (both listed in the API docs).

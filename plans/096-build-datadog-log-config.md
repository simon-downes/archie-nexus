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

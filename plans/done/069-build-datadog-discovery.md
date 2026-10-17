# 069 — Datadog discovery: search(), describe_metric(), search_metrics filters

Status: done
Branch: build-it/069-datadog-discovery (base: main @ fc5545d)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/069-datadog-discovery

## Objective

When the agent starts with only a term, it can't explore Datadog well. Metric search matches
names only, so `rabbitmq` finds nothing when the metrics are named `aws.amazonmq.*`. There is
also no way to see a metric's tag keys and values. This build adds a broad `datadog.search`
across sources, `datadog.describe_metric` for one metric, and `tag`/`active_within` filters on
`datadog.search_metrics`. A search for `rabbitmq` then leads to `aws.amazonmq.*` metrics
and the tags that filter them.

## Context

- The user asked for "search(), describe_metric(), tag and active_within to search_metrics()"
  after a session where `search_metrics` gave no route from `rabbitmq` to
  `aws.amazonmq.message_count`. The workaround was reading a dashboard's queries.
- The design was discussed in conversation. `search` is a top-level discovery call like
  `google.search` (`agent/src/archie_agent/exec/tools/discovery.py`). It returns references with
  a handoff to the next call. `describe_metric` stays separate from `search_metrics` because
  each describe costs three requests and most results are near-duplicate variants.
- Tool package: `agent/src/archie_agent/exec/tools/datadog/`. Every request goes through
  `client.py`'s `_ALLOWED` list (no DELETE). Validation lives in `validation.py`, normalisers in
  `formatting.py`, policy gates in `policy.py`. Tests use `tests/datadog_fakes.py` (`dd_fake`).
  The FakeClient asserts every call is inside the real allowlist.
- Live probes against the configured account (EU site, current key):
  - `GET /api/v1/metrics?from=<epoch>&tag_filter=<tags>` lists active metric names. `queue:*`
    gives 17, `from=0` gives 3,791 (148 KB), and a bogus tag gives 0.
  - `GET /api/v1/metrics/{m}` gives metadata (type, unit, description); unknown → 404.
  - `GET /api/v2/metrics/{m}/all-tags` gives flat `key:value` tags (111 for
    `aws.amazonmq.message_count`); unknown → 404.
  - `GET /api/v2/metrics/{m}/assets` gives the dashboards and monitors (id, title) that use it.
    Unknown → 200 with empty lists.
  - `GET /api/v1/monitor/search?query=rabbitmq` matches 10 monitors. Their queries name
    `aws.amazonmq.*` metrics.
  - `POST /api/v2/logs/analytics/aggregate` grouped by `service` gives service counts, no bodies.
  - `POST /api/v2/spans/analytics/aggregate` works but returned 429 after about six calls.
    `traces.py` already notes span search allows 5 requests per minute.
  - Hosts, host tags, SLOs, synthetics and notebooks return 403 for this key.

## Baseline

- Check commands: `uv run pytest -q`; `uv run ruff check .`; `uv run ruff format --check .`
- Setup: `uv sync`
- Setup leftovers: none
- Failing before this work: none (1574 passed)
- Not included: `persona/skills/aws-agent-access/`, `plans/060-spec-container-aws-profiles.md`
  (untracked in the user's checkout, unrelated)

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Merge describe into search_metrics | Separate `describe_metric` | `describe=True` flag; always describe | 3 requests per metric; agreed in conversation | user (discussion) |
| D2 | Where `search` lives | New `datadog/discovery.py`, function `datadog.search` | Add to `metrics.py` | It spans five sources; mirrors `google.search` in its own module | self |
| D3 | `search` sources | `monitors`, `dashboards`, `metrics`, `logs` by default; `spans` opt-in | All on; spans default on | Span analytics hit 429 after ~6 calls; 5/min limit noted in `traces.py` | self |
| D4 | Partial failure | Return results plus `skipped: {source: reason}`; raise only if every selected source fails | Fail the whole call (google.search) | One rate-limited source shouldn't hide the other four | self |
| D5 | `search` limit | Per source, default 10, max 50 | One total limit | Fan-out results are per source; a total limit lets one source crowd out others | self |
| D6 | Monitor → metric pivot | Extract metric names from matched metric/query-alert monitor queries and return them as metric results | Don't pivot | This is what links `rabbitmq` to `aws.amazonmq.*` | self |
| D7 | Logs/spans in `search` | Aggregate by `service`, return service references with counts | Return log/span records | No customer data in discovery output; records are one handoff away | self |
| D8 | Span query from a bare word | `service:*<w>* OR resource_name:*<w>*` for a single bare word; other queries as-is | Pass through always | Bare-word span search returned nothing live; resource match worked | self |
| D9 | Metric tag match in `search` | Only when the query looks like a tag (`key:value`, no whitespace): `GET /api/v1/metrics` (replacing name search; see Deviations) with `tag_filter=<query>`, `from=0`, names sorted and cut to `limit` | Always; never | Tag filters need a key; `*rabbitmq*` returned nothing live | self |
| D10 | `search_metrics` with tag/active_within | `GET /api/v1/metrics` with `from` + `tag_filter`, then case-insensitive name substring filter | `v2/metrics` with cursor paging | One request, no paging; v1 accepts both filters | self |
| D11 | `active_within` unset but `tag` set | `from=0` (any metric Datadog still lists) | Default 1d | Doesn't silently drop quiet metrics; `active_within` is the explicit way to require recency | self |
| D12 | `active_within` format | Relative age only (`15m`, `2h`, `30d`), max 365d | ISO datetimes too | `validation._RELATIVE` already parses this form; `/api/v1/metrics` only takes a `from` epoch, so ISO adds nothing | self |
| D13 | Tag values in `describe_metric` | Grouped by key; `max_values` per key (default 20, 1–100); each key reports its total count | Flat list | 111+ tags per metric; 357 over 30 days | self |
| D14 | Metric name validation | `^[A-Za-z][A-Za-z0-9_.]{0,199}$`, used in the allowlist path patterns too | Free text | Datadog's metric-name charset; no `/` means no path traversal | self |
| D15 | Variant grouping (`.maximum` etc.) | Not built; Follow-up | Build now | Not in the user's list for this build | self |
| D16 | Dashboard matching in `search` | `GET /api/v1/dashboard`, case-insensitive substring on title or description | Title only (as `list_dashboards`); fetch each dashboard's widgets | One request; description matched 1 of 84 for `queue` live; widget fetch is 84 requests | self |
| D17 | Which sources use `start` | Logs and spans only; monitors, dashboards and metric names ignore it | Apply to all | Only the aggregate endpoints take a time window | self |
| D18 | Client per source | Each source opens its own `datadog_client()` (own 60s deadline) inside a 25s per-source timeout | One shared client | A shared client's 60s deadline would fail the whole call, not skip the slow source | self |

## Requirements

**R1 `describe_metric(name, max_values=20)`**
- MUST return `name`, `type`, `unit`, `per_unit`, `description`, `integration`, `tags`
  (`{key: {"values": [...], "value_count": n}}`), `tag_count`, `dashboards` and `monitors`
  (each `[{id, title}]`), and `truncated`.
  - AC: with fakes for the three endpoints, the result has those keys, tags grouped by key, and
    the dashboards and monitors from `assets`.
- MUST cap values per key at `max_values` and keys at 100, and dashboards and monitors at 50
  each, setting `truncated` when anything is cut.
  - AC: 30 values for one key with `max_values=5` → 5 values, `value_count` 30, `truncated` true.
- MUST reject an invalid name or `max_values` with `DatadogInputError` before any request.
  - AC: `"a/b"`, `"1abc"`, `""`, and `max_values=0` each raise and make no call.
- MUST raise `DatadogNotFoundError` for an unknown metric. If metadata succeeds but `all-tags`
  returns 404, it MUST return empty `tags` instead of raising.
  - AC: a metadata 404 propagates as `DatadogNotFoundError`. An `all-tags` 404 gives `tags == {}`.

**R2 `search_metrics(query=None, limit=50, tag=None, active_within=None)`**
- MUST keep the current behaviour when only `query` is given (`/api/v1/search`, `[]` on no match).
  - AC: existing `search_metrics` tests pass unchanged.
- MUST, when `tag` or `active_within` is set, list names via `GET /api/v1/metrics` with
  `tag_filter=tag` and `from=now - active_within` (or `0`). It then keeps names containing
  `query` (case-insensitive), sorts them and cuts to `limit`.
  - AC: the fake sees `tag_filter` and `from` as specified; the names are filtered and limited.
- In the filtered path a blank or `None` `query` MUST mean no name filter. `tag_filter` is
  sent only when `tag` is set.
  - AC: `search_metrics("", tag="queue:*")` returns every listed name, cut to `limit`.
    `search_metrics(active_within="1h")` sends `from` and no `tag_filter`.
- With neither `tag` nor `active_within`, a blank or `None` `query` MUST raise
  `DatadogInputError`, as today.
- MUST raise `DatadogInputError` if none of the three is given, or if `tag` is blank, has
  whitespace or exceeds 500 characters, or if `active_within` isn't a relative age up to 365d.
  - AC: each case raises with no call.

**R3 `search(query, sources=None, start="1h", limit=10)`**
- MUST return `{"results": [...], "skipped": {...}}`. Every result has exactly these keys:

  | type | `id` | `title` | `source` | `matched_on` | `count` | `handoff` |
  |---|---|---|---|---|---|---|
  | `monitor` | monitor id (int) | monitor name | `monitors` | `"monitor search"` | `None` | `datadog.get_monitor` |
  | `dashboard` | dashboard id | title | `dashboards` | `"title"` or `"description"` | `None` | `datadog.get_dashboard` |
  | `metric` | metric name | metric name | `metrics` or `monitors` | `"name"`, `"tag"`, or `"monitor <id>"` | `None` | `datadog.describe_metric` |
  | `service` | service name | service name | `logs` or `spans` | `"log content"` or `"span attributes"` | events in window | `datadog.search_logs` / `datadog.search_spans` |

  - AC: a fake-backed search over all default sources returns each type with exactly these
    values.
- MUST search the sources in D3, each limited to `limit`, with `spans` only when requested.
  Metric results can number up to 2×`limit`: up to `limit` from name/tag search plus up to
  `limit` extracted from monitors.
  - AC: default sources make no spans call; `sources=["spans"]` makes only the spans call.
- MUST add metric results extracted from matched `query alert`/`metric alert` monitor queries,
  with `matched_on` naming the monitor, and dedupe metrics by name (name search wins).
  - AC: a monitor `max(last_5m):max:aws.amazonmq.x{broker:dev} > 1` yields metric
    `aws.amazonmq.x`. The same name from the name search appears once. A `log alert` yields none.
- MUST, for a single bare word (`^[A-Za-z0-9_.-]+$`), send spans the query
  `service:*<w>* OR resource_name:*<w>*` (D8). Other queries pass through unchanged. Logs
  always get the query as given.
  - AC: `search("rabbitmq", sources=["spans"])` sends that rewritten filter query.
- The metrics source MUST use the existing name search (`GET /api/v1/search`,
  `q=metrics:<query>`). For a tag-shaped query it MUST use the list call (D9) instead, because
  name search rejects `:` (see Deviations). The list is cut to `limit`. A dashboard matching both title and description gets
  `matched_on: "title"`.
- MUST make the tag-filtered metric list call (D9) only for a tag-shaped query.
  - AC: `search("queue:foo", sources=["metrics"])` makes only the `/api/v1/metrics` call with
    `tag_filter=queue:foo`; `search("rabbitmq", sources=["metrics"])` makes only the name
    search.
- MUST put a failed source in `skipped` as `"<ErrorClass>: <message>"` and return the other
  results. This covers any `Exception` from a source, including unexpected response shapes. A
  source timeout is converted to `DatadogTransportError("Datadog source exceeded 25s")`. If
  every selected source fails, it MUST raise the first source's error in selection order:
  the converted `DatadogTransportError` for a timeout, or `DatadogResponseError` wrapping a
  non-Datadog exception.
  - AC: spans raising `DatadogRateLimitError` → `skipped["spans"]` set, other results present.
    All sources failing → raises the first source's error.
- MUST re-raise `DatadogPolicyError`, `DatadogConfigurationError` and
  `DatadogAuthenticationError` from any source, because they mean every source would fail.
  - AC: a source raising `DatadogAuthenticationError` makes `search` raise it.
- MUST validate `query` (required), `sources` (non-empty unique subset), `start` (as
  `time_window`) and `limit` (1–50) before any request.
  - AC: each invalid input raises `DatadogInputError` with no call.
- MUST bound each source with its own 25s timeout and its own client, so a slow source is
  skipped rather than failing the call (D18).
  - AC: a source raising `TimeoutError` is reported in `skipped`.

**R4 Interface and safety**
- MUST add only these allowlist entries, all GET or read-only POST: `GET /api/v1/metrics`,
  `GET /api/v1/metrics/{metric}`, `GET /api/v2/metrics/{metric}/all-tags`,
  `GET /api/v2/metrics/{metric}/assets`, `POST /api/v2/logs/analytics/aggregate`,
  `POST /api/v2/spans/analytics/aggregate`.
  - AC: the allowlist test still has no DELETE. `PUT /api/v1/metrics/x` and
    `GET /api/v1/metrics/a/../b` are refused.
- MUST register `search` and `describe_metric` as `native=False, exec_docs=False`
  in namespace `datadog` (16 functions).
  - AC: registration and allowlist-drive tests cover all 16.
- MUST document both new functions, the new `search_metrics` arguments, and a discovery playbook
  in `persona/skills/datadog/SKILL.md`, with no raw API paths.
  - AC: `tests/test_datadog_skill.py` passes with 16 functions.

Out of scope: variant grouping, catalog/team sources in `search`, host/SLO/synthetics discovery
(403 for this key), dashboard widget-content search, a standalone `top_values`.

## Design

### Overview

There are three read functions. `describe_metric` drills into one metric. `search_metrics` gets
cheap tag and recency filters. `search` fans out to monitors, dashboards, metric names
(plus tags when the query is a tag), log services and optionally span services, at the same
time. It returns references that hand off to the existing functions.

### Code structure

- `client.py`: `METRIC = r"[A-Za-z][A-Za-z0-9_.]{0,199}"` and the six allowlist entries (R4).
- `validation.py`: `metric_name`, `tag_filter`, `age` (relative duration → timedelta, ≤365d),
  `sources`.
- `metrics.py`: extend `search_metrics`; add `describe_metric`.
- `formatting.py`: `metric_description(meta, tags, assets, max_values)`.
- `discovery.py` (new): `search` and one private coroutine per source. It follows
  `tools/discovery.py` for the reference shape and `monitors.py` for the request pattern.
- `__init__.py`: export both. Tests: `test_datadog_reads.py` (describe_metric, search_metrics,
  READS set); `test_datadog_monitors.py` (PUBLIC set, `calls` dict, the patched module tuple
  gains `discovery`, `bodies` gains every new path); `test_datadog_client.py` (allowlist);
  `test_datadog_skill.py` (function count). New `tests/test_datadog_discovery.py` for `search`.
- The function count rises one per milestone that adds a function: 15 after M1, 16 after M3.

### Error handling

- `describe_metric`: errors propagate (404 → `DatadogNotFoundError`).
- `describe_metric`: an `all-tags` 404 after successful metadata → empty `tags`.
- `search`: each source runs in `asyncio.timeout(25)` and opens its own `datadog_client()`
  (D18). Sources run under `asyncio.gather(..., return_exceptions=True)`, so every source
  finishes or fails before classification and nothing is left running. Each `TimeoutError`
  becomes `DatadogTransportError`, and other non-Datadog exceptions become
  `DatadogResponseError`. Then `DatadogPolicyError`, `DatadogConfigurationError` and
  `DatadogAuthenticationError` re-raise (first in selection order). Any other error goes to
  `skipped`. If every source is skipped, it raises the first source's error.
  `require_read()` runs once before the fan-out. `search` therefore only raises
  `DatadogError` subclasses, matching the documented contract.

### Non-functional

- Request cost: `search` makes at most 1 request per source, plus 1 more for metrics when the
  query is a tag (≤6 total). `describe_metric` makes 3 requests.
- Output bounds: `search` gives ≤ `limit` per source, plus extracted metrics capped at `limit`.
  `describe_metric` caps are in R1.

## Milestones

### [x] M1 — describe_metric end to end

- **Approach:** Add `METRIC` and the three per-metric allowlist entries to `client.py`, plus
  `validation.metric_name`. Add `formatting.metric_description`, then `describe_metric` in
  `metrics.py` (three sequential requests in one `datadog_client()`). Export it, add it to the
  PUBLIC/READS sets and the allowlist-drive test, and document it in `SKILL.md`. The skill
  test's function count becomes 15.
- **Test seam:** `describe_metric` via `dd_fake({...}, "metrics")`; client allowlist via `allowed()`.
- **Edge cases:** bare tag without `:` → key with no values; non-string tags ignored; assets
  missing `included` → empty lists.
- **Deliverable:** `datadog.describe_metric("aws.amazonmq.message_count")` returns grouped tags
  and linked assets.
- **Verify:** `uv run pytest -q tests/test_datadog_*` passes. A live call returns the `queue`
  key with values and the "RabbitMQ Queue Worker Dashboard".
- **Outcome:** Built `describe_metric` with `formatting.metric_description`, and added
  `validation.metric_name` and the three per-metric allowlist entries. I also added
  `validation.tag_filter`, `age` and `sources` now, for M2 and M3. The tests cover grouping,
  caps, an unknown metric, an `all-tags` 404, malformed metadata, bad names, and refused
  metric paths in the client.
  - `uv run pytest -q`: 1593 passed. Ruff check and format are clean.
  - Live `describe_metric("aws.amazonmq.message_count", max_values=3)` returned 18 tag keys,
    including `queue` (48 values), 4 dashboards including "RabbitMQ Queue Worker Dashboard",
    and 2 monitors, in 2,210 characters.

### [x] M2 — search_metrics tag and active_within

- **Approach:** Add `GET /api/v1/metrics` to the allowlist, plus `validation.tag_filter` and
  `validation.age`. Branch in `search_metrics` per R2. Update its `SKILL.md` section.
- **Test seam:** `search_metrics` via `dd_fake({...}, "metrics")`.
- **Edge cases:** `metrics: null` → `[]`; non-list → `DatadogResponseError`.
- **Deliverable:** `search_metrics(tag="queue:*")` returns the `aws.amazonmq.*` names.
- **Verify:** `uv run pytest -q tests/test_datadog_*` passes. A live `search_metrics(tag="queue:*", active_within="1d")` returns
  `aws.amazonmq.*` names.
- **Outcome:** `search_metrics` now takes `tag` and `active_within`, which use
  `GET /api/v1/metrics` with `from` and an optional `tag_filter`. I split out
  `search_metric_names` and `list_metric_names` helpers so `search` can reuse them. The
  allowlist-drive test makes an extra filtered call so the new path is covered.
  - `uv run pytest -q`: 1616 passed. Ruff check and format are clean.
  - Live, `search_metrics(tag="queue:*", active_within="1d", limit=5)` returned 5
    `aws.amazonmq.*` names. `("message_count", tag="environment:production")` returned the 5
    message_count variants. A name-only `"rabbitmq"` still returns `[]`.

### [x] M3 — search() across sources

- **Approach:** Add the two aggregate POSTs to the allowlist and `validation.sources`. Write
  `discovery.py` per Design: per-source coroutines run with `asyncio.gather`, monitor query
  metric extraction (regex for a metric token directly before `{`, only for `query alert` and
  `metric alert` monitors), dedupe and the skipped map. Export it, update the registration
  tests (count 16), and add the `SKILL.md` section plus a "Discovering what exists" playbook.
  Also update the "Choose the right function" table and the investigation paragraph to start
  from `search`. Note in "Shared input rules" that `search` limits are 1–50, and in "Rate
  limits that matter" that `search(sources=["spans"])` shares the span limit. M1 and M2 add
  their rows to the "Choose the right function" table.
- **Test seam:** `search` via `dd_fake({...}, "discovery")`.
- **Edge cases:** one source raises a rate-limit error → skipped; all fail → raise; a monitor
  with no `{` → no extraction; a log-alert monitor → no extraction; dashboards matched by
  description.
- **Deliverable:** `datadog.search("rabbitmq")` returns RabbitMQ monitors, the dashboard,
  `aws.amazonmq.*` metrics with handoff `datadog.describe_metric`, and log services.
- **Verify:** `uv run pytest -q`, `uv run ruff check .` and `uv run ruff format --check .`
  pass. A live `search("rabbitmq")` includes a metric starting
  `aws.amazonmq.`.
- **Outcome:** Built `discovery.py` and the two aggregate allowlist entries. The SKILL.md
  `search` section and the "Discovering what exists" playbook are written, and the function
  table, investigation paragraph, limit rule and rate-limit notes are updated. 40 tests are
  in `tests/test_datadog_discovery.py`, and the registration and allowlist-drive tests now
  cover 16 functions.
  - `uv run pytest -q`: 1656 passed. `ruff check` and `ruff format --check` are clean.
  - Live `search("rabbitmq", limit=5)` took 0.6s with nothing skipped (2,493 characters). It
    returned 5 monitors, 1 dashboard, 4 log services, and 3 `aws.amazonmq.*` metrics taken
    from monitor queries (`system_cpu_utilization`, `rabbit_mqmem_used`, `rabbit_mqmem_limit`).
  - Live `search("queue:audit-log", sources=["metrics"])` returned `aws.amazonmq.*` tag
    matches.
  - Live `search("rabbitmq", sources=["spans","dashboards"])` returned span service
    `core-worker` (488 spans) with nothing skipped.
  - Deviation below: tag-shaped queries skip the name search.
  - The monitor-query regex now requires an `<aggregator>:` prefix. A test caught `by {host}`
    being read as metric `by`.

## Deviations

- M3: For a tag-shaped query, `search`'s metrics source now uses only the tag listing, not
  name search plus tag listing. Live, Datadog's name search returned HTTP 400 (`bad query:
  metrics:queue:audit-log`) for a query containing `:`, which would have skipped the whole
  metrics source. Metric names can't contain `:`, so no results are lost. R3's merge rule
  ("name matches first, then tag matches") no longer applies, and `matched_on` for these
  results is always `"tag"`.

## Risks

- Datadog rate limits on `all-tags`, `assets` and the aggregate endpoints are unmeasured. A
  burst of `describe_metric` calls may hit 429, which surfaces as `DatadogRateLimitError`.
- The metric extraction regex is heuristic. Formula or arithmetic queries may yield extra
  tokens, and these are filtered by `METRIC`.

## Review

- Plan review: 1 pass. I fixed the one ❌: D9 now names the extra request, its `from` and its
  limit. I fixed every ⚠️ and ℹ️:
  - R3 gives the full result-shape table, the D8/D9 ACs, the first-error ordering and the
    2×`limit` metric bound.
  - New D16 (dashboard matching), D17 (`start` scope) and D18 (client per source).
  - R1 gives the `all-tags` 404 response; R2 gives the blank-query response.
  - D12's Why is now specific to this codebase.
  - The timeout message is fixed.
  - The test seams use the right `dd_fake` signature.
  - The registration-test details are listed, and the function count rises per milestone.
- Plan review pass 2: no ❌. I fixed every ⚠️ and ℹ️:
  - Timeouts and unexpected exceptions map to `DatadogError` subclasses, so `search` keeps the
    error contract.
  - The fan-out uses `gather(return_exceptions=True)`, so nothing is left running.
  - R2 now covers the blank query and when `tag_filter` is sent.
  - The metrics base request and the merge order are named, as is the dashboard `matched_on`
    tie.
  - The SKILL.md sections to update are listed, and D16–D18 are reordered.
- Code review: I ran the `general` and `risk` roles in parallel, for 1 round. There were no ❌, so I ran no second round. I fixed every finding:
  - general ⚠️: monitor-query extraction missed percentile aggregators (`p95:`). The regex
    now accepts `p<n>` and `p<n>.<n>`, with tests for `p95` and `p99.9`.
  - general ℹ️: a bad `max_values` error said "limit". `validation.limit` now takes `name`,
    and a test checks the message.
  - general ℹ️: `tag_count` counted duplicate tags. It now counts distinct tags, which the
    test and SKILL.md reflect.
  - general ℹ️: the timeout tests patched the private `_dashboards`. They now slow the fake
    client at the network boundary and check the monitor results by id.
  - risk ℹ️: a multi-term query containing `:` sent a name search that always fails with 400.
    The metrics source now returns no metrics for such a query without sending the request.
    A test covers it, and SKILL.md says so.
  - general ❓ (`status:alert` taking the tag path): intended. Metric names can't contain
    `:`, so a tag listing is the only metric lookup such a term can do. A non-metric tag key
    returns `[]` cheaply.
  - risk ❓ (`from=0` payload size for a broad tag): accepted. The live unfiltered list was
    3,791 names and 148 KB in one request. Results are cut to `limit` before output, and
    `search_metrics(tag=..., active_within=...)` is the recency-bounded option.
- Merge risk: Low. Every change is a new read-only function or an optional argument. The
  allowlist gains six read entries and no DELETE. The worst realistic failure is a wrong or
  missing discovery result, which the agent sees and can route around with the existing
  functions.

## Outcome

Delivered three discovery functions:
- `datadog.describe_metric`
- `tag` and `active_within` filters on `datadog.search_metrics`
- `datadog.search`, across monitors, dashboards, metrics, logs and spans

SKILL.md now documents all three and has a "Discovering what exists" playbook. The tool
exposes 16 functions.

Evidence:
- `uv run pytest -q`: 1660 passed. `ruff check` and `ruff format --check` are clean.
- Live against the configured account:
  - `search("rabbitmq")` returned 5 monitors, a dashboard, 4 log services and 3
    `aws.amazonmq.*` metrics in 0.5s.
  - `describe_metric("aws.amazonmq.message_count")` returned 18 tag keys, 4 dashboards and
    2 monitors.
  - `search_metrics(tag="queue:*", active_within="1d")` returned `aws.amazonmq.*` names.

Not verified:
- Rate limits for `all-tags`, `assets` and the aggregate endpoints under sustained use.
- `search(..., sources=["spans"])` under load: it worked live once and was not
  rate-limited.

PR: https://github.com/simon-downes/archie-nexus/pull/7
CI: none (no checks configured on the repository)

## Follow-ups

- Group aggregation variants (`.maximum`, `.minimum`, `.sum`, `.samplecount`) in
  `search_metrics` and `search` results.
- Catalog entities as a `search` source, if the catalog becomes populated.

# 069 — Datadog discovery: search(), describe_metric(), search_metrics filters

Status: planning
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
| D9 | Metric tag match in `search` | Only when the query looks like a tag (`key:value`, no whitespace) | Always; never | `v2` tag filters need a key; `*rabbitmq*` returned nothing | self |
| D10 | `search_metrics` with tag/active_within | `GET /api/v1/metrics` with `from` + `tag_filter`, then case-insensitive name substring filter | `v2/metrics` with cursor paging | One request, no paging; v1 accepts both filters | self |
| D11 | `active_within` unset but `tag` set | `from=0` (any metric Datadog still lists) | Default 1d | Doesn't silently drop quiet metrics; `active_within` is the explicit way to require recency | self |
| D12 | `active_within` format | Relative age only (`15m`, `2h`, `30d`), max 365d | ISO datetimes too | It's a duration; matches the existing relative-age syntax | self |
| D13 | Tag values in `describe_metric` | Grouped by key; `max_values` per key (default 20, 1–100); each key reports its total count | Flat list | 111+ tags per metric; 357 over 30 days | self |
| D14 | Metric name validation | `^[A-Za-z][A-Za-z0-9_.]{0,199}$`, used in the allowlist path patterns too | Free text | Datadog's metric-name charset; no `/` means no path traversal | self |
| D15 | Variant grouping (`.maximum` etc.) | Not built; Follow-up | Build now | Not in the user's list for this build | self |

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
- MUST raise `DatadogNotFoundError` for an unknown metric.
  - AC: a metadata 404 propagates as `DatadogNotFoundError`.

**R2 `search_metrics(query=None, limit=50, tag=None, active_within=None)`**
- MUST keep the current behaviour when only `query` is given (`/api/v1/search`, `[]` on no match).
  - AC: existing `search_metrics` tests pass unchanged.
- MUST, when `tag` or `active_within` is set, list names via `GET /api/v1/metrics` with
  `tag_filter=tag` and `from=now - active_within` (or `0`). It then keeps names containing
  `query` (case-insensitive), sorts them and cuts to `limit`.
  - AC: the fake sees `tag_filter` and `from` as specified; the names are filtered and limited.
- MUST raise `DatadogInputError` if none of the three is given, or if `tag` is blank, has
  whitespace or exceeds 500 characters, or if `active_within` isn't a relative age up to 365d.
  - AC: each case raises with no call.

**R3 `search(query, sources=None, start="1h", limit=10)`**
- MUST return `{"results": [...], "skipped": {...}}`. Each result has `type`
  (`monitor|dashboard|metric|service`), `id`, `title`, `source`, `matched_on`, `count`
  (services only, otherwise `None`) and `handoff` (a `datadog.*` function name).
  - AC: a fake-backed search over all default sources returns each type with the right handoff.
- MUST search the sources in D3, each limited to `limit`, with `spans` only when requested.
  - AC: default sources make no spans call; `sources=["spans"]` makes only the spans call.
- MUST add metric results extracted from matched metric/query-alert monitor queries, with
  `matched_on` naming the monitor, and dedupe metrics by name.
  - AC: a monitor `max(last_5m):max:aws.amazonmq.x{broker:dev} > 1` yields metric
    `aws.amazonmq.x`. The same name from the name search appears once.
- MUST put a failed source in `skipped` with the error class and message, and return the other
  results. If every selected source fails, it MUST raise the first error.
  - AC: spans raising `DatadogRateLimitError` → `skipped["spans"]` set, other results present.
    All sources failing → raises.
- MUST validate `query` (required), `sources` (non-empty unique subset), `start` (relative age or
  ISO, as `time_window`) and `limit` (1–50) before any request.
  - AC: each invalid input raises `DatadogInputError` with no call.
- MUST bound each source with its own timeout under the client deadline, so a slow source is
  skipped rather than failing the call.
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
- `__init__.py`: export both; tests updated in `test_datadog_reads.py` (new functions),
  `test_datadog_monitors.py` (PUBLIC/allowlist drive), `test_datadog_client.py` (allowlist),
  `test_datadog_skill.py` (count 16). New `tests/test_datadog_discovery.py` for `search`.

### Error handling

- `describe_metric`: errors propagate (404 → `DatadogNotFoundError`).
- `search`: each source runs as `asyncio.timeout(25)` → its coroutine. A `DatadogError` or
  `TimeoutError` becomes `skipped[source] = "<Class>: <message>"`. `DatadogPolicyError`,
  `DatadogConfigurationError` and `DatadogAuthenticationError` re-raise, because they mean
  every source would fail. If every source is skipped, it raises the first error.

### Non-functional

- Request cost: `search` makes at most 1 request per source, plus 1 more for metrics when the
  query is a tag (≤6 total). `describe_metric` makes 3 requests.
- Output bounds: `search` gives ≤ `limit` per source, plus extracted metrics capped at `limit`.
  `describe_metric` caps are in R1.

## Milestones

### [ ] M1 — describe_metric end to end

- **Approach:** Add `METRIC` and the three per-metric allowlist entries to `client.py`, plus
  `validation.metric_name`. Add `formatting.metric_description`, then `describe_metric` in
  `metrics.py` (three sequential requests in one `datadog_client()`). Export it, add it to the
  PUBLIC/READS sets and the allowlist-drive test, and document it in `SKILL.md`.
- **Test seam:** `describe_metric` via `dd_fake("metrics")`; client allowlist via `allowed()`.
- **Edge cases:** bare tag without `:` → key with no values; non-string tags ignored; assets
  missing `included` → empty lists.
- **Deliverable:** `datadog.describe_metric("aws.amazonmq.message_count")` returns grouped tags
  and linked assets.
- **Verify:** `uv run pytest -q tests/test_datadog_*` passes. A live call returns the `queue`
  key with values and the "RabbitMQ Queue Worker Dashboard".
- **Outcome:**

### [ ] M2 — search_metrics tag and active_within

- **Approach:** Add `GET /api/v1/metrics` to the allowlist, plus `validation.tag_filter` and
  `validation.age`. Branch in `search_metrics` per R2. Update its `SKILL.md` section.
- **Test seam:** `search_metrics` via `dd_fake("metrics")`.
- **Edge cases:** `metrics: null` → `[]`; non-list → `DatadogResponseError`.
- **Deliverable:** `search_metrics(tag="queue:*")` returns the `aws.amazonmq.*` names.
- **Verify:** tests pass. A live `search_metrics(tag="queue:*", active_within="1d")` returns
  `aws.amazonmq.*` names.
- **Outcome:**

### [ ] M3 — search() across sources

- **Approach:** Add the two aggregate POSTs to the allowlist and `validation.sources`. Write
  `discovery.py` per Design: per-source coroutines run with `asyncio.gather`, monitor query
  metric extraction (regex for a metric token directly before `{`, only for `query alert` and
  `metric alert` monitors), dedupe and the skipped map. Export it, update the registration
  tests, and add the `SKILL.md` section plus a "Discovering what exists" playbook.
- **Test seam:** `search` via `dd_fake("discovery")`.
- **Edge cases:** one source raises a rate-limit error → skipped; all fail → raise; a monitor
  with no `{` → no extraction; a log-alert monitor → no extraction; dashboards matched by
  description.
- **Deliverable:** `datadog.search("rabbitmq")` returns RabbitMQ monitors, the dashboard,
  `aws.amazonmq.*` metrics with handoff `datadog.describe_metric`, and log services.
- **Verify:** all checks pass. A live `search("rabbitmq")` includes a metric starting
  `aws.amazonmq.`.
- **Outcome:**

## Deviations

## Risks

- Datadog rate limits on `all-tags`, `assets` and the aggregate endpoints are unmeasured. A
  burst of `describe_metric` calls may hit 429, which surfaces as `DatadogRateLimitError`.
- The metric extraction regex is heuristic. Formula or arithmetic queries may yield extra
  tokens, and these are filtered by `METRIC`.

## Review

## Outcome

PR: pending
CI: pending

## Follow-ups

- Group aggregation variants (`.maximum`, `.minimum`, `.sum`, `.samplecount`) in
  `search_metrics` and `search` results.
- Catalog entities as a `search` source, if the catalog becomes populated.

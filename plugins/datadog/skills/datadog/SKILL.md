---
name: datadog
description: >
  Use for Datadog investigation and monitor upkeep through the exec datadog namespace:
  discovery search across monitors, dashboards, metrics and services, metric queries,
  metric search and description (tags, where used), log and span search, the event stream,
  monitor search and details, dashboards, the software catalog and teams, and creating or updating
  monitors that this tool owns (tagged managedby:archie). Not for deleting anything,
  dashboard edits, downtimes or muting, SLOs, posting events, credentials, or raw Datadog
  API calls.
---

# Datadog

## Global rules

- Call Datadog only through `exec`, using `await datadog.<function>(...)`.
- Credentials are implicit. Never request, pass, print, or store Datadog keys, and do not make raw HTTP calls to Datadog.
- **Nothing can be deleted.** The tool only sends a fixed list of requests, and none of them deletes, mutes, or creates downtimes. Dashboards are read-only. Use only the `datadog.*` functions; never import the tool's internals or credential modules, or call Datadog any other way.
- **Writes are limited to monitors this tool owns.** `create_monitor` always adds the tag `managedby:archie`. `update_monitor` refuses any monitor without that tag, even when write policy is on. Do not try to add the tag to someone else's monitor to get around this.
- Reads are enabled by default; writes are disabled by default. Policy:

  ```yaml
  plugins:
    datadog:
      policy:
        read:
          enabled: true
        write:
          enabled: true   # needed for create_monitor and update_monitor
  ```

  A `scope` in either block is not supported; setting one makes every Datadog call fail with `DatadogPolicyError`.
- Get explicit user intent before creating or changing a monitor. A monitor can page people. Prefer `validate_monitor` first.
- Calls return JSON-serializable values or raise typed errors; they do not return error objects.
- Results are normalised and bounded (see each function). The exec result shown to you is capped at 32,000 characters, but data inside the script is not. If a result may exceed the cap, process it in the script and return only the part you need, or write it to a file under `/tmp` and read it in chunks with `read(path, offset, limit)`. Keep `limit`s small. A large dashboard alone can be over 10,000 characters.
- Logs and spans can contain customer data. Search narrowly (service, status, time window), and don't copy raw log messages into Slack, Jira or other external places without need.
- Results are a snapshot of a live system. Say when data was read if timing matters.

## Choose the right function

| Need | Function |
|---|---|
| Find anything about a term when you don't know what exists | `datadog.search` |
| A metric's values over time | `datadog.query_metrics` |
| Find metric names | `datadog.search_metrics` |
| Metrics that carry a tag (for example every metric with a `queue` tag), or that reported recently | `datadog.search_metrics(tag=..., active_within=...)` |
| A metric's tag keys and values, and the dashboards and monitors that use it | `datadog.describe_metric` |
| Error logs or log lines for a service | `datadog.search_logs` |
| Log counts or measures grouped by facets (top IPs, IP × user agent, per-10-minute counts) | `datadog.aggregate_logs` |
| Slow or failing requests (APM spans) | `datadog.search_spans` |
| Deploys, Kubernetes changes, integration and alert events | `datadog.list_events` |
| Which monitors are alerting, or monitors for a service | `datadog.search_monitors` |
| One monitor's query, message and options | `datadog.get_monitor` |
| Check a monitor definition without creating it | `datadog.validate_monitor` |
| Create or change a monitor this tool owns | `datadog.create_monitor`, `datadog.update_monitor` |
| Find a dashboard, and see its widgets and queries | `datadog.list_dashboards`, `datadog.get_dashboard` |
| Services, APIs and other catalog entities | `datadog.list_catalog_entities` |
| Datadog teams | `datadog.list_teams` |

For an investigation, start with `search_monitors("status:alert")`. Then use `query_metrics` on the monitor's query, `search_logs` and `search_spans` for the service in the same window, and `list_events` for deploys or changes just before the problem started. If you only have a name or a technology ("rabbitmq", "payments"), start with `search` instead; see "Discovering what exists".

## Discovering what exists

Use this when you don't know the metric names, tag keys or services yet.

1. Run `datadog.search("<term>")`. It returns monitors, dashboards, metrics and the services that log the term, each with a `handoff` naming the function to call next.
2. If it returns metrics, call `describe_metric` on the one or two that look right. Use its tag keys and values to build the query, and its dashboards and monitors to see working queries.
3. If it returns no metrics but does return monitors or dashboards, open one with `get_monitor` or `get_dashboard` and reuse its query. Metric names often differ from the product name: RabbitMQ on Amazon MQ is `aws.amazonmq.*`.
4. If you know a tag key, list the metrics that carry it with `search_metrics(tag="<key>:*")`, or call `search("<key>:<value>")`.
5. Query with `query_metrics`, grouping by a tag (`by {queue}`) to see its live values. Narrow the query one filter at a time. An empty result means the metric, a tag or the window is wrong, but it doesn't say which.

```python
async def main():
    found = await datadog.search("rabbitmq", limit=5)
    metrics = [r["id"] for r in found["results"] if r["type"] == "metric"]
    detail = await datadog.describe_metric(metrics[0], max_values=10) if metrics else None
    return {"skipped": found["skipped"], "metrics": metrics, "detail": detail}
```

## Shared input rules

- **Query strings** use Datadog's own syntax, the same as in the Datadog UI: metric queries (`avg:system.cpu.user{env:prod} by {host}`), log and span search (`service:api status:error @http.status_code:502`), event search, and monitor search (`status:alert tag:service:api`). They are 1–2,000 characters. Optional queries may be `None` or blank, meaning no filter.
- **Times** (`start`, `end`): an ISO-8601 datetime (naive means UTC), or a relative age `<n>m`, `<n>h` or `<n>d` meaning that long ago. `start` defaults to `1h` and `end` to now. `end` must be after `start`, and the window can be at most 31 days. Output timestamps are UTC strings.
- **Limit:** an integer `1..100` (`bool` is invalid). For `search`, `1..50` per source.
- **Monitor ID:** a positive integer. **Dashboard ID:** the `abc-def-ghi` form from `list_dashboards` or a dashboard URL.
- **Sort** (logs, spans): `newest` (default) or `oldest`.
- Invalid input raises `DatadogInputError` before any request.

## Rate limits that matter

Datadog limits each endpoint separately, and some limits are tight:

- `search_spans`: 5 calls per minute. Make one well-filtered call, not a loop. `search(sources=[..., "spans"])` uses a span endpoint with a similarly tight limit, which is why spans are not searched by default.
- `search_logs`: 10 calls per 10 seconds.
- `aggregate_logs`: Datadog doesn't document this limit. Pace it like `search_logs`: no more than about one call per second in a loop.
- `describe_metric` makes three requests per call. Describe the metrics you need, not every search result.
- Other reads have higher limits, but don't call them in tight loops.

Nothing retries automatically. On `DatadogRateLimitError`, wait `retry_after_seconds` before one retry.

## Retrieving larger datasets

Each `search_logs` call returns at most `limit` (100) records, and no function returns a cursor for the next page. Before fetching records, check whether a count answers the question:

- **Counts, top values and per-window peaks:** use `aggregate_logs`. Datadog counts on the server, grouped by up to four facets, so one call covers millions of logs. Examples: the busiest IPs in a window, the user agents behind each IP, or counts per 10 minutes.
- **Counts or rates from metrics:** if a metric exists (for example `trace.*` APM metrics, or a log-based metric found with `search`), `query_metrics` covers the whole window in one call.
- **A sample:** if 100 recent records answer the question, make one `search_logs` call.
- **A narrower query:** add `service`, `status`, `env` or an `@attribute` filter, or shorten the window, before paging through noise.

### Aggregating over a long window

`aggregate_logs` returns at most 10,000 buckets per call. Over a long window with many distinct values, such as client IPs, that isn't enough. Split the window into fixed slices instead and aggregate each one; a day in 10-minute slices takes 144 calls. Fixed slices undercount bursts that cross a slice boundary by up to half. To catch those, run a second pass with slices offset by half a slice, or leave headroom in any threshold you derive.

```python
from datetime import UTC, datetime, timedelta

async def main():
    query = "service:web env:production @http.status_code:404"
    facet = "@http.x_forwarded_for"
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    start = end - timedelta(hours=24)
    peaks, step = {}, timedelta(minutes=10)
    slice_start = start
    while slice_start < end:
        result = await datadog.aggregate_logs(
            query, [facet], start=slice_start.isoformat(),
            end=(slice_start + step).isoformat(),
        )
        for bucket in result["buckets"]:
            key = bucket["by"][facet]
            peaks[key] = max(peaks.get(key, 0), bucket["value"])
        slice_start += step
        await asyncio.sleep(1)
    top = sorted(peaks.items(), key=lambda kv: -kv[1])[:20]
    return {"keys": len(peaks), "top_10m_peaks": top}
```

Each slice returns the top 100 values by default, which is enough to find the busiest clients. Raise the first level's `limit` (up to 10,000) if you need every value.

Before you trust grouped counts, check two things on a short window:

1. **The facet holds what you think.** Behind a proxy or load balancer, `@network.client.ip` can be the proxy's internal address, and `@http.x_forwarded_for` can be the whole chain (`client, edge`). In that case the same client splits across buckets, one per edge address. Compare a few buckets with the raw `message` from `search_logs`.
2. **The filter matches the same records as a free-text query.** A facet filter only matches records where the attribute was parsed. Compare `aggregate_logs(query)` totals for the facet filter and for a free-text equivalent. A large gap means some records aren't parsed, and grouped counts will miss them.

### Paging through records

If you need the records themselves, page by time. Sort newest first (the default), then call again with `end` set to the oldest `timestamp` you have so far. Dedupe on `id`: a record at the boundary timestamp can come back twice. Records that share the boundary timestamp beyond the 100 already returned can be missed, so treat a page in which every record has the same timestamp as a sign to narrow the query. Fix `start` as an absolute time before the loop, because a relative `start` such as `6h` moves on each call. Stop when a page is short, or when the next `end` would not be after `start` (that raises `DatadogInputError`).

Keep the records in the script, and return a summary. To inspect raw records afterwards, write them to a file under `/tmp` and `read` it in chunks.

```python
from collections import Counter
from datetime import UTC, datetime, timedelta

async def main():
    query = "service:api status:error"
    start = datetime.now(UTC) - timedelta(hours=6)
    seen, rows, end = set(), [], None
    for page in range(10):  # at most 1,000 logs
        if page:
            await asyncio.sleep(1)  # stay under 10 calls per 10 seconds
        batch = await datadog.search_logs(query, start=start.isoformat(), end=end, limit=100)
        new = [r for r in batch if r["id"] not in seen]
        seen.update(r["id"] for r in new)
        rows += new
        oldest = batch[-1]["timestamp"] if batch else None
        if len(batch) < 100 or not new or datetime.fromisoformat(oldest) <= start:
            break
        end = oldest
    with open("/tmp/api-errors.jsonl", "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    top = Counter(r["message"][:120] for r in rows).most_common(10)
    return {"fetched": len(rows), "oldest": rows[-1]["timestamp"] if rows else None, "top": top}
```

If the loop stops at its page cap, `oldest` shows how far back the records go. Say so instead of presenting the result as the whole window.

The same approach works for the other time-ordered reads, with these differences:

- `list_events` is always newest first. Use each event's `timestamp` as the next `end`.
- `search_spans` allows only 5 calls per minute. Use each span's `start` as the next `end`, and keep the loop to two or three pages. Spans have no `id`, so dedupe on `(trace_id, span_id)`. For more than a few hundred spans, use APM metrics with `query_metrics` instead.
- The other list functions (`search_monitors`, `list_dashboards`, `list_catalog_entities`, `list_teams`) are not time-ordered and can't be paged. Narrow them with `query` (and `kind` for the catalog).

## Functions

### `datadog.search(query, sources=None, start="1h", limit=10)`

Searches several sources for one term at the same time, and returns references to follow up with other functions:

```json
{
  "results": [
    {"type": "monitor", "id": 123, "title": "RabbitMQ high CPU", "source": "monitors", "matched_on": "monitor search", "count": null, "handoff": "datadog.get_monitor"},
    {"type": "dashboard", "id": "abc-def-ghi", "title": "RabbitMQ Queue Worker Dashboard", "source": "dashboards", "matched_on": "title", "count": null, "handoff": "datadog.get_dashboard"},
    {"type": "metric", "id": "aws.amazonmq.system_cpu_utilization", "title": "aws.amazonmq.system_cpu_utilization", "source": "monitors", "matched_on": "monitor 123", "count": null, "handoff": "datadog.describe_metric"},
    {"type": "service", "id": "core-worker", "title": "core-worker", "source": "logs", "matched_on": "log content", "count": 3, "handoff": "datadog.search_logs"}
  ],
  "skipped": {"spans": "DatadogRateLimitError: Datadog rate limit reached; retry after 30 seconds"}
}
```

What each source matches:

| Source | Matches | Result |
|---|---|---|
| `monitors` | Monitor search: name, query and tags | `monitor`. Metric names used in metric-monitor queries also come back as `metric` results with `matched_on: "monitor <id>"`, and this is often the only route from a product name to its metrics. |
| `dashboards` | Title or description (case-insensitive; widgets are not searched) | `dashboard`, with `matched_on` `title` or `description` |
| `metrics` | Metric names containing the term. A single `key:value` term (such as `queue:audit*`) instead matches metrics that carry that tag. Any other term containing `:` finds no metrics. | `metric`, with `matched_on` `name` or `tag` |
| `logs` | Log search over `start` to now, grouped by service | `service`, with `count` = matching log events. No log content is returned. |
| `spans` | Span search over `start` to now, grouped by service. A single word also matches `service` and `resource_name`. Not searched by default. | `service`, with `count` = matching spans |

- `sources`: a list from `monitors`, `dashboards`, `metrics`, `logs` and `spans`. The default is every source except `spans`.
- `query` uses each source's own syntax, so a plain word is the most portable choice. Logs get the query as given.
- `limit` applies to each source separately (1–50). Metric results can be up to twice `limit`: name and tag matches, plus metrics taken from monitor queries. A metric found more than once appears once.
- `start` only affects `logs` and `spans`.
- A source that fails is listed in `skipped` with its error, and the other results are still returned. If every source fails, the first source's error is raised. Authentication, configuration and policy errors are always raised.
- Each source has a 25-second limit. A slow source is skipped with `DatadogTransportError`.

### `datadog.query_metrics(query, start="1h", end=None)`

Runs a metric query and returns:

```json
{
  "query": "avg:system.cpu.user{*}",
  "from": "2026-10-01T10:00:00Z",
  "to": "2026-10-01T11:00:00Z",
  "series_count": 1,
  "series": [
    {
      "metric": "system.cpu.user",
      "display_name": "system.cpu.user",
      "expression": "avg:system.cpu.user{*}",
      "scope": "*",
      "tags": [],
      "unit": "percent",
      "interval_seconds": 20,
      "point_count": 180,
      "points": [["2026-10-01T10:00:00Z", 12.5]],
      "truncated": false
    }
  ],
  "truncated": false
}
```

At most 20 series are returned. Each keeps its **last** 300 points; `point_count` is the full count. A point value can be `null`. A query that Datadog cannot parse raises `DatadogRequestRejectedError` with Datadog's message.

### `datadog.search_metrics(query=None, limit=50, tag=None, active_within=None)`

Returns metric names, for example `["aws.ec2.cpuutilization", ...]`. Returns `[]` when nothing matches.

**By name** (only `query`): a substring match on the metric **name**, which does not search tags or integration names. `query` is required in this form. A search for `rabbitmq` finds nothing if RabbitMQ metrics are named `aws.amazonmq.*`. If a name search returns `[]`, try the provider prefix (`aws.amazonmq`) or a single name fragment (`message_count`), or filter by tag instead.

**By tag or recency** (`tag` or `active_within` set):

- `tag` keeps metrics that carry a tag: `queue:*` (any value of `queue`), `queue:audit*` (a value prefix), `queue:audit-log`, or several tags separated by commas (`environment:production,queue:*`). It cannot match a bare word across all tags (`*rabbitmq*` finds nothing), so you need to know the tag key. Get keys from `describe_metric` on a related metric.
- `active_within` (`15m`, `2h`, `30d`, at most `365d`) keeps metrics that reported within that age. Without it, a `tag` search includes quiet metrics too.
- `query` is optional here and keeps names that contain it (case-insensitive). Results are sorted by name.

```python
names = await datadog.search_metrics(tag="queue:*", active_within="1d")
names = await datadog.search_metrics("message_count", tag="environment:production")
```

### `datadog.describe_metric(name, max_values=20)`

Describes one metric so you can write a correct query. It makes three requests, so describe the one or two metrics you will use, not every search result.

```json
{
  "name": "aws.amazonmq.message_count",
  "type": "gauge",
  "unit": "message",
  "per_unit": null,
  "description": "The total number of messages stored on the broker.",
  "integration": null,
  "tags": {
    "environment": {"values": ["dev", "production", "staging"], "value_count": 3},
    "queue": {"values": ["asset-processing", "audit-log", "..."], "value_count": 59}
  },
  "tag_count": 111,
  "dashboards": [{"id": "abc-def-ghi", "title": "RabbitMQ Queue Worker Dashboard"}],
  "monitors": [{"id": "125239081", "title": "Core Queue Job Count High ({{value}} jobs)"}],
  "truncated": false
}
```

- `tags` maps each tag key to its sorted values, at most `max_values` (1–100) per key and 100 keys. `value_count` is the full number of values for that key, and `tag_count` is the number of distinct tags. A tag with no `:` appears as a key with no values. Use the keys in `{...}` filters and `by {...}` groupings.
- `dashboards` and `monitors` (at most 50 each) are where the metric is already used. Pass a dashboard `id` to `get_dashboard`, or a monitor `id` (as an integer) to `get_monitor`, to see working queries.
- `name` is a metric name: a letter, then letters, digits, `_` or `.`. An unknown metric raises `DatadogNotFoundError`. A metric with no tag data returns `tags` as `{}`.

### `datadog.search_logs(query, start="1h", end=None, limit=20, sort="newest")`

Returns log records:

```json
{
  "id": "...",
  "timestamp": "2026-10-01T10:59:00Z",
  "service": "api",
  "host": "i-0abc",
  "status": "error",
  "message": "Payment failed ...",
  "tags": ["env:prod"],
  "attributes": {"http.status_code": 502, "context": "…"},
  "truncated": false
}
```

`message` is at most 2,000 characters and `tags` at most 50. `attributes` has at most 30 top-level keys; strings are cut at 200 characters, and nested objects or lists show as `"…"`. To read a nested attribute, put it in the query (`@context.status:ERROR`) rather than expecting it in the output.

### `datadog.aggregate_logs(query, group_by=None, start="1h", end=None, aggregation="count", metric=None, interval=None, order="desc")`

Aggregates matching logs on the server and returns buckets, not log records:

```json
{
  "from": "2026-10-01T10:00:00Z",
  "to": "2026-10-01T10:10:00Z",
  "group_by": ["@network.client.ip", "@http.useragent"],
  "bucket_count": 2,
  "buckets": [
    {"by": {"@network.client.ip": "203.0.113.7", "@http.useragent": "PowerShell/7.4"}, "value": 412},
    {"by": {"@network.client.ip": "198.51.100.2", "@http.useragent": "Mozilla/5.0 ..."}, "value": 38}
  ],
  "partial": false,
  "warnings": []
}
```

- `group_by`: up to four levels. Each level is a facet name (`"service"`, `"@http.useragent"`) or a dict `{"facet": ..., "limit": ..., "missing": ...}`. The default `limit` is 100 for the first level and 10 for later ones. The product of the limits must be at most 10,000. Each level keeps its top values by the aggregation, so `["@network.client.ip", "@http.useragent"]` means the top 100 IPs, then the top 10 user agents for each IP. Logs without a facet are left out unless you set `missing` to a placeholder such as `"none"`. With no `group_by`, one bucket holds the total for the whole query.
- You can only group by facets. A query can filter on an attribute that has no facet, but grouping by it fails or returns nothing. If a grouped call returns no buckets while the same query has matches in `search`, check that the attribute is a facet.
- `aggregation`: `count` (default), `cardinality`, `sum`, `min`, `max`, `pc75`, `pc90`, `pc95`, `pc98` or `pc99`. Every aggregation except `count` needs `metric`, which is the facet or measure to aggregate (`@duration`, `@network.client.ip` for `cardinality`).
- `interval` (`30s`, `10m`, `1h`): returns a timeseries per bucket. Each bucket then has `points`, a list of `[time, value]` pairs, instead of `value`. Without it, each bucket has a single `value` for the whole window.
- `order`: `desc` (default) keeps the largest values at each level; `asc` keeps the smallest.
- `partial` is true when Datadog timed out and returned partial results, and `warnings` holds Datadog's warning titles. Treat partial results as a lower bound, and retry with a shorter window.
- A request that would exceed the bucket cap is rejected before any call is made. The tool follows Datadog's cursor for up to five pages.
- Counts are of log records. If one record bundles several lines, such as an access log that ships several requests in one event, the count undercounts requests.

### `datadog.search_spans(query, start="1h", end=None, limit=20, sort="newest")`

Returns spans:

```json
{
  "trace_id": "...",
  "span_id": "...",
  "parent_id": "...",
  "service": "api",
  "resource": "GET /orders",
  "operation": "http.request",
  "env": "prod",
  "start": "2026-10-01T10:59:00.169Z",
  "end": "2026-10-01T10:59:00.541Z",
  "duration_ms": 372.04,
  "status": "error",
  "error": "http_error: 502 Bad Gateway",
  "truncated": false
}
```

This makes one request, so it returns at most `limit` spans. `error` is at most 500 characters. Custom span attributes are not returned.

### `datadog.list_events(query=None, start="1h", end=None, limit=20)`

Returns events, newest first:

```json
{"id": "...", "timestamp": "...", "title": "... or null", "message": "...", "source": "kube_cronjob", "tags": ["service:api"], "truncated": false}
```

### `datadog.search_monitors(query=None, limit=20)`

Returns monitor summaries:

```json
{
  "id": 123,
  "name": "API 5xx rate",
  "type": "query alert",
  "status": "Alert",
  "query": "sum(last_5m):...",
  "tags": ["service:api"],
  "priority": 2,
  "managed": false,
  "created": "...",
  "modified": "...",
  "truncated": false
}
```

`status` is Datadog's state: `OK`, `Alert`, `Warn`, `No Data`, and so on. `managed` is true when the monitor has `managedby:archie`, which means `update_monitor` may change it. Useful queries are `status:alert`, `tag:service:api`, `type:log`, and `tag:managedby:archie`.

### `datadog.get_monitor(monitor_id)`

Returns a monitor summary plus `message` (up to 4,000 characters) and `options`. Only these option keys are shown and accepted: `thresholds`, `notify_no_data`, `no_data_timeframe`, `renotify_interval`, `evaluation_delay`, `new_group_delay`, `require_full_window`, `include_tags`, `notify_audit`, `escalation_message`, `timeout_h`.

### `datadog.validate_monitor(name, type, query, message, tags=None, priority=None, options=None)`

Asks Datadog whether a definition is valid, without creating anything. It needs read policy only. Returns `{"valid": true}`, or raises `DatadogRequestRejectedError` with Datadog's reason. It takes the same inputs as `create_monitor`.

### `datadog.create_monitor(name, type, query, message, tags=None, priority=None, options=None)`

Creates a monitor and returns its detail. Needs write policy.

- `type`: a Datadog monitor type such as `metric alert`, `query alert`, `log alert`, `trace-analytics alert`, `event-v2 alert`, `composite`, `service check`, `rum alert`, or another type from Datadog's monitor API.
- `name`: up to 500 characters. `message`: up to 4,000 characters, including any `@` notification handles. Put a handle in only if the user asked for it.
- `tags`: up to 49 of your own tags; `managedby:archie` is always added.
- `priority`: 1 (highest) to 5.
- `options`: only the keys listed under `get_monitor`. Anything else, such as `restricted_roles`, is rejected.

### `datadog.update_monitor(monitor_id, name=None, query=None, message=None, tags=None, priority=None, options=None)`

Changes a monitor that has `managedby:archie`, and returns its detail as Datadog reports it after the change. Needs write policy.

- Only the arguments you pass are sent. At least one is required.
- `tags` replaces the tag list; `managedby:archie` is always kept.
- `options` is merged over the current options, so `options={"thresholds": {...}}` leaves the other options unchanged.
- A monitor without the tag raises `DatadogOwnershipError` before anything is changed.

### `datadog.list_dashboards(query=None, limit=50)`

Returns dashboard summaries: `{"id", "title", "description", "layout_type", "author", "created_at", "modified_at", "truncated"}`. `query` keeps dashboards whose title contains it (case-insensitive).

### `datadog.get_dashboard(dashboard_id)`

Returns a summary plus `url`, `template_variables` (names), `widget_count`, and up to 100 top-level `widgets`:

```json
{"id": 123, "type": "timeseries", "title": "Errors", "queries": ["sum:errors{$env}"], "truncated": false}
```

Each widget has at most 10 queries of up to 500 characters. A group widget also has `child_count`; widgets inside groups are not listed. Return only what you need, because large dashboards approach the exec output limit.

### `datadog.list_catalog_entities(kind=None, query=None, limit=50)`

Returns `{"id", "kind", "name", "display_name", "owner", "tags", "truncated"}` per entity. `kind` is a catalog kind such as `service`, `api`, `datastore`, `queue` or `system`. `query` filters by entity name.

### `datadog.list_teams(query=None, limit=50)`

Returns `{"id", "handle", "name", "member_count"}` per team. `query` matches team names and handles.

## Errors and retry policy

Errors are sanitized and never contain keys. Some carry Datadog's error message, which explains a bad query.

- `DatadogConfigurationError`: credentials are missing, or the Datadog site setting is invalid or unreadable. Do not retry; tell the user to run `archie auth login datadog` or fix the config.
- `DatadogPolicyError`: reads or writes are disabled, a scope is set, or the request is not one the tool permits. Do not bypass or retry.
- `DatadogInputError`: local validation failed. Correct the input.
- `DatadogOwnershipError`: the monitor is not tagged `managedby:archie`. It was not changed. Tell the user; do not retry.
- `DatadogRequestRejectedError`: Datadog rejected the query or definition (HTTP 400/422, or a metric query error). Fix it using the message; do not retry unchanged.
- `DatadogAuthenticationError`: the keys were rejected. Do not retry.
- `DatadogAuthorizationError`: the application key lacks a permission for this call. Do not retry; report which function failed.
- `DatadogNotFoundError`: the monitor, dashboard or metric does not exist.
- `DatadogRateLimitError`: throttled. `retry_after_seconds` is an integer or `None`. Wait at least that long before one retry.
- `DatadogTransportError`: the network failed or the 60-second call limit expired. Reads are safe to retry once.
- `DatadogResponseError`: Datadog returned something unexpected. Report it; retry a read at most once. After `update_monitor`, the message says the update was applied; check the monitor with `get_monitor` instead of retrying.
- `DatadogMutationIndeterminateError`: a create or update may or may not have happened. **Do not retry blindly.** Check with `get_monitor`, or `search_monitors("tag:managedby:archie")` for a create, before doing anything else.

## Examples

```python
async def main():
    alerting = await datadog.search_monitors("status:alert tag:service:api", limit=5)
    if not alerting:
        return "No alerting monitors for api"
    errors = await datadog.search_logs("service:api status:error", start="30m", limit=10)
    deploys = await datadog.list_events("service:api", start="6h", limit=5)
    return {
        "monitors": [(m["name"], m["status"]) for m in alerting],
        "errors": [(e["timestamp"], e["message"][:200]) for e in errors],
        "recent_events": [(e["timestamp"], e["title"] or e["message"][:100]) for e in deploys],
    }
```

```python
async def main():
    result = await datadog.query_metrics("sum:trace.http.request.errors{service:api}.as_count()", start="2h")
    return [
        {"scope": s["scope"], "latest": s["points"][-1] if s["points"] else None}
        for s in result["series"]
    ]
```

```python
async def main():
    definition = dict(
        name="API p95 latency high",
        type="metric alert",
        query="avg(last_10m):p95:trace.http.request{service:api} > 2",
        message="p95 latency above 2s for api.",
        tags=["service:api"],
        options={"thresholds": {"critical": 2}},
    )
    await datadog.validate_monitor(**definition)
    return await datadog.create_monitor(**definition)
```

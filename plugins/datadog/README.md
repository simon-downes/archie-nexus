# Datadog plugin

Gives the agent bounded reads over Datadog metrics, logs, spans, events, monitors, dashboards
and the software catalog, plus create/update for monitors it owns. It calls the Datadog REST
API directly with `httpx`.

## Namespaces and functions

One namespace, `datadog`. The model learns the functions from `skills/datadog/SKILL.md`.

- Read (`policy.read`): `datadog.search`, `datadog.query_metrics`, `datadog.search_metrics`,
  `datadog.describe_metric`, `datadog.search_logs`, `datadog.search_spans`,
  `datadog.list_events`, `datadog.search_monitors`, `datadog.get_monitor`,
  `datadog.validate_monitor`, `datadog.list_dashboards`, `datadog.get_dashboard`,
  `datadog.list_catalog_entities`, `datadog.list_teams`.
- Write (`policy.write`): `datadog.create_monitor`, `datadog.update_monitor`.

## Credential

Static, fields `api_key` and `app_key`. `archie auth login datadog` reads a JSON object from
stdin (`{"api_key": "...", "app_key": "..."}`) or, without piped input, prompts for both
with hidden input. Not projected into the container environment; the plugin reads the
credential through its context on each call.

## Configuration

```yaml
plugins:
  datadog:
    enabled: true
    settings:
      site: app.datadoghq.eu   # default
    policy:
      read: {enabled: true}    # default
      write: {enabled: true}   # default false; needed for create/update_monitor
```

| Key | Type | Default | Read | Meaning |
| --- | --- | --- | --- | --- |
| `settings.site` | str | `app.datadoghq.eu` | startup | Datadog site; the API host is `https://api.<site>` with any `app.` prefix removed |
| `policy.read.enabled` | bool | `true` | every exec call | All read functions |
| `policy.write.enabled` | bool | `false` | every exec call | Monitor create/update |

No scope: a non-empty `scope` in either block is a config load error, and the plugin's
gates also fail every call with `DatadogPolicyError` if one reaches them. `enabled: false`
removes the namespace and skill.

## How it works

- `client.py`: `DatadogClient` and `datadog_client()`; takes the credential and `site` from
  `context()`, applies a 60 s overall deadline, maps HTTP errors to typed `DatadogError`s and
  redacts keys from provider error text.
- `policy.py`: `require_read()` / `require_write()`, wrapping the SDK gates and rejecting scopes.
- `validation.py`: local input checks; every helper raises `DatadogInputError` before a request.
- `formatting.py`, `paging.py`: bounded normalisers and v2 paging helpers.
- `metrics.py`, `logs.py`, `traces.py`, `events.py`, `dashboards.py`, `catalog.py`,
  `discovery.py` (cross-source `search`), `monitors.py`: the tools.

Safety rules enforced in code:

- The client sends only the `(method, path)` pairs in `client._ALLOWED`; none is a DELETE, so
  nothing can be deleted, muted or downtimed. Any other request raises `DatadogPolicyError`.
- `create_monitor` always adds the tag `managedby:archie`; `update_monitor` fetches the
  monitor first and refuses (`DatadogOwnershipError`) unless it already carries that tag.
  Tags passed to an update keep the ownership tag.
- A write that may have reached Datadog and then failed raises
  `DatadogMutationIndeterminateError` instead of a plain transport error.
- `site` must be one of the known Datadog sites in `client.SITES`, otherwise
  `DatadogConfigurationError`.

No cache and no startup hook.

## Tests

`plugins/datadog/tests/`: `datadog_fakes.py` holds `FakeClient` (canned responses keyed by
`(method, path)`, checked against the real allowlist) and the fixtures
`datadog_context_installed` (autouse), `dd_fake` and `policy`; `conftest.py` imports them.
Run `uv run pytest -q plugins/datadog`.

See [docs/plugins.md](../../docs/plugins.md) for the manifest format, the plugin SDK and testing conventions.

# incident.io plugin

Gives the agent read-only access to incident.io: incidents and their timelines, alerts,
actions, follow-ups, post-mortems, on-call schedules, escalation paths, teams, users and the
catalog. It calls the incident.io REST API (`https://api.incident.io`) directly with `httpx`.

## Namespaces and functions

One namespace, `incident_io`. The model learns the functions from
`skills/incident-io/SKILL.md`.

- Read (`policy.read`): `incident_io.list_incidents`, `incident_io.get_incident`,
  `incident_io.incident_timeline`, `incident_io.incident_updates`, `incident_io.list_alerts`,
  `incident_io.get_alert`, `incident_io.list_actions`, `incident_io.list_follow_ups`,
  `incident_io.list_postmortems`, `incident_io.get_postmortem`, `incident_io.list_schedules`,
  `incident_io.on_call`, `incident_io.list_escalation_paths`, `incident_io.list_teams`,
  `incident_io.list_users`, `incident_io.list_catalog_types`,
  `incident_io.list_catalog_entries`, `incident_io.reference`.
- Write: none. The plugin has no write tools and no write policy block.

## Credential

Static, one field `api_key`. `archie auth login incident_io` reads a JSON object from stdin
(`{"api_key": "..."}`) or, without piped input, prompts for it with hidden input. The key is
sent as a Bearer token. Not projected into the container environment.

## Configuration

```yaml
plugins:
  incident_io:
    enabled: true
    policy:
      read: {enabled: false}   # default true
```

| Key | Type | Default | Read | Meaning |
| --- | --- | --- | --- | --- |
| `policy.read.enabled` | bool | `true` | every exec call | All functions |

No settings, no write block (a `policy.write` entry is a config load error) and no scope (a
non-empty `policy.read.scope` is a config load error; the gate also fails every call with
`IncidentIoPolicyError` if one reaches it). `enabled: false` removes the namespace and skill.

## How it works

- `client.py`: `IncidentIoClient` and `incident_io_client()`; takes the credential from
  `context()`, applies a 60 s overall deadline and at most 20 pages per list, and maps HTTP
  errors to typed `IncidentIoError`s. It exposes only `get` methods: there is no way to send
  a non-GET request.
- `policy.py`: `require_read()` around the SDK gate; no write gate.
- `validation.py`: local input checks, raising `IncidentIoInputError` before any request.
- `formatting.py`: normalisers; long text is returned whole, short metadata fields are bounded, and nested lists are capped at 50.
- `reference.py`: reference data and name-to-ID resolution used by the list filters.
- `incidents.py`, `alerts.py`, `work.py` (actions, follow-ups, post-mortems), `on_call.py`,
  `directory.py` (teams, users, catalog): the tools.

No settings, no cache and no startup hook.

## Tests

`plugins/incident_io/tests/`: `incident_io_fakes.py` holds `FakeClient` and the fixtures
`incident_io_context_installed` (autouse), `fake` (installs a `FakeClient` in named tool
modules) and `policy`; `conftest.py` imports them. Run `uv run pytest -q plugins/incident_io`.

See [docs/plugins.md](../../docs/plugins.md) for the manifest format, the plugin SDK and testing conventions.

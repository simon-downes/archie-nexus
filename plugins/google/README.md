# Google plugin

Gives the agent the signed-in user's Google Workspace: Gmail search, read and label/archive
changes, Calendar reads, Drive search and reads, Meet notes/transcripts/recordings, and a
cross-service search. It calls the Gmail, Calendar v3, Drive v3 and Meet v2 REST APIs with
`httpx` and one OAuth token.

## Namespaces and functions

Five namespaces, all documented for the model in `skills/google/SKILL.md`:

- `google`: `google.search` (metadata-only search across mail, calendar and drive).
- `mail`: read `mail.search`, `mail.read`, `mail.list_labels`; write `mail.label`,
  `mail.archive`, `mail.unarchive`, `mail.mark_read`, `mail.mark_unread`, `mail.star`,
  `mail.unstar`.
- `calendar`: read `calendar.today`, `calendar.upcoming`, `calendar.event`,
  `calendar.search`.
- `drive`: read `drive.search`, `drive.list`, `drive.metadata`, `drive.read`.
- `meet`: read `meet.notes`, `meet.transcript`, `meet.recording`.

Write = gated by `policy.write`; everything else by `policy.read`.

## Credential

One OAuth credential covers all five namespaces. `archie auth login google` runs the browser
flow through the orchestrator with fixed Google endpoints, `access_type=offline` and
`prompt=consent`, requesting the read-only Gmail, Calendar, Drive and Meet scopes plus
`gmail.modify` and `userinfo.email`. Google has no dynamic client registration, so
`plugins.google.auth.client_id` is required before login can start, and the redirect URI
`<orchestrator public_url>/auth/callback/google` must be registered on the Google OAuth
client. A client secret is not configuration: if one is needed it lives in the credential
store entry (`client_secret`) and is sent on the token exchange and refresh; it is never
returned by the auth API. The orchestrator refreshes the token before it expires. Not
projected into the container environment.

## Configuration

```yaml
plugins:
  google:
    enabled: true
    auth:
      client_id: your-client-id.apps.googleusercontent.com   # required for login
    settings:
      timezone: Europe/London   # default
    policy:
      read: {enabled: true}     # default
      write: {enabled: true}    # default false; needed for mail label/archive/read/star changes
```

| Key | Type | Default | Read | Meaning |
| --- | --- | --- | --- | --- |
| `auth.client_id` | str | none | startup (orchestrator) | Google OAuth client ID |
| `settings.timezone` | str | `Europe/London` | startup | IANA zone for the day boundaries of `calendar.today` and `calendar.upcoming`; an unknown zone falls back to the default |
| `policy.read.enabled` | bool | `true` | every exec call | All read functions |
| `policy.write.enabled` | bool | `false` | every exec call | Mail mutations |

No scope: a non-empty `scope` in either block is a config load error. `enabled: false`
removes all five namespaces and the skill.

## How it works

- `common.py`: typed `GoogleError`s, input validation, `require_read()` / `require_write()`,
  `credential()` (token from `context()`) and the shared `google_json()` / `google_text()`
  HTTP helpers, which never follow redirects and sanitise provider errors.
- `discovery.py` (`google`), `mail.py`, `calendar.py`, `drive.py`, `meet.py`: one module per
  namespace. `__init__.py` imports them relatively so `calendar` and `mail` never resolve to
  the stdlib modules.

Label cache: `mail.label` resolves USER label names to IDs through `mail.list_labels`'s data,
cached in the plugin cache under `google.labels` for 24 h (default `cache.max_bytes`, 256 KiB).
A warm cache lets `mail.label` run with reads disabled; a miss or malformed entry needs read
policy. System labels (INBOX, UNREAD, STARRED) can only be changed through the dedicated
functions. Mail mutations take 1-500 message IDs per call. No startup hook.

## Tests

`plugins/google/tests/`: `google_fakes.py` holds `fake_credential`, `google_context` and the
fixtures `google_context_installed` (autouse), `policy` and `google_settings`; `conftest.py`
imports them. Run `uv run pytest -q plugins/google`.

See [docs/plugins.md](../../docs/plugins.md) for the manifest format, the plugin SDK and testing conventions.

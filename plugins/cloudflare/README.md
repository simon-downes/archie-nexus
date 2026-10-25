# Cloudflare plugin

Gives agent containers a read-only Cloudflare API token and account id as environment
variables, for Cloudflare's `cf` CLI. The plugin has no Python code and no tools.

## Namespaces and functions

No namespaces and no tools.

## Credential

Static, one field `api_token`, mapped to `CLOUDFLARE_API_TOKEN`. `archie auth login cloudflare`
reads the token from the `CLOUDFLARE_API_TOKEN` environment variable on the host (there is no
prompt or JSON stdin; it fails naming the variable when it is unset):

```bash
CLOUDFLARE_API_TOKEN=... archie auth login cloudflare
```

`set_env: true`: new containers get `CLOUDFLARE_API_TOKEN`.

## Configuration

```yaml
plugins:
  cloudflare:
    enabled: true
    settings:
      account_id: 0123456789abcdef0123456789abcdef   # default none -> CLOUDFLARE_ACCOUNT_ID
```

| Key | Type | Default | Read | Meaning |
| --- | --- | --- | --- | --- |
| `settings.account_id` | str | none | container start | Exported as `CLOUDFLARE_ACCOUNT_ID`; `cf` uses it for account-scoped commands |

No tool policy (no tools). `enabled: false` stops the token and the account id being exported
and hides login and status for the provider.

## How it works

`shared/src/archie_shared/credentials/runtime.py` (`runtime_environment()`) builds the
environment for each new container: the stored `api_token` as `CLOUDFLARE_API_TOKEN` (skipped
when no credential is stored), and `account_id` as `CLOUDFLARE_ACCOUNT_ID` when set. Values are
a snapshot at container start; restart the session after logging in again or changing settings.

## Tests

Projection and login are covered by `tests/test_credentials_runtime.py`,
`tests/test_plugin_enabled.py` and `tests/test_cli_auth_login.py`.

See [docs/plugins.md](../../docs/plugins.md) for the manifest format, the plugin SDK and testing conventions.

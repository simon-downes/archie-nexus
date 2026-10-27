# Scalr plugin

Gives agent containers a Scalr API token and the Scalr hostname and account as environment
variables, for any CLI or script in the container that reads them. The plugin has no Python
code, no tools and no skill: it only declares the credential and two settings.

## Namespaces and functions

No namespaces, no tools and no skill.

## Credential

Static, one field `token`, mapped to `SCALR_TOKEN`. `archie auth login scalr` reads the token
from the `SCALR_TOKEN` environment variable on the host (there is no prompt or JSON stdin; it
fails naming the variable when it is unset):

```bash
SCALR_TOKEN=... archie auth login scalr
```

`set_env: true`: new containers get `SCALR_TOKEN`.

A stored entry from before hostname/account became settings (with `hostname`/`account`
fields) fails validation as unknown fields. Move the values to `plugins.scalr.settings` and
log in again.

## Configuration

```yaml
plugins:
  scalr:
    enabled: true
    settings:
      hostname: scalr.example.com   # default none -> SCALR_HOSTNAME
      account: account-name         # default none -> SCALR_ACCOUNT
```

| Key | Type | Default | Read | Meaning |
| --- | --- | --- | --- | --- |
| `settings.hostname` | str | none | next command | Exported as `SCALR_HOSTNAME` |
| `settings.account` | str | none | next command | Exported as `SCALR_ACCOUNT` |

No tool policy (no tools). `enabled: false` stops the token and both settings being exported
and hides login and status for the provider.

## How it works

`shared/src/archie_shared/credentials/runtime.py` (`runtime_environment()`) builds the
environment for each shell command and exec run: the stored `token` as `SCALR_TOKEN`
(skipped when no credential is stored), and each setting that declares `env` in the
manifest, when its value is set. The agent recomputes it when `config.yaml` or
`credentials.yaml` changes, so a new login or settings edit reaches running sessions at the
next command. `archie auth login scalr` prompts for the token when `SCALR_TOKEN` is unset,
and for `hostname` and `account` when they aren't in `config.yaml`. If two plugins claim the
same variable, the agent keeps the last good variables, or shows a warning if it has none.

## Tests

No `plugins/scalr/tests`. Projection and login are covered by
`tests/test_credentials_runtime.py`, `tests/test_plugin_enabled.py` and
`tests/test_cli_auth_login.py`. Run them with
`uv run pytest -q tests/test_credentials_runtime.py tests/test_plugin_enabled.py tests/test_cli_auth_login.py`.

See [docs/plugins.md](../../docs/plugins.md) for the manifest format, the plugin SDK and testing conventions.

# Cloudflare plugin

Gives agent containers a read-only Cloudflare API token and account id as environment
variables for Cloudflare's `cf` CLI (open beta, https://developers.cloudflare.com/cf/). The
`cloudflare` skill teaches the agent to inspect Workers, Queues, Workflows, Workers VPC, R2,
Workers KV, zones and domains, DNS, SSL and certificates, WAF, Turnstile and Tunnels. The
plugin has no Python code and no tools.

The token's permissions are the only permission layer. There is no wrapper or command
allowlist in front of `cf`: if the token grants a permission, the agent can use it. Keep the
token read-only. Resources are managed by Terraform, and the skill tells the agent not to
change anything.

## Namespaces and functions

No namespaces and no tools. The agent runs `cf` in the shell. The image installs
`cf@1.0.0-beta.21` (pinned in `Dockerfile`) and sets `CF_SEND_TELEMETRY=false`.

## Skill

`skills/cloudflare/SKILL.md` covers access (env vars, account id), the read-only rules (no
changes, never `--force`, denials are not worked around, no secret-value reads such as
`cf tunnels token get`, `cf kv keys get` or `cf r2 objects get`), a workflow (`cf auth whoami`,
`cf cli search`, `--help`, `cf schema`, `--dry-run`), read commands per area, and errors.

## Token permissions

Create a custom API token in the Cloudflare dashboard (My Profile → API Tokens) with only
*Read* permissions, scoped to the one account and the zones the agent should see. Names are
from Cloudflare's [API token permissions](https://developers.cloudflare.com/fundamentals/api/reference/permissions/)
reference.

| Area | Scope | Permission |
| --- | --- | --- |
| Account lookup (`cf accounts list`) | Account | Account Settings Read |
| Workers, Workflows | Account | Workers Scripts Read (the Workflows API accepts it) |
| Queues | Account | Queues Read |
| R2 | Account | Workers R2 Storage Read |
| Workers KV | Account | Workers KV Storage Read |
| Turnstile | Account | Turnstile Read |
| Tunnels | Account | Cloudflare Tunnel Read |
| Account rulesets and WAF | Account | Account Rulesets Read, Account WAF Read |
| Account certificates | Account | Account: SSL and Certificates Read |
| Workers VPC | Account | Unconfirmed: the docs name a `Connectivity Directory Read` role; no token group is listed |
| Registrar | Account | Unconfirmed: no token group is listed |
| Zones and domains | Zone | Zone Read, Zone Settings Read |
| DNS | Zone | DNS Read |
| SSL and certificates | Zone | SSL and Certificates Read |
| WAF and firewall rules | Zone | Zone WAF Read, Firewall Services Read |

Don't grant *Edit* permissions. Anything the token allows is available to the agent. With
Workers KV Storage Read and Workers R2 Storage Read the token can read values and object
bodies too; the skill tells the agent not to read them, but only the token enforces it.

## Credential

Static, one field `api_token`, mapped to `CLOUDFLARE_API_TOKEN`. `archie auth login cloudflare`
reads the token from the `CLOUDFLARE_API_TOKEN` environment variable on the host (there is no
prompt or JSON stdin; it fails naming the variable when it is unset):

```bash
CLOUDFLARE_API_TOKEN=... archie auth login cloudflare
```

Login uses `CLOUDFLARE_API_TOKEN` when it is set and prompts for the token otherwise. It does
not check the token's permissions. Shells used for Terraform or wrangler often hold an Edit
token in that variable, so set the variable to the dedicated read-only token on the command
line, as above. If `settings.account_id` is unset, login then prompts for it on the terminal.

`set_env: true`: agent shell commands and exec runs get `CLOUDFLARE_API_TOKEN`.

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
| `settings.account_id` | str | none | each command | Exported as `CLOUDFLARE_ACCOUNT_ID`; `cf` uses it for account-scoped commands |

No tool policy (no tools). `enabled: false` stops the token and the account id being exported
and hides login and status for the provider.

## How it works

`shared/src/archie_shared/credentials/runtime.py` (`runtime_environment()`) builds the
environment that the agent adds to each shell command and exec run: the stored `api_token` as `CLOUDFLARE_API_TOKEN` (skipped
when no credential is stored), and `account_id` as `CLOUDFLARE_ACCOUNT_ID` when set. The agent
re-reads `config.yaml` and `credentials.yaml` when either changes, so a new login or setting
applies to the next command without restarting the session.

## Tests

`plugins/cloudflare/tests/test_cloudflare_skill.py` checks the skill's area headings and
rules. It also runs each `cf …` command the skill names with `--help`, checking that the
command exists in the installed CLI. Those cases skip when `cf` is not on PATH. Re-run them
after changing the pinned version:

```bash
npm install --prefix /tmp/cfcli cf@<version>
PATH=/tmp/cfcli/node_modules/.bin:$PATH uv run pytest -q plugins/cloudflare
```

Projection and login are covered by `tests/test_credentials_runtime.py`,
`tests/test_plugin_enabled.py` and `tests/test_cli_auth_login.py`. No `conftest.py` or fakes:
there is no plugin code to fake.

See [docs/plugins.md](../../docs/plugins.md) for the manifest format, the plugin SDK and testing conventions.

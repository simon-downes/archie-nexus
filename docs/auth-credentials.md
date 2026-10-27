# Authentication and credentials

This document describes how Archie Nexus authenticates providers, stores credentials, exposes auth APIs, and makes credentials available to agent containers.

## Overview

Nexus has three cooperating parts:

- **CLI** — starts authentication, gathers static credentials, opens the OAuth browser flow, and displays redacted status.
- **Orchestrator** — owns the client-facing `/auth/*` HTTP API, OAuth state, discovery, token exchange, refresh, validation, and credential persistence.
- **Shared credential package** — defines provider schemas, typed credential records, provider metadata, API DTOs, and the YAML-backed store used by the orchestrator and agents.

The credential persistence boundary is the shared file:

```text
<ARCHIE_HOME_DIR>/credentials.yaml
```

By default:

```text
~/.nexus/credentials.yaml
```

`ARCHIE_HOME_DIR` changes this location for both the host-side orchestrator and agent containers.

## Supported providers

| Provider | Auth type | Credential fields | Login input |
| --- | --- | --- | --- |
| `notion` | OAuth | access/refresh token and OAuth metadata | Browser OAuth flow |
| `slack` | OAuth | access/refresh token and OAuth metadata | Browser OAuth flow |
| `google` | OAuth | access/refresh token and OAuth metadata | Browser OAuth flow |
| `github` | Static | `personal_token`, `work_token` | Prompt or JSON stdin |
| `aws` | Static | `access_key_id`, `secret_access_key`, `session_token` | AWS credential chain plus STS validation |
| `scalr` | Static | `token` | `SCALR_TOKEN`; hostname/account are settings (`plugins.scalr.settings`) |
| `cloudflare` | Static | `api_token` | `CLOUDFLARE_API_TOKEN`; account id is a setting (`plugins.cloudflare.settings.account_id`) |
| `jira` | Static | `email`, `token`, `cloud_id` | Prompt or JSON stdin |
| `bedrock` | Static | `aws_access_key_id`, `aws_secret_access_key`, `aws_session_token` | AWS credential chain plus STS validation |
| `datadog` | Static | `api_key`, `app_key` | Prompt or JSON stdin; site defaults to `app.datadoghq.eu` |
| `incident_io` | Static | `api_key` | Prompt or JSON stdin; API host is `https://api.incident.io/` |
| `openai` | Static | `api_key` | `OPENAI_API_KEY`; used by the `openai-*` models |

`bedrock` and `aws` are separate provider identities and separate store entries. Their fields are intentionally distinct even though both represent AWS access keys.

## Credential storage

The store is a YAML mapping keyed by provider name. Each provider contains a flat mapping of fields. A representative file has this shape:

```yaml
bedrock:
  aws_access_key_id: AKIA...
  aws_secret_access_key: ...
  aws_session_token: ...

github:
  personal_token: github_pat_...
  work_token: github_pat_...

scalr:
  token: ...

google:
  access_token: ...
  refresh_token: ...
  expires_at: 2026-09-03T12:00:00+00:00
  client_id: ...
```

The file is created with mode `0600`. Writes use a temporary file followed by an atomic replacement. Provider replacement, deletion, and merge updates use an advisory lock at:

```text
<ARCHIE_HOME_DIR>/credentials.yaml.lock
```

A provider replacement replaces that provider’s complete entry. Omitted fields are removed, so stale optional values are not retained. Updates preserve unrelated provider entries.

The store validates known provider entries against concrete `msgspec.Struct` types. Unknown fields are rejected. Invalid stored data is reported as provider status/error information rather than returned as a credential object.

Credential values are never returned by status or provider-description APIs. They should not be logged or included in command arguments.

## Provider metadata and configuration

Provider behavior is defined by the `credential` block of each plugin's `plugins/<id>/plugin.yaml`; `bedrock` is the one core provider defined in code. A provider descriptor identifies its auth type and required fields. OAuth descriptors additionally define:

- authorization and token endpoints, when fixed;
- a discovery `server_url`, when endpoints must be discovered;
- dynamic registration support;
- requested scopes;
- nested access-token and refresh-token paths;
- the `expires_in` path;
- extra authorization parameters.

For example:

- Notion discovers from `https://mcp.notion.com` and uses the RFC 9470/RFC 8414 metadata flow.
- Slack uses nested token paths such as `authed_user.access_token` and requests `user_scope`
  covering every `slack.*` tool: conversation read/history for all four conversation types,
  `users:read` + `users:read.email`, the granular `search:read.*` scopes, `reactions:read`,
  `reactions:write`, and `chat:write`. Slack grants are additive — re-running
  `archie auth login slack` adds newly requested scopes to the existing token.
- Google requests offline access with consent.

Overrides are configured under `plugins.<id>.auth` in `config.yaml`; the orchestrator re-reads them on each auth request, so edits apply without a restart (see [Configuration](config.md)). Google uses fixed authorization and token endpoints but does not support dynamic client registration, so `plugins.google.auth.client_id` is required before `archie auth login google` can start. Slack takes its `client_id` the same way:

```yaml
plugins:
  google:
    auth:
      # Required: OAuth client ID from the Google Cloud OAuth client.
      client_id: your-client-id.apps.googleusercontent.com
      scopes:
        - https://www.googleapis.com/auth/gmail.readonly
  slack:
    auth:
      client_id: "111.222"
```

To pin the callback origin explicitly, add:

```yaml
orchestrator:
  profiles:
    default:
      host: 127.0.0.1
      port: 7600
      public_url: http://127.0.0.1:7600
```

The exact Google redirect URI is then:

```text
http://127.0.0.1:7600/auth/callback/google
```

Supported override fields are OAuth `client_id`, `authorization_endpoint`, `token_endpoint`, `registration_endpoint`, `server_url`, `scopes`, `token_path`, `refresh_token_path`, `expires_in_path` and `extra_params`, plus `env` and `set_env` for static providers. A static provider rejects the OAuth fields and an OAuth provider rejects `env` and `set_env`. Bedrock has no config overrides. Client secrets are credential-store data, not public provider configuration.

Effective OAuth values are resolved in this order:

1. Code-defined provider defaults.
2. Explicit `config.yaml` overrides.
3. Persisted OAuth metadata, such as discovered endpoints and dynamically registered client IDs, where no explicit value already exists.

The running local CLI targets the configured default orchestrator profile. The current auth CLI does not expose remote profile selection.

## CLI commands

### `archie auth login <provider>`

This is the credential setup command for both OAuth and static providers.

#### OAuth providers

For `notion`, `slack`, and `google`:

1. The CLI calls `POST /auth/login/{provider}` on the local orchestrator.
2. The orchestrator resolves effective configuration and, if necessary, discovers OAuth endpoints.
3. If required, the orchestrator dynamically registers an OAuth client.
4. The orchestrator creates short-lived PKCE and state data in memory.
5. The CLI opens the returned authorization URL.
6. The provider redirects the browser to the orchestrator callback.
7. The orchestrator validates the flow, exchanges the authorization code, normalizes token fields, and persists the credential.
8. The CLI polls `GET /auth/flow/{flow_id}` until the flow succeeds or fails.

The browser callback is orchestrator-owned. The authorization code and PKCE verifier remain inside the orchestrator flow; they are not returned to the CLI.

Completed flows retain only redacted outcome information for a short retention period. Flow status does not expose the OAuth CSRF state or credential values.

#### Static providers

For `aws` and `bedrock`, the CLI uses boto3’s standard AWS credential chain. This supports environment credentials, AWS config files, profiles, SSO, and role-based resolution supported by boto3. The CLI validates the resolved credentials with STS before sending them to the orchestrator.

The field mapping differs by provider:

```text
bedrock → aws_access_key_id, aws_secret_access_key, aws_session_token
aws     → access_key_id, secret_access_key, session_token
```

For Scalr, the CLI reads `SCALR_TOKEN` into `token`, or prompts for it when the variable is unset. The non-secret hostname and account are plugin settings, projected into agent commands as `SCALR_HOSTNAME` and `SCALR_ACCOUNT`; login prompts for them when they are missing:

```yaml
plugins:
  scalr:
    settings:
      hostname: scalr.example.com
      account: account-name
```

A stored `scalr` entry that still has `hostname`/`account` fields (pre-settings shape) fails validation as unknown fields; move the values to `plugins.scalr.settings` and re-run `archie auth login scalr`.

For Cloudflare, the CLI reads `CLOUDFLARE_API_TOKEN` into `api_token`. Login stores the token it is given (from the variable or the prompt) without checking its permissions, so use a dedicated read-only token (`CLOUDFLARE_API_TOKEN=... archie auth login cloudflare`); its permissions are the only limit on what the agent's `cf` CLI can do (see `plugins/cloudflare/README.md`). The non-secret account id is a plugin setting, projected into agent commands as `CLOUDFLARE_ACCOUNT_ID`; login prompts for it if it is unset:

```yaml
plugins:
  cloudflare:
    settings:
      account_id: 0123456789abcdef0123456789abcdef
```

For other static providers, the CLI first uses each mapped environment variable that is set, such as `SCALR_TOKEN`. If fields are still missing, it reads a JSON object from stdin when stdin is a non-empty pipe; that object supplies all fields. Otherwise it prompts on the terminal for each missing field. Secret-like fields such as tokens, keys, and passwords are hidden while prompting. With no terminal, no stdin and missing fields, login fails and names the missing variables.

After the credential is saved, for both static and OAuth providers, the CLI checks the plugin's settings. For each scalar setting (`str`, `int`, `float`, `bool`) with no manifest default and no value in `config.yaml`, it prompts on the terminal; an empty answer skips the setting. Answers are written to `plugins.<id>.settings` in `config.yaml` with `ruamel.yaml`, which keeps comments and formatting. The edited file is validated before it replaces the original. Without a terminal, the CLI lists the missing setting keys instead. List and dict settings, such as `plugins.github.settings.work_orgs`, are never prompted for.

Datadog uses `api_key` and `app_key` as secrets. Its non-secret site is the plugin setting `plugins.datadog.settings.site`, default `app.datadoghq.eu`; Datadog API clients derive the API host (`https://api.datadoghq.eu` by default) from that site:

```yaml
plugins:
  datadog:
    settings:
      site: datadoghq.com
```

OpenAI uses one `api_key` secret, read from `OPENAI_API_KEY` on the host. It authenticates the direct OpenAI Responses backend for the `openai-*` catalog models; the agent reads the stored key and falls back to an `OPENAI_API_KEY` already in its environment. Unlike the `bedrock-*` models, these requests go to `https://api.openai.com/v1` and leave AWS, so prompts, tool results and images are subject to OpenAI's data handling rather than Bedrock's. `gpt-5.6-cyber` also needs Daybreak approval on the key's OpenAI organisation:

```bash
OPENAI_API_KEY=sk-... archie auth login openai
```

incident.io uses one `api_key` secret. The API uses the fixed host `https://api.incident.io/` and sends the key as a Bearer token. API keys can have account-level and team-scoped permissions; no additional authentication secret or account identifier is required.

Example using JSON stdin:

```bash
printf '{"email":"user@example.com","token":"...","cloud_id":"..."}' \
  | archie auth login jira
```

Example using interactive prompts:

```bash
archie auth login jira
```

The CLI sends static credentials to `PUT /auth/credential/{provider}`. The orchestrator performs strict structural decoding, validates required fields, replaces that provider entry atomically, and returns only redacted status.

### `archie auth refresh <provider>`

Refresh is for OAuth providers with stored refresh state:

```text
POST /auth/refresh/{provider}
```

The orchestrator loads the typed OAuth credential, calls the effective token endpoint, preserves a previous refresh token when the provider does not rotate it, stores a rotated token when one is returned, and calculates the new ISO-UTC expiry. Failed refreshes do not replace the previous credential.

Static providers and OAuth providers without refresh state return an actionable reauthentication error.

### `archie auth status`

Status calls:

```text
GET /auth/status
```

The response contains one redacted status per provider. Status includes:

- provider name;
- auth type;
- whether a usable credential is present;
- state such as `missing`, `configured`, `valid`, `expired`, or `needs_reauthentication`;
- expiry time where applicable;
- a non-secret validation error where needed.

No access tokens, refresh tokens, API keys, secret keys, or client secrets are displayed.

## Orchestrator HTTP API

### `GET /auth/providers`

Returns effective, secret-free provider descriptors. Static entries expose required fields. OAuth entries expose endpoint metadata, scopes, token paths, extra authorization parameters, effective non-secret client ID, and the selected callback URI where applicable.

### `GET /auth/status`

Returns redacted status for every registered provider.

### `PUT /auth/credential/{provider}`

Replaces one static provider entry. The request body is JSON matching that provider’s concrete credential struct. Unknown fields, malformed JSON, OAuth-provider submissions, missing required fields, and unknown providers are rejected without changing the existing entry.

### `DELETE /auth/credential/{provider}`

Deletes one provider entry. Deleting a missing entry is idempotent. The response is redacted status.

### `POST /auth/login/{provider}`

Starts an OAuth flow and returns:

```json
{
  "flow_id": "...",
  "authorization_url": "https://...",
  "redirect_uri": "http://127.0.0.1:7600/auth/callback/google"
}
```

### `GET /auth/callback/{provider}`

Consumes the browser callback. It validates provider, state, flow expiry, one-time-use status, and callback URI before exchanging the code. The browser receives a non-secret success or failure page.

### `GET /auth/flow/{flow_id}`

Returns redacted flow state and outcome while the flow is retained. It does not expose authorization codes, PKCE verifiers, CSRF state, tokens, or provider error payloads.

### `POST /auth/refresh/{provider}`

Performs non-interactive OAuth refresh and returns redacted credential status.

## Agent container access

The orchestrator mounts the host Nexus home directory into each agent container:

```text
host <ARCHIE_HOME_DIR> → /home/archie/.nexus
```

The agent therefore sees the same credentials file at its container-side `ARCHIE_HOME_DIR` and can use the shared credential APIs. Bedrock integrations explicitly read the typed `bedrock` entry and fall back to the normal AWS chain when appropriate.

Credential updates do not send a live control-plane event to running agents. A running process may already have loaded credentials. New containers see the latest persisted file. The agent rebuilds the projected environment for each new shell command and exec run (see [Command environment projection](#command-environment-projection)), so a new login reaches running sessions at the next command. The `gh` wrapper (see [GitHub CLI token selection](#github-cli-token-selection)) and the AWS `credential_process` helper (see [AWS credential refresh](#aws-credential-refresh)) also read the store on every call. A process that is already running, such as the agent's own LLM client or a long-lived server started from a shell, keeps the values it loaded.

### Command environment projection

Static provider descriptors may define an `env` mapping from credential fields to process environment variables and a `set_env` flag. The mapping is used in both directions:

- `archie auth login` reads configured environment variables into credential fields for providers that define them;
- the agent reads stored credential fields when it starts each shell command or exec run, and exports them only when `set_env: true`.

Current mappings are:

| Provider | Mapping | `set_env` |
| --- | --- | --- |
| `bedrock` | `aws_access_key_id` → `AWS_ACCESS_KEY_ID`, `aws_secret_access_key` → `AWS_SECRET_ACCESS_KEY`, `aws_session_token` → `AWS_SESSION_TOKEN` | `false` |
| `aws` | `access_key_id` → `AWS_ACCESS_KEY_ID`, `secret_access_key` → `AWS_SECRET_ACCESS_KEY`, `session_token` → `AWS_SESSION_TOKEN` | `false` |
| `scalr` | `token` → `SCALR_TOKEN` | `true` |
| `cloudflare` | `api_token` → `CLOUDFLARE_API_TOKEN` | `true` |
| `openai` | `api_key` → `OPENAI_API_KEY` | `false` |

The `aws` mapping is unused by default: containers get AWS access through the generated profiles, which read the store on every call (see [AWS credential refresh](#aws-credential-refresh)). Setting `plugins.aws.auth.set_env: true` projects the base credentials again. Profile calls ignore them, but calls without a profile would use them.

Plugin settings fields that declare `env` in their manifest are projected too, when the resolved value is set: `plugins.scalr.settings.hostname` → `SCALR_HOSTNAME`, `plugins.scalr.settings.account` → `SCALR_ACCOUNT`, `plugins.cloudflare.settings.account_id` → `CLOUDFLARE_ACCOUNT_ID`. Non-scalar settings cannot be projected: a manifest that puts `env` on a non-scalar or nested field is rejected at load. A plugin with `enabled: false` projects neither credentials nor settings.

Only explicitly enabled providers are projected. Missing providers are skipped. Environment values are never logged.

The agent computes the variables from the current `config.yaml` and `credentials.yaml` and caches them until either file's modification time or size changes. `grep`, `glob` and `code` subprocesses don't get them. If the variables can't be computed, for example because `config.yaml` fails validation, a store entry is invalid, or a mapping names an unknown credential field, an invalid environment name or a variable that two owners claim, the agent keeps using the last variables it computed successfully, as the live tool policy keeps its last valid snapshot. If it has none yet, the command runs without them and its stderr starts with an `[archie warning: …]` line. For errors outside `config.yaml` the warning gives only the exception type, so it never quotes the store.

The orchestrator no longer passes these variables to `docker run`. Environment variables are inherited by processes inside the container and may be visible to processes with access to the container, so mappings should remain limited to tools that need them.

### GitHub CLI token selection

GitHub credentials are not projected into the container environment. The image installs the real GitHub CLI as `gh-base` and puts a wrapper at `/usr/local/bin/gh` (source: `container/bin/gh`). On each call the wrapper:

1. Finds the target owner, in this order: a `gh api` endpoint (path or full URL) starting `repos/`, `orgs/` or `users/<owner>`, the repository argument of `gh repo <subcommand>`, or the owner argument of `gh repo list`; then `--owner`; then `-R`/`--repo`; then `GH_REPO`; then the GitHub remote gh treats as the base repository (the one `gh repo set-default` marked, then `upstream`, `github`, `origin`, then the first). Remotes on other hosts are ignored.
2. Reads `plugins.github.settings.work_orgs` from `config.yaml` with `yq`. If the owner matches an entry exactly, ignoring case, it uses `github.work_token`. Otherwise it uses `github.personal_token`.
3. Reads that one token from `credentials.yaml` with `yq` and runs `gh-base` with all arguments unchanged and `GH_TOKEN` set only in that process.

```yaml
plugins:
  github:
    settings:
      work_orgs: [tillo]
```

Both files are read on every call, so changes apply to running sessions. If the caller already set `GH_TOKEN` or `GITHUB_TOKEN`, the wrapper leaves the environment alone. If the selected token is missing or not a string, or a file cannot be parsed, the wrapper prints a warning to stderr and runs `gh-base` without a token; gh then falls back to its own stored login, if one exists in the container.

Owner detection uses argument patterns rather than gh's flag definitions. A `gh repo` argument that directly follows another flag is skipped, because it may be that flag's value (`gh repo view -b feature/x`). The cost is that `gh repo view --web o/r` uses the remote's token. A command that names its repository only through a URL argument (for example `gh pr view https://github.com/o/r/pull/1`) also uses the token for the current directory's remote. Pass `-R owner/repo` in those cases.

`work_orgs` is accepted only under `plugins.github.settings`.

Upgrading from the single `github.token` field: the old entry fails validation and `archie auth status` reports it as needing reauthentication. Rerun `archie auth login github`. Also remove any `plugins.github.auth.env` override from `config.yaml`; mapping the removed `token` field stops containers from starting.

### AWS credential refresh

The `aws` credential is not projected into the container environment. When `plugins.aws.settings.profiles` is set, the AWS plugin's startup hook writes `~/.aws/config` with a base profile, `archie-base`, and one role profile per entry:

```ini
[profile archie-base]
region = eu-west-1
credential_process = /opt/archie/plugins/aws/bin/credential-process

[profile staging]
region = eu-west-1
role_arn = arn:aws:iam::111111111111:role/agent-access
source_profile = archie-base
```

`credential-process` reads the `aws` entry from `credentials.yaml` with `yq` each time it runs and prints it as `credential_process` JSON. It writes nothing and keeps nothing in the environment. To renew expired credentials, run `aws-vault exec <profile> -- archie auth login aws` (or another login that refreshes the AWS chain) on the host; running sessions pick up the new values without a restart:

- Role credentials are reused until about 15 minutes before they expire. The AWS CLI caches them under `~/.aws/cli/cache` across calls; an SDK session keeps them in memory. When they are due for renewal, the client gets base credentials from the helper, which reads the store, and assumes the role again. A new login therefore takes effect at the next role renewal (within an hour with the default role session length), and immediately for a profile with no cached role credentials.
- The helper reports an `Expiration` 20 minutes ahead, because the store does not record the real one. Clients treat credentials within 15 minutes of `Expiration` as due for refresh, so a long-running SDK session never holds base credentials for more than about 5 minutes before it runs the helper again. Without an `Expiration`, SDK sessions would keep the first base credentials forever.

If the store has no usable `aws` entry, the helper exits 1 with a message naming `archie auth login aws`, and the AWS CLI reports that error.

## Security model

The auth API follows the current trusted local deployment model:

- There is no client-to-orchestrator authentication or authorization layer.
- The orchestrator API is intended for the trusted local host/network boundary.
- Credential values are accepted over the local HTTP API and persisted in the shared store.
- Static credential responses and OAuth flow responses are redacted.
- OAuth state is unpredictable, short-lived, one-use, and kept in orchestrator memory.
- Callback access-log records are suppressed so authorization query parameters are not written by the normal Uvicorn access logger.
- Unexpected application errors return a generic 500 response and are logged without exception message text.
- Docker command arguments are not logged because they may contain mounted paths or future secret-bearing environment values.

## Common workflows

### Configure GitHub

```bash
archie auth login github   # prompts for personal_token and work_token (input hidden)
archie auth status
```

### Configure AWS tools

```bash
aws sso login
archie auth login aws
archie auth status
```

Run the same login again when the credentials expire; running sessions pick up the new values (see [AWS credential refresh](#aws-credential-refresh)).

### Configure Bedrock

```bash
aws sso login
archie auth login bedrock
```

Bedrock and general AWS credentials are persisted under different provider names and are not conflated.

### Authenticate with Google

```bash
archie auth login google
archie auth status
```

The browser flow is handled by the orchestrator and the resulting tokens are written to the shared credential store.

### Configure Jira from a pipe

```bash
printf '%s' '{"email":"user@example.com","token":"...","cloud_id":"..."}' \
  | archie auth login jira
```

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
| `linear` | Static | `token` | Prompt or JSON stdin |
| `github` | Static | `token` | `GH_TOKEN` |
| `aws` | Static | `access_key_id`, `secret_access_key`, `session_token` | AWS credential chain plus STS validation |
| `scalr` | Static | `token`, `hostname`, `account` | `SCALR_TOKEN`, `SCALR_HOSTNAME`, `SCALR_ACCOUNT` |
| `jira` | Static | `email`, `token`, `cloud_id` | Prompt or JSON stdin |
| `bedrock` | Static | `aws_access_key_id`, `aws_secret_access_key`, `aws_session_token` | AWS credential chain plus STS validation |

`bedrock` and `aws` are separate provider identities and separate store entries. Their fields are intentionally distinct even though both represent AWS access keys.

## Credential storage

The store is a YAML mapping keyed by provider name. Each provider contains a flat mapping of fields. A representative file has this shape:

```yaml
bedrock:
  aws_access_key_id: AKIA...
  aws_secret_access_key: ...
  aws_session_token: ...

github:
  token: ...

scalr:
  token: ...
  hostname: scalr.example.com
  account: account-name

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

Provider behavior is defined in the shared provider registry. A provider descriptor identifies its auth type and required fields. OAuth descriptors additionally define:

- authorization and token endpoints, when fixed;
- a discovery `server_url`, when endpoints must be discovered;
- dynamic registration support;
- requested scopes;
- nested access-token and refresh-token paths;
- the `expires_in` path;
- extra authorization parameters.

For example:

- Notion discovers from `https://mcp.notion.com` and uses the RFC 9470/RFC 8414 metadata flow.
- Slack uses nested token paths such as `authed_user.access_token` and requests `user_scope`.
- Google requests offline access with consent.

OAuth overrides are configured globally for the running orchestrator under `auth.providers` in `config.yaml`:

```yaml
auth:
  providers:
    google:
      client_id: your-client-id
      authorization_endpoint: https://accounts.google.com/o/oauth2/v2/auth
      token_endpoint: https://oauth2.googleapis.com/token
      scopes:
        - https://www.googleapis.com/auth/gmail.readonly
```

Supported override fields include OAuth client ID, endpoints, discovery server URL, scopes, token paths, expiry path, and authorization parameters. Client secrets are credential-store data, not public provider configuration.

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

For GitHub, the CLI reads:

```text
GH_TOKEN
```

and sends it as the provider’s `token` field.

For Scalr, the CLI reads:

```text
SCALR_HOSTNAME
SCALR_TOKEN
SCALR_ACCOUNT
```

and maps them to `hostname`, `token`, and `account`.

For other static providers, the CLI accepts a JSON object on stdin. If stdin is not a non-empty pipe, it prompts interactively for the provider’s declared fields. Secret-like fields such as tokens, keys, and passwords are hidden while prompting.

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

Credential updates do not send a live control-plane event to running agents. A running process may already have loaded credentials. New containers see the latest persisted file; restarting a session is the way to ensure a newly started process reloads changed credentials.

### Startup environment projection

Static provider descriptors may define an `env` mapping from credential fields to process environment variables and a `set_env` flag. The mapping is used in both directions:

- `archie auth login` reads configured environment variables into credential fields for providers that define them;
- the orchestrator reads stored credential fields at container startup and exports them only when `set_env: true`.

Current mappings are:

| Provider | Mapping | `set_env` |
| --- | --- | --- |
| `bedrock` | `aws_access_key_id` → `AWS_ACCESS_KEY_ID`, `aws_secret_access_key` → `AWS_SECRET_ACCESS_KEY`, `aws_session_token` → `AWS_SESSION_TOKEN` | `false` |
| `aws` | `access_key_id` → `AWS_ACCESS_KEY_ID`, `secret_access_key` → `AWS_SECRET_ACCESS_KEY`, `session_token` → `AWS_SESSION_TOKEN` | `true` |
| `github` | `token` → `GH_TOKEN` | `true` |
| `scalr` | `token` → `SCALR_TOKEN`, `hostname` → `SCALR_HOSTNAME`, `account` → `SCALR_ACCOUNT` | `true` |

This preserves the distinction between Bedrock and general AWS credentials: Bedrock integrations read the typed `bedrock` entry, while the general `aws` entry supplies standard AWS environment variables for shell commands, the AWS CLI, and `exec`.

Only explicitly enabled providers are projected. Missing providers are skipped, and mapping errors such as unknown credential fields, invalid environment names, or duplicate environment ownership fail container startup before Docker is invoked. Environment values are never logged.

Projection is a startup snapshot. New containers use the latest stored credentials; an already-running agent keeps its original environment and must be restarted to receive changed values. Environment variables are inherited by processes inside the container and may be visible to processes with access to the container, so mappings should remain limited to tools that need them.

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
export GH_TOKEN=ghp_...
archie auth login github
archie auth status
```

### Configure AWS tools

```bash
aws sso login
archie auth login aws
archie auth status
```

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

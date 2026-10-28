# Configuration

Nexus reads configuration from `~/.nexus/config.yaml`. Set `ARCHIE_HOME_DIR` to use a
 different Nexus home directory. An absent config file uses the defaults described by the
 application schema.

## Application settings

```yaml
global:
  model: bedrock-openai-gpt-5-6-luna
  region: eu-west-1
  workspace_root: ~/dev
cli: {}
web: {}
agent:
  subagents:
    max_concurrent: 5
  turn_cost_limit: 50.0
  compaction:
    model: bedrock-openai-gpt-6-luna
    percent: 80
    cold_cost_threshold: 0.5
orchestrator:
  profiles:
    default:
      host: 127.0.0.1
      port: 7600
plugins: {}
```

The agent reads each section when a session starts, except `plugins.<id>.policy`, which it re-reads before each exec call (see below). `cli` and `web` are currently reserved for future settings.

The orchestrator re-reads `config.yaml` when it uses it, so most edits apply without
restarting it:

- Starting a session reads the file as it is at that moment: `mounts` and
  `workspace_root`. An invalid file makes `POST /sessions` fail with the validation
  error; no session starts with a stale config.
- Plugin `settings` that declare `env` (Scalr's `SCALR_HOSTNAME`) and credential
  environment variables are not part of session start. The agent reads them from the
  current file for each shell command and exec run, and keeps the last good values if
  the file becomes invalid (see
  [Command environment projection](auth-credentials.md#command-environment-projection)).
- Authentication (`archie auth ...`, the auth web page and the background OAuth refresh)
  reads `plugins.<id>.auth` overrides, `plugins.<id>.enabled` and the `default`
  profile's `public_url` on each use. If the file becomes invalid, these keep using the
  last valid config and the orchestrator logs one warning per invalid version.
- `orchestrator.profiles` host and port are read once by `archie serve` when it binds;
  changing them needs an orchestrator restart.

The file is parsed again only when its modification time, size or inode changes.

## Plugins

The `plugins` section configures each plugin under `plugins/<id>/` by its id. Every key is
optional, and an unknown plugin id or field is a load error:

- `enabled` (default `true`): `false` hides the plugin everywhere: its namespaces, skills,
  credential login and refresh, environment variables and startup hook.
- `policy`: overrides the `read` and `write` blocks of the plugin's tool policy. Each block
  takes `enabled` and, where the plugin supports one, a `scope`.
- `auth`: overrides for the plugin's credential provider, such as an OAuth `client_id`. See
  [Authentication and credentials](auth-credentials.md).
- `settings`: values the plugin's code reads, declared in its `plugin.yaml`.

The manifest keys behind each of these are described in [Plugins](plugins.md).

The agent re-reads `policy` before each exec call, so policy edits apply to running
sessions; if the file is invalid, the last valid policy stays in force and the TUI shows a
warning. `enabled`, `auth` and `settings` are read when a session starts, by both the
orchestrator (container environment) and the agent (tools and skills), so a new session
picks up edits without an orchestrator restart. Setting
`enabled: false` in a running session also turns both policy blocks off from the next exec
call; the rest takes effect in the next session.

```yaml
plugins:
  datadog:
    settings:
      site: datadoghq.com      # default app.datadoghq.eu
  github:
    settings:
      work_orgs: [tillo]       # owners whose repos use github.work_token
  slack:
    auth:
      client_id: "111.222"
    policy:
      read: {enabled: true, scope: {deny: [C0123456789, "#hr-private"]}}
  google:
    auth:
      client_id: your-client-id.apps.googleusercontent.com
  jira:
    policy:
      write: {enabled: true, scope: [OPS, PLAT]}
  notion:
    policy:
      write:
        enabled: true
        scope: {pages: [page-id], databases: [database-id]}
```

Per-plugin notes (each plugin's full settings, policy and scope rules are in its
`plugins/<id>/README.md`; see the [index](../plugins/README.md)):

- `jira` takes a list of project keys as its scope.
- `notion` takes `pages` and `databases` lists of root IDs as its scope; a resource is in
  scope when it is, or sits under, one of them.
- `slack` takes a `deny` list of conversation IDs or `#channel` names as its scope.
- `incident_io` accepts only `policy.read.enabled` (default `true`): the incident.io tools
  are read-only and do not support a scope.
- `datadog` takes `policy.read.enabled` (default `true`) and `policy.write.enabled`
  (default `false`, needed to create or update monitors tagged `managedby:archie`); a
  `scope` is not supported.
- `google` takes `policy.read.enabled` (default `true`) and `policy.write.enabled`
  (default `false`, needed for mail label/archive changes); a `scope` is not supported.
  `settings.timezone` (default `Europe/London`) sets the day boundaries for
  `calendar.today()` and `calendar.upcoming()`; an unknown zone falls back to the default.
- `cloudflare` has no tool policy. `settings.account_id` (default none) is exported into new
  containers as `CLOUDFLARE_ACCOUNT_ID` for the `cf` CLI.

## Migrating an old config

Older configs used a `tools:` section for tool policy and `auth.providers` for provider
overrides. Nexus no longer loads a file that has either section; the error names
`archie config migrate`, which rewrites the file in place:

```bash
archie config migrate            # <ARCHIE_HOME_DIR>/config.yaml
archie config migrate --path ./config.yaml
```

It moves `tools.<id>` to `plugins.<id>.policy`, `auth.providers.github.work_orgs` to
`plugins.github.settings.work_orgs`, `auth.providers.datadog.site` to
`plugins.datadog.settings.site`, and every other `auth.providers.<id>.<field>` to
`plugins.<id>.auth.<field>`. `linear` entries are dropped with a note. An existing
`plugins` section is merged; the command exits 1 naming the key if a value conflicts, or if
a key has no new location (for example `auth.providers.bedrock`).

The result is validated before anything is written. The original is copied to
`config.yaml.bak-<UTC timestamp>` and the file is replaced atomically with the same mode.
Comments are not kept; the backup has them. Running it again prints "already migrated" and
changes nothing.

To deploy this change: running containers bind-mount the host `agent/` directory, so stop
all sessions and the orchestrator **before** merging or pulling. Then run
`archie config migrate`, then `archie serve`.

## Kiro backend

The `kiro` section configures sessions started with `archie start --kiro` (see
[kiro.md](kiro.md)). These are the defaults:

```yaml
kiro:
  default_model: gpt-5.6-luna
  models: [claude-opus-5.5, claude-sonnet-5.5, claude-haiku-5.5,
           gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna]
  effort: null          # none | low | medium | high | xhigh | max; null = model default
  credits:
    included: 2000      # credits in the subscription per period
    resets_on: 1        # day of month the period starts (00:00 UTC; clamped to month length)
    rate_usd: 0.02      # USD per included credit
    overage_rate_usd: 0.04
```

`models` is the allowlist of Kiro model ids the session may switch to; `default_model`
must be one of them. Archie model keys for these are `kiro/<id>`. Each Kiro turn's credits are
priced at `rate_usd` until the period's total reaches `included`, and at `overage_rate_usd`
after that. The agent reads this section when a Kiro session starts.

## Host mounts

Use the top-level `mounts` list to add host bind mounts to every agent container. Each
entry is either a source string or a two-item source/destination pair:

```yaml
mounts:
  # Source-only entries use the corresponding path under /home/archie.
  - ~/.gitconfig

  # A destination can include :ro or :rw. The default is :rw.
  - [~/.gitconfig.local, ~/.gitconfig.local:ro]

  # Wildcards expand into separate mounts.
  - [~/.ssh/id_ed25519*, ~/.ssh:ro]
```

The source path is resolved on the host running the orchestrator. `~` means that host
user's home directory. Absolute sources outside the home directory keep the same path in
the container; for example, `/etc/hostname` maps to `/etc/hostname`. A source wildcard
must be in the final path component, is expanded non-recursively, and matches are sorted
lexically. Recursive `**` patterns are rejected. For example:

```yaml
mounts:
  - ~/.gitconfig*
  - [~/.ssh/id_ed25519*, ~/.ssh:ro]
```

If the host contains `.gitconfig`, `.gitconfig.local`, and `.gitconfig.work`, the first
entry creates three separate mounts. If the SSH directory contains `id_ed25519` and
`id_ed25519.pub`, the second entry creates:

```text
~/.ssh/id_ed25519     -> /home/archie/.ssh/id_ed25519:ro
~/.ssh/id_ed25519.pub -> /home/archie/.ssh/id_ed25519.pub:ro
```

For a wildcard source with an explicit destination, the destination is treated as a
container directory and the matched filename is appended. A literal source with an
explicit destination uses that destination exactly. Source-only wildcard entries use
the source pattern's parent directory and append each matched filename.

Only `:ro` and `:rw` are supported. Missing literal sources and wildcard patterns with no
matches are skipped, so optional files do not prevent a session from starting. Relative
container destinations, empty destinations, destinations under fixed mounts such as
`/workspace` and `/home/archie/.nexus`, invalid mount entries, and invalid modes are
rejected before Docker starts the session.

### Security warning

Mounts expose host content to the agent container. SSH private keys, Git credentials,
configuration files, and other mounted files may be readable or writable by agent code.
Mount sensitive files with `:ro`, and configure only paths you trust the session to access.
The orchestrator does not copy these files into the image; it passes the configured bind
mounts directly to Docker.

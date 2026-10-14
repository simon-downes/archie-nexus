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
    max_concurrent: 3
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
auth:
  providers: {}
tools: {}
```

See [Authentication and credentials](auth-credentials.md) for `auth` provider settings and
credential storage. The `tools` section controls provider tool policy. `cli` and `web` are
currently reserved for future settings.

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

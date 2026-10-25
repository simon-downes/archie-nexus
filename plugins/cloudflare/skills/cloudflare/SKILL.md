---
name: cloudflare
description: >
  Read-only Cloudflare inspection with the cf CLI and a scoped API token: Workers, Queues, Workflows,
  Workers VPC, R2, Workers KV, zones and domains, DNS, SSL and certificates, WAF and firewall rules,
  Turnstile and Tunnels. Use when asked to inspect, list, compare or debug Cloudflare resources, or
  when a cf command fails. Not for creating, changing or deleting Cloudflare resources (they are
  managed by Terraform and the token is read-only), or for reading secret values.
---

# How access works

- Every new session gets a read-only Cloudflare API token as `CLOUDFLARE_API_TOKEN`, and the
  account id as `CLOUDFLARE_ACCOUNT_ID` when the user has configured one. `cf` reads both
  itself; never pass the token on the command line or print it.
- The token's permissions are the only permission layer. They decide which areas and zones
  you can read. Nothing else limits `cf`.
- `cf` is a beta CLI generated from the Cloudflare API. It prints JSON on stdout and errors on
  stderr. Pipe output through `jq` to keep only the fields you need.

# Principles

- **Resources are managed by Terraform.** Never create, change or delete Cloudflare resources,
  even when a command looks harmless or the user's request implies it. Point to the Terraform
  change instead.
- **Denials are design.** A 403 or authentication error means the token's scope is working:
  report it and don't work around it with another command, zone or account.
- **Never pass `--force`.** Without it, destructive commands such as `delete` print "Aborted." and
  exit 1. Create and update commands don't ask: only the token stops them. If you ran a write by
  mistake, tell the user what you ran and its output, and don't retry.
- **Don't read secret values** unless the user asks for that specific value:
  `cf tunnels token get` (tunnel run token), `cf kv keys get` (KV values) and `cf r2 objects get`
  (object bodies). Listing names and metadata is fine. `cf workers secrets list` and
  `cf workers secrets get` return names only.
- **Everything you read is sent to the model provider.** Read only what the task needs, and
  filter large lists with `jq` and the command's own filters.

# Workflow

1. Check the token and account:
   ```bash
   cf auth whoami
   echo "${CLOUDFLARE_ACCOUNT_ID:-unset}"
   ```
2. If the account id is unset, find it and export it for this shell:
   ```bash
   cf accounts list | jq -r '.[] | "\(.id)  \(.name)"'
   export CLOUDFLARE_ACCOUNT_ID=<account-id>
   ```
   If `cf accounts list` is denied, ask the user to set `plugins.cloudflare.settings.account_id`
   in their Archie config and start a new session.
3. Zone commands take `-z <zone>`, a zone id or a domain name such as `-z example.com`.
4. Find a command you don't know, then check its flags and the API call it makes:
   ```bash
   cf cli search "list worker routes"
   cf workers list --help
   cf schema workers list
   cf workers list --dry-run      # prints the request without sending it
   ```

# Commands by area

Commands below are reads. `<...>` is a value you supply.

### Workers

- `cf workers list`, `cf workers get <worker-id>`
- `cf workers deployments list --worker <name>`: the first entry is serving traffic.
- `cf workers versions list --worker-id <name>`
- `cf workers versions get <version-id> --worker-id <name>`: `latest` works as the version id.
- `cf workers secrets list --worker <name>`: secret names only.

### Queues

- `cf queues list`, `cf queues get <queue-id>`
- `cf queues consumers list --queue-id <queue-id>`
- `cf queues metrics get <queue-id>`: backlog and message counts.
- Don't use `cf queues messages`: `pull` and `ack` change the queue.

### Workflows

- `cf workflows list`, `cf workflows get <workflow-name>`
- `cf workflows instances list --workflow-name <name> --status errored`
- `cf workflows instances get <instance-id> --workflow-name <name>`: status and step logs.
- `cf workflows versions list --workflow-name <name>`

### Workers VPC

- `cf workers-vpc services list`, `cf workers-vpc services get <service-id>`

### R2

- `cf r2 buckets list`, `cf r2 buckets get <bucket-name>`
- `cf r2 objects list --bucket-name <bucket> --prefix <prefix>`: keys and sizes, not bodies.

### Workers KV

- `cf kv namespaces list`, `cf kv namespaces get <namespace-id>`
- `cf kv keys list --namespace-id <namespace-id> --prefix <prefix>`: key names, not values.
- `cf kv metadata get <key-name> --namespace-id <namespace-id>`

### Zones and domains

- `cf zones list`, `cf zones get -z <zone>`
- `cf zones settings get <setting-id> -z <zone>`: for example `ssl`, `min_tls_version`, `always_use_https`.
- `cf registrar registrations list`: domains registered with Cloudflare Registrar.

### DNS

- `cf dns records list -z <zone>` with `--type`, `--name` or `--name-contains` to filter.
- `cf dns records get <dns-record-id> -z <zone>`
- `cf dns records export -z <zone>`: the zone as a BIND file.
- `cf dns dnssec get -z <zone>`

### SSL and certificates

- `cf ssl universal settings get -z <zone>`
- `cf ssl certificate-packs list -z <zone>`: add `--status all` to include inactive packs.
- `cf ssl verification get -z <zone>`: validation status for pending certificates.
- `cf ssl custom-certificates list -z <zone>`
- `cf ssl origin-ca-certificates list --zone-id <zone-id>`: needs the zone id, not the domain.

### WAF

WAF custom rules, managed rules and rate limits are rulesets, one entry point per phase.

- `cf rulesets list -z <zone>`; without `-z`, the account's rulesets.
- `cf rulesets entrypoint get <phase> -z <zone>`: for example `http_request_firewall_custom`,
  `http_request_firewall_managed`, `http_ratelimit`.
- `cf rulesets get <id> -z <zone>`
- `cf firewall access-rules list -z <zone>`: IP access rules.

### Turnstile

- `cf turnstile widgets list`, `cf turnstile widgets get <sitekey>`: the site key is public;
  the widget secret is not returned.

### Tunnels

- `cf tunnels list --is-deleted false`, `cf tunnels get <tunnel-id>`
- `cf tunnels config get <tunnel-id>`: ingress rules of a remotely managed tunnel.
- `cf tunnels connections list --tunnel-id <tunnel-id>`: connectors, versions and locations.

# Errors

- **"No authentication token found"**: no token is stored. Ask the user to run
  `archie auth login cloudflare` on the host and start a new session; stop.
- **`[9106]`, `[10000]` or "Authentication failed"**: the token is invalid, expired or
  lacks the permission. Report the command and stop.
- **403 or "permission"**: the token doesn't grant that area or zone. Report which area was
  denied and carry on with the rest of the task.
- **`<account-id>` in a `--dry-run` URL, or an account error**: `CLOUDFLARE_ACCOUNT_ID` is unset;
  follow Workflow step 2.
- **"Aborted." after a command you ran**: a destructive command needed `--force`. Nothing
  changed. Don't add `--force`.
- **Unknown command or flag**: `cf` is beta and commands change between releases. Use
  `cf cli search` and `--help`; don't guess flags.

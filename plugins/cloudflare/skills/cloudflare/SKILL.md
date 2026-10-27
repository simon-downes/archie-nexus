---
name: cloudflare
description: >
  Read-only Cloudflare inspection with the cf CLI: Workers, Queues, Workflows, Workers VPC, R2,
  Workers KV, zones and domains, DNS, SSL and certificates, WAF and firewall rules, Turnstile and
  Tunnels. Use when asked to inspect, list, compare or debug Cloudflare resources, or when a cf
  command fails: "check the DNS for", "why is this worker failing", "what WAF rules does this zone
  have". Not for creating, changing or deleting Cloudflare resources (they are managed by
  Terraform), or for reading secret values.
---

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
  `cf tunnels token get` (tunnel run token), `cf kv keys get` (KV values), `cf r2 objects get`
  (object bodies) and `cf turnstile widgets get` (includes the widget secret). Listing names and
  metadata is fine. `cf workers secrets list` and
  `cf workers secrets get` return names only.

# Using cf

- **Find the command, don't guess it.** `cf` is a beta CLI generated from the Cloudflare API, and
  names don't always match the dashboard. Search by intent, then read the flags:
  ```bash
  cf cli search "get tunnel configuration"     # ranked JSON list; starting with a verb works best
  cf tunnels config get --help
  cf schema tunnels config get                  # the API operation, path and query parameters
  cf tunnels config get <tunnel-id> --dry-run   # the request it would send, without sending it
  ```
  Search results include write commands; check the verb before you run one.
- **Output is JSON.** Results go to stdout and errors to stderr. For list commands, stdout is the
  bare `result` array. Use `jq` to keep only the fields you need.
- **Lists return one page, silently.** `cf` drops `result_info` from the output, so nothing says
  whether more pages exist. If the array is as long as the page size, assume it is incomplete:
  - Page-numbered lists (`--per-page`, `--page`): fetch the next page.
  - Cursor-paged lists (`cf kv keys list`, `cf r2 objects list`, `cf workflows instances list`):
    the cursor never reaches you. Raise `--limit` or `--per-page`, or narrow with `--prefix` or
    `--status`. `cf r2 objects list` can continue with `--start-after <last key>`.
  If you still can't get the whole list, tell the user the result may be incomplete.
- **Filter on the server.** Many list commands take filter flags; check `--help` before
  filtering a full list with `jq`. For example, `cf dns records list` takes `--type`, `--name`,
  `--name-contains`, `--content-contains`, `--proxied` and `--search`.
- **Zones take `-z <zone>`**, a zone id or a domain name such as `-z example.com`.

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
- `cf workflows instances get <instance-id> --workflow-name <name>`: status and step logs. Add
  `--simple` for metadata only, or `--order desc` for the latest steps first.
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

- `cf dns records list -z <zone>`, filtered with the flags under *Using cf*.
- `cf dns records get <dns-record-id> -z <zone>`
- `cf dns records export -z <zone>`: the zone as a BIND file.
- `cf dns dnssec get -z <zone>`

### SSL and certificates

- `cf ssl universal settings get -z <zone>`
- `cf ssl certificate-packs list -z <zone>`: add `--status all` to include inactive packs.
- `cf ssl verification get -z <zone>`: validation status for pending certificates.
- `cf ssl custom-certificates list -z <zone>`
- `cf ssl origin-ca-certificates list --zone-id <zone-id>`: ignores `-z` and doesn't resolve a
  domain; pass the 32-character id from `cf zones list`.

### WAF

WAF custom rules, managed rules and rate limits are rulesets, one entry point per phase.

- `cf rulesets list -z <zone>`; without `-z`, the account's rulesets.
- `cf rulesets entrypoint get <phase> -z <zone>`: for example `http_request_firewall_custom`,
  `http_request_firewall_managed`, `http_ratelimit`.
- `cf rulesets get <id> -z <zone>`
- `cf firewall access-rules list -z <zone>`: IP access rules.

### Turnstile

- `cf turnstile widgets list`: names, site keys, domains and modes, without secrets. Use it
  instead of `get`, which returns the widget secret.

### Tunnels

- `cf tunnels list --is-deleted false`, `cf tunnels get <tunnel-id>`
- `cf tunnels config get <tunnel-id>`: ingress rules of a remotely managed tunnel.
- `cf tunnels connections list --tunnel-id <tunnel-id>`: connectors, versions and locations.

# Example

"`app.example.com` returns 502s."

1. `cf dns records list -z example.com --name app.example.com | jq '.[] | {type, content, proxied}'`
   shows a proxied CNAME to `<tunnel-id>.cfargotunnel.com`.
2. `cf tunnels connections list --tunnel-id <tunnel-id>` returns an empty array: no connector is
   running.
3. `cf tunnels config get <tunnel-id> | jq '.config.ingress'` confirms that the hostname routes to
   `http://localhost:8080`.
4. Report it: the tunnel has no live connector, so Cloudflare can't reach the origin. The fix is on
   the host that runs `cloudflared`, not in Cloudflare.

# Notion plugin

Gives the agent Notion search, page and database reads, database queries, comments, and
gated page create/update and commenting. It talks to Notion's hosted MCP server
(`https://mcp.notion.com/mcp`) over streamable HTTP with the `mcp` client library, one
session per call.

## Namespaces and functions

One namespace, `notion`. The model learns the functions from `skills/notion/SKILL.md`.

- Read (`policy.read`): `notion.search`, `notion.get_page`, `notion.get_database`,
  `notion.query_database`, `notion.get_comments`.
- Write (`policy.write`): `notion.create_page`, `notion.update_page`, `notion.add_comment`.

## Credential

OAuth. `archie auth login notion` runs the browser flow through the orchestrator. Endpoints
are discovered from `server_url: https://mcp.notion.com` (RFC 9470/8414) and, with no
`client_id` configured, the orchestrator registers a client dynamically at the discovered
registration endpoint; endpoints, client id and any client secret are stored with the token.
The orchestrator refreshes the token before it expires. Not projected into the container
environment.

## Configuration

```yaml
plugins:
  notion:
    enabled: true
    policy:
      read:                       # default: enabled, no scope
        enabled: true
        scope: {pages: [<page-id>], databases: [<database-id>]}
      write:                      # default: disabled
        enabled: true
        scope: {pages: [<page-id>]}
```

| Key | Type | Default | Read | Meaning |
| --- | --- | --- | --- | --- |
| `policy.read.enabled` | bool | `true` | every exec call | Read functions |
| `policy.read.scope` | `{pages, databases}` | none | every exec call | Roots reads may touch |
| `policy.write.enabled` | bool | `false` | every exec call | Write functions |
| `policy.write.scope` | `{pages, databases}` | none | every exec call | Roots writes may touch |

`auth` takes the usual OAuth overrides (see `docs/auth-credentials.md`); none are needed. No
settings. Scope lists hold up to 100 Notion IDs each (32-hex or UUID form). Missing or empty
means unrestricted; read and write scopes are independent. An invalid scope in either block
fails every call closed. `enabled: false` removes the namespace and skill.

## How it works

- `client.py`: `notion_call(name, arguments)` opens an MCP session with the access token from
  `context()`, calls one Notion MCP tool, decodes its JSON text and applies a 60 s deadline.
- `normalization.py`: parses MCP response envelopes, including the ancestor path used for
  scope checks.
- `policy.py`: ID normalisation, `require_read()` / `require_write()` and `in_scope()`.
- `search.py`, `reads.py`, `query.py`, `writes.py`: the tools.

Ancestor scope, enforced in code (`policy.in_scope`): a resource is in scope when it is a
root, or a root appears among its ancestors as Notion reports them. Database resources
(databases, data sources, items) match `databases` roots, and also `pages` roots that are
their ancestors; pages match only `pages` roots. When a scope is set, reads and writes fetch
the target (or the parent, for `create_page`) first and refuse if its ancestry does not
prove the relationship. Search does one provider-wide search and drops results that are out
of scope or lack ancestry, so it may return fewer than `limit`. Any failure of the mutation
call itself raises `NotionMutationIndeterminateError`, since the write may have happened.

No cache and no startup hook.

## Tests

`plugins/notion/tests/`: `notion_fakes.py` holds `fake_credential`, `notion_context` and the
fixtures `notion_context_installed` (autouse) and `policy`; `conftest.py` imports them. Run
`uv run pytest -q plugins/notion`.

See [docs/plugins.md](../../docs/plugins.md) for the manifest format, the plugin SDK and testing conventions.

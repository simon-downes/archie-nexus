# Jira plugin

Gives the agent Jira Cloud project and issue reads, structured issue search, and gated
issue writes (create, update, transition, comment, attach). It calls the Jira Cloud REST API
v3 through `https://api.atlassian.com/ex/jira/<cloud_id>/rest/api/3` with `httpx`.

## Namespaces and functions

One namespace, `jira`. The model learns the functions from `skills/jira/SKILL.md`.

- Read (`policy.read`): `jira.list_projects`, `jira.get_project`, `jira.list_issues`,
  `jira.get_issue`, `jira.search_users`.
- Write (`policy.write`): `jira.create_issue`, `jira.update_issue`, `jira.transition_issue`,
  `jira.add_comment`, `jira.attach_file`.

## Credential

Static, fields `email`, `token` (an Atlassian API token) and `cloud_id`. `archie auth login
jira` reads a JSON object from stdin or, without piped input, prompts for each field (the
token hidden):

```bash
printf '%s' '{"email":"user@example.com","token":"...","cloud_id":"..."}' | archie auth login jira
```

Requests use HTTP Basic auth with `email:token`. Not projected into the container
environment.

## Configuration

```yaml
plugins:
  jira:
    enabled: true
    policy:
      read: {enabled: true, scope: [OPS, PLAT]}    # default: enabled, no scope
      write: {enabled: true, scope: [OPS]}         # default: disabled
```

| Key | Type | Default | Read | Meaning |
| --- | --- | --- | --- | --- |
| `policy.read.enabled` | bool | `true` | every exec call | Read functions |
| `policy.read.scope` | list[str] | none | every exec call | Project keys reads may touch |
| `policy.write.enabled` | bool | `false` | every exec call | Write functions |
| `policy.write.scope` | list[str] | none | every exec call | Project keys writes may touch |

No settings. Scope is a list of up to 100 project keys, matched case-insensitively. Missing or
empty means unrestricted; read and write scopes are independent. An invalid scope in either
block fails every call closed. `enabled: false` removes the namespace and skill.

## How it works

- `client.py`: `JiraClient` and `jira_client()`; takes the credential from `context()`,
  validates `cloud_id`, applies a 60 s deadline and maps HTTP errors to typed errors.
- `policy.py`: key validation and `require_read(key)` / `require_write(key)`, which parse both
  scopes and check the issue's project against the block's scope.
- `search.py`: `list_issues` builds JQL from structured filters (no raw JQL) and adds
  `project in (...)` from the read scope, so the scope cannot be bypassed.
- `tools.py` (projects), `issues.py` (issue reads, users, mutations), `attachments.py`,
  `formatting.py`.
- `adf.py`: `markdown_to_adf()` converts description and comment Markdown (CommonMark plus GFM
  tables and strikethrough, parsed with `markdown-it-py`) to Atlassian Document Format. A
  normaliser keeps every node where ADF allows it, using the `ALLOWED` table, and degrades
  disallowed nesting rather than failing; `> [!NOTE]`-style alerts become panels. Input nested
  deeper than the parser's limit falls back to plain paragraphs and line breaks. Descriptions
  and comments are limited to 32,767 characters, Jira Cloud's own cap. `adf_to_markdown()`
  renders descriptions and comment bodies on read as Markdown the writer reads back to the same
  ADF; Jira-only nodes (expands, mentions, media, statuses, task lists) become readable text.

Safety rules enforced in code:

- `list_projects` drops projects outside the read scope; `search_users` is read-gated but not
  project-scoped.
- Internal lookups done for a write (assignee resolution, transition discovery) are gated by
  the write scope.
- `attach_file` is confined to the workspace (`context().workspace`): the path must resolve
  inside it, every component is opened with `O_NOFOLLOW` (no symlinks), the extension must be
  one of `.png .jpg .jpeg .gif .pdf .txt .csv .json .zip`, and the file is at most 10 MiB.
- A write that may have been sent and then failed raises `JiraMutationIndeterminateError`.

No cache and no startup hook.

## Tests

`plugins/jira/tests/`: `jira_fakes.py` holds `fake_credential`, `jira_context` and the
fixtures `jira_context_installed` (autouse) and `policy`; `conftest.py` imports them. Run
`uv run pytest -q plugins/jira`.

See [docs/plugins.md](../../docs/plugins.md) for the manifest format, the plugin SDK and testing conventions.

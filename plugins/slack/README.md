# Slack plugin

Gives the agent the signed-in user's Slack: user and conversation directories, search,
history, threads and reactions, plus gated message sends and reactions. It calls the Slack
Web API (`https://slack.com/api/`) and Real-time Search (`assistant.search.context`) with a
user token, using `httpx`.

## Namespaces and functions

One namespace, `slack`. The model learns the functions from `skills/slack/SKILL.md`.

- Read (`policy.read`): `slack.users`, `slack.conversations`, `slack.search`,
  `slack.messages`, `slack.thread`, `slack.reactions`.
- Write (`policy.write`): `slack.send_message`, `slack.react`.

## Credential

OAuth with a user token. `archie auth login slack` runs the browser flow through the
orchestrator; it needs `plugins.slack.auth.client_id` (Slack has no dynamic registration).
The manifest requests every `user_scope` the tools use; Slack grants are additive, so
re-running login adds new scopes to the existing token. The token is read from
`authed_user.access_token` and refreshed by the orchestrator when it has a refresh token. Not
projected into the container environment.

## Configuration

```yaml
plugins:
  slack:
    enabled: true
    auth:
      client_id: "111.222"          # required for login
    policy:
      read:                         # default: enabled, deny nothing
        enabled: true
        scope: {deny: [C0123456789, "#hr-private"]}
      write:                        # default: disabled
        enabled: true
        scope: {deny: ["#general"]}
```

| Key | Type | Default | Read | Meaning |
| --- | --- | --- | --- | --- |
| `auth.client_id` | str | none | startup (orchestrator) | Slack app client ID |
| `policy.read.enabled` | bool | `true` | every exec call | Read functions |
| `policy.read.scope.deny` | list[str] | `[]` | every exec call | Conversations hidden from reads |
| `policy.write.enabled` | bool | `false` | every exec call | Write functions |
| `policy.write.scope.deny` | list[str] | `[]` | every exec call | Conversations writes refuse |

No settings. The scope is a deny list, not an allow list: entries are conversation IDs
(`C…`, `G…`, `D…`) or `#channel` names, matched case-insensitively. Read and write lists are
independent. An invalid scope in either block fails every call closed. `enabled: false`
removes the namespace and skill.

## How it works

- `client.py`: `api_call()` and `search_context()`; one attempt per call, token from
  `context()`, typed errors from `errors.py` (missing OAuth scope →
  `SlackReauthorizationRequiredError`).
- `policy.py`: validates both blocks on every call; `require_read()` and
  `deny_conversation()`. Writes check `policy()["write"]` directly.
- `resolver.py`: direct targets accept only canonical IDs (`C`/`G`/`D…`); names are not
  resolved yet.
- `users.py`, `conversations.py`, `search.py`, `messages.py`, `thread.py`, `reactions.py`,
  `writes.py`: the tools; `models.py` and `records.py` hold projections and cache records.

Deny list, as enforced in code: the read list filters `slack.conversations` results and
`slack.search` results, and refuses `slack.search(conversation=...)` on a denied target. It is
not checked by `slack.messages`, `slack.thread` or `slack.reactions`, which read any
canonical ID they are given. The write list refuses `send_message` and `react` before any
request. `send_message` prefixes the text with `:archie: ` and accepts only non-interactive
Block Kit (`header`, `section`, `context`, `divider`). An uncertain send or reaction raises
`SlackMutationIndeterminateError`.

Cache (`cache.max_bytes: 1048576`): `slack.users` (24 h TTL) and `slack.conversations`
(15 min TTL) store complete directory lists; `refresh=True` bypasses them, and a list over the
size limit is returned but not cached. No startup hook.

## Tests

`plugins/slack/tests/`: `slack_fakes.py` holds `fake_credential`, `slack_context` and the
fixtures `slack_context_installed` (autouse) and `policy`; `conftest.py` imports them. Run
`uv run pytest -q plugins/slack`.

See [docs/plugins.md](../../docs/plugins.md) for the manifest format, the plugin SDK and testing conventions.

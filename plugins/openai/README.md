# OpenAI plugin

Holds the OpenAI API key used by the direct OpenAI Responses backend, which serves the
`openai-*` catalog models (`openai-gpt-6-astra`, `openai-gpt-6-1-sol`, `openai-gpt-6-luna`,
`openai-gpt-5-6-cyber`). The plugin has no Python code, no tools and no skill: it only
declares the credential. Requests to these models go to `https://api.openai.com/v1`, not
through AWS.

## Namespaces and functions

No namespaces, no tools and no skill.

## Credential

Static, one field `api_key`, mapped to `OPENAI_API_KEY`. `archie auth login openai` reads the
key from the `OPENAI_API_KEY` environment variable on the host (there is no prompt or JSON
stdin; it fails naming the variable when it is unset):

```bash
OPENAI_API_KEY=sk-... archie auth login openai
```

`set_env: false`: the key is not exported into containers. The agent's OpenAI client reads
the stored `openai.api_key` from the shared credentials file and falls back to an
`OPENAI_API_KEY` variable already set in the agent's environment. `gpt-5.6-cyber` also needs
Daybreak approval on the key's OpenAI organisation.

## Configuration

```yaml
plugins:
  openai:
    enabled: true
```

No settings and no tool policy (no tools). `enabled: false` hides login and status for the
provider; it does not stop the agent using a key that is already stored (or `OPENAI_API_KEY`)
for `openai-*` models. Delete the credential to stop that.

## How it works

`agent/src/archie_agent/llm/openai_api.py` (`OpenAIClient`) builds an `openai.OpenAI` client on
the first request that finds a key, then reuses it. A request with no key fails with an error
naming `archie auth login openai`; the next request looks for the key again, so logging in
takes effect without restarting the session. Request building and stream parsing reuse the
Responses helpers in `agent/src/archie_agent/llm/bedrock_openai.py`, including explicit prompt
cache breakpoints with a 30-minute TTL.

## Tests

No `plugins/openai/tests`. The client and catalog are covered by `tests/test_openai_api.py`
and `tests/test_models_catalog.py`. Run them with
`uv run pytest -q tests/test_openai_api.py tests/test_models_catalog.py`.

See [docs/plugins.md](../../docs/plugins.md) for the manifest format, the plugin SDK and testing conventions.

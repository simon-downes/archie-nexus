# Kiro plugin

Holds the Kiro API key used by sessions started with `archie start --kiro`, which run on
`kiro-cli acp --agent-engine=v3` instead of Archie's own agent loop (see
[docs/kiro.md](../../docs/kiro.md)). The plugin has no Python code, no tools and no skill: it
only declares the credential.

## Namespaces and functions

No namespaces, no tools and no skill.

## Credential

Static, one field `api_key`, mapped to `KIRO_API_KEY`. `archie auth login kiro` reads the key
from the `KIRO_API_KEY` environment variable on the host:

```bash
KIRO_API_KEY=... archie auth login kiro
```

`set_env: false`: the key is not exported into containers or into tool commands. The Kiro
backend reads the stored `kiro.api_key` from the shared credentials file (falling back to a
`KIRO_API_KEY` variable already set in the agent's environment) and passes it only to the
`kiro-cli` process.

## Configuration

```yaml
plugins:
  kiro:
    enabled: true
```

No settings and no tool policy. Kiro models, effort and credit pricing live in the top-level
`kiro:` section of `config.yaml` (see [docs/config.md](../../docs/config.md)).

## Tests

No `plugins/kiro/tests`. The backend is covered by `tests/test_kiro_*.py`.

See [docs/plugins.md](../../docs/plugins.md) for the manifest format, the plugin SDK and testing conventions.

# Plugins

A plugin is one integration (Datadog, Slack, Google, AWS, …) kept in one directory under
`plugins/<id>/`. It holds the integration's manifest, Python code, skills and tests. Code
and tests are authoritative when this document and the implementation disagree.

## What is and isn't a plugin

A plugin may provide:

- exec tools under its own namespaces (for example `datadog.search_logs`);
- a credential provider (static or OAuth), named after the plugin id;
- tool policy defaults, settings, a cache limit and a startup hook;
- skills that tell the model how to use its tools.

Core tools are not plugins and stay in `agent/src/archie_agent/exec/tools/`: filesystem
(`read`, `write`, `edit`, `glob`, `grep`), `shell`, web (`web_fetch`, `web_search`),
`code`, `brain_search`, plus the harness tools `exec`, `skill` and `task`. The `bedrock`
model provider is also core and defined in `shared/src/archie_shared/credentials/providers.py`.

## Directory layout

```text
plugins/<id>/
  plugin.yaml              # manifest (required)
  README.md                # maintainer doc for this plugin (required; see below)
  __init__.py              # package archie_plugins.<id>; present when the plugin has Python code
  *.py                     # more modules, imported relatively or as archie_plugins.<id>.<mod>
  skills/<name>/SKILL.md   # one or more skills
  tests/                   # no __init__.py
    conftest.py            # imports fixtures from the fakes module
    <id>_fakes.py          # fakes and fixtures, a top-level module
    test_<id>_*.py         # basenames unique across the repo
```

The directory name is the plugin id (`[a-z][a-z0-9_]*`). Python code is imported as
`archie_plugins.<id>`: `archie_plugins` is a synthetic package whose path is the plugins
root, so `plugins/` itself is never on `sys.path`. `plugins_dir()` resolves the root from
`ARCHIE_PLUGINS_DIR`, defaulting to `<repo>/plugins`. In a container the directory is
mounted read-only at `/opt/archie/plugins`.

Plugins without code (`github`, `scalr`) have only `plugin.yaml` and `README.md`.

Each plugin's `README.md` covers what is specific to it: what it does and how it talks to the
service, its namespaces and functions grouped read vs write, its credential and
`archie auth login` behaviour (and any environment variables it projects), a `plugins.<id>`
config snippet with every setting and policy default and the scope semantics, how the code
works (module layout, safety rules, caching, hooks), and how to run its tests. It links back
here instead of repeating the manifest format, SDK or testing conventions. Skills are read
only from `skills/<name>/SKILL.md`, so the README is never loaded as a skill.

## `plugin.yaml` reference

The manifest is parsed by `shared/src/archie_shared/plugins/manifest.py`. Unknown keys are
errors, and every error names the plugin directory.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | str | required | Plugin id; must equal the directory name |
| `namespaces` | list[str] | `[]` | Exec namespaces the plugin's tools register under. Unique across all plugins; must not equal a core tool name or a runner builtin (`asyncio`, `os`, `json`, `re`, `Path`) |
| `entry` | str | none | Module imported to register tools; must be `archie_plugins.<id>` or start with `archie_plugins.<id>.`. Absent means no tools |
| `hooks.startup` | str | none | `module:function` run once at agent startup; module must be under `archie_plugins.<id>` |
| `credential` | mapping | none | Credential provider; see below |
| `policy` | mapping | see below | Tool policy defaults |
| `settings` | field map | `{}` | Values the plugin's code reads |
| `cache.max_bytes` | int | `262144` | Size limit for the plugin's cache, `1`..`8388608` |
| `context` | list[str] | `[]` | Brain entries the plugin's skills rely on, relative to the brain root, no `..` |

### `credential`

`kind` is `static` or `oauth`. The provider id is the plugin id.

Static fields:

| Key | Default | Meaning |
| --- | --- | --- |
| `fields` | required | Credential field names, at least one |
| `env` | `{}` | Field → environment variable. `archie auth login` reads these variables; keys must be in `fields` |
| `set_env` | `false` | Export the `env` variables into new containers |
| `login` | none | `aws_chain`: `archie auth login` reads the AWS credential chain |

OAuth fields:

| Key | Default | Meaning |
| --- | --- | --- |
| `server_url` | none | Resource server to discover endpoints from (RFC 9470/8414) |
| `authorization_endpoint`, `token_endpoint`, `registration_endpoint` | none | Fixed endpoints |
| `scopes` | none | Requested scopes |
| `token_path` | `access_token` | Path to the access token in the token response |
| `refresh_token_path` | `refresh_token` | Path to the refresh token |
| `expires_in_path` | `expires_in` | Path to the expiry |
| `extra_params` | none | Extra authorization parameters (Slack's `user_scope`, Google's `access_type`) |

A static credential may not set OAuth fields, and an OAuth credential may not set `fields`,
`env`, `set_env` or `login`. See [Authentication and credentials](auth-credentials.md) for
the login flows and storage.

### `policy`

```yaml
policy:
  read: {enabled: true}      # default when policy is present
  write: {enabled: false}    # absent = the plugin has no write tools
  scope: {type: "list[str]"} # absent = scopes are not supported
```

`scope` is a field spec (below, without `env`) describing the value accepted in
`plugins.<id>.policy.<read|write>.scope`. A plugin with an `entry` but no `policy` key gets
`read: {enabled: true}` and no writes. A plugin without an `entry` and without `policy` has
no tool policy.

### Field specs (`settings`, `policy.scope`)

Each field is `{type, default?, fields?, env?}`:

- `type`: `str`, `int`, `float`, `bool`, `list[str]`, `dict[str,str]` or `object`.
- `default`: checked against `type`; absent means `None`.
- `fields`: required for `object`, a nested field map; not allowed for other types.
- `env` (settings only): an environment variable name. Allowed only on a top-level field
  of type `str`, `int`, `float` or `bool` (not inside an `object`). When the resolved value
  is set, the orchestrator exports it into new containers (Scalr's `hostname` →
  `SCALR_HOSTNAME`).

Field names follow `[a-z][a-z0-9_]*` and may not be Python keywords. The field map is
compiled to a msgspec struct with unknown fields forbidden, so the host validates config
without importing plugin code.

### Example

```yaml
name: datadog
namespaces: [datadog]
entry: archie_plugins.datadog
credential:
  kind: static
  fields: [api_key, app_key]
policy:
  read: {enabled: true}
  write: {enabled: false}
settings:
  site: {type: str, default: app.datadoghq.eu}
```

## `config.yaml`: `plugins.<id>`

Each plugin is configured under `plugins.<id>` in `~/.nexus/config.yaml`:

- `enabled` (default `true`): `false` removes the plugin's tools and skills and stops its
  credentials and settings being exported into new containers.
- `policy`: `read` and `write` blocks, each `{enabled, scope}`, validated against the
  manifest's `policy`.
- `auth`: overrides for the credential provider, such as an OAuth `client_id`.
- `settings`: values validated against the manifest's `settings`.

`policy` is re-read before every exec call. `enabled` is read at startup for which plugins
are imported and which skills are listed; a plugin disabled while a session runs has both
policy blocks turned off from the next exec call. `auth` and `settings` are read at
startup. Unknown plugin ids, unknown fields, wrong types, a `write` block for a plugin
without writes and a `scope` for a plugin without one are load errors. See
[Configuration](config.md#plugins) for examples.

## The plugin SDK

Plugin code imports only from the SDK:

```python
from archie_agent.exec.plugin import ToolError, context, require_read, require_write, tool
```

- `tool(namespace=...)` registers an async function as `<namespace>.<function>`. The
  module must be under `archie_plugins.<id>`; the plugin id comes from the module name. The
  namespace must be declared in that plugin's manifest, otherwise loading fails.
- `ToolError` is the base for the plugin's exceptions. The runner reports a `ToolError` as
  type and message, without a traceback.
- `context()` returns the current `PluginContext`.
- `require_read(error, message, *, scope_supported=False, scope_message=None)` and
  `require_write(...)` return the current policy block or raise `error(message)` when it
  is disabled. Without `scope_supported`, a non-empty scope also raises. A plugin with no
  write block always fails `require_write`.

### `PluginContext`

| Field | Meaning |
| --- | --- |
| `plugin` | Plugin id |
| `settings` | Read-only mapping: manifest defaults merged with `plugins.<id>.settings` |
| `policy` | `PluginPolicy(read, write)`; each a `PolicyBlock(enabled, scope)`, `write` is `None` when the plugin has no writes |
| `credentials` | `CredentialSource`; `get()` returns this plugin's stored credential or `None` |
| `cache` | `ToolCache` rooted at `<ARCHIE_HOME_DIR>/cache/<id>`, limited to `cache.max_bytes` |
| `workspace` | Workspace root, or `None` (startup hooks) |
| `clock` | Callable returning the current UTC `datetime` |

### Rules

- Fail closed: `context()` raises `PluginContextError` (a `ToolError`) when no context is
  installed. A plugin without a tool policy has reads disabled.
- Per-call context: the runner installs the plugin's own context around each tool call,
  so one exec can call several plugins and each sees its own policy and credentials.
- Credentials are read from the store on every `get()`, so tokens the orchestrator
  refreshes during a run are seen. A plugin can read only its own provider's credential.
- Plugin code must not read config, credentials or environment directly
  (`load_nexus_config`, `get_credential`, `os.environ`, …); `tests/test_plugin_layout.py`
  enforces this.

## Startup hooks

`hooks.startup` names a synchronous function with no arguments. The agent runs the hooks
of enabled plugins once at startup, before the harness is built, each inside its own
context with `workspace=None` and an empty policy. A failing hook, a missing function or
an async function stops the agent with a message naming the plugin. AWS uses a hook to
write `~/.aws/config` from `plugins.aws.settings.profiles`. `~/.aws` must not be mounted
into containers when `plugins.aws.settings.profiles` is set: the hook refuses to overwrite a
mounted `~/.aws` or `~/.aws/config`, or any existing `~/.aws/config` that does not start
with its generated header.

## How plugin tools are exposed

- Exec only: plugin tools are never native tools.
- Namespaced: model code calls `datadog.search_logs(...)`; core tools are flat names.
- Not listed in the exec tool description and never add system-prompt guidelines. The
  model learns them from the plugin's skills, loaded with the `skill` tool.
- Skills are discovered from `plugins/<id>/skills/` for enabled plugins, after
  `~/.agents/skills` and `persona/skills`. When a manifest's `context` entry is missing
  from the brain, loading the skill starts with a warning naming it.
- Disabled plugins (`enabled: false`) are not imported by the runner. The runner imports
  only the plugins listed in `context.json`'s `enabled`; a missing, unreadable or
  old-shape context, or one without an `enabled` list, loads no plugins.

`exec.tools.get_all_tools()` is the core registry only; it feeds native specs, the exec
description and guidelines. The runner adds plugin tools from
`exec.plugin_loader.load_plugins()`.

## Testing

Plugin tests live in `plugins/<id>/tests/` and run with the rest of the suite
(`testpaths = ["tests", "plugins"]`). The repo-root `conftest.py` installs
`archie_plugins` and points `ARCHIE_HOME_DIR` at a temp dir for every test.

Build a context with `make_plugin_context` and install it with `install_context`:

```python
import pytest
from archie_agent.exec.plugin import install_context
from archie_agent.exec.plugin_testing import make_plugin_context
from archie_shared.credentials.models import CREDENTIAL_TYPES


@pytest.fixture(autouse=True)
def datadog_context(tmp_path):
    credential = CREDENTIAL_TYPES["datadog"](api_key="k", app_key="a")
    ctx = make_plugin_context(
        "datadog",
        tmp_path=tmp_path,
        policy={"datadog": {"read": {"enabled": True}, "write": {"enabled": False}}},
        credential=credential,
    )
    install_context(ctx)
    yield ctx
    install_context(None)
```

`make_plugin_context` takes manifest defaults for anything not given, keeps the cache
under `tmp_path`, and serves `credential` from memory. `install_context` sets the context
by value because async tests run in a copied context.

Generic contract tests in `tests/test_plugin_layout.py` check every plugin:

- every tool appears as `` `<ns>.<fn>(`` in one of its skills;
- every tool's namespace is declared and every declared namespace has a tool;
- a plugin with code has at least one skill;
- every plugin has a `README.md` that names each of its namespaces, settings keys and
  credential fields and links to this document;
- every skill has a `SKILL.md` with `name` and `description` frontmatter;
- no skill mentions `ARCHIE_HOME_DIR`, `credentials.yaml` or `archie_agent.`;
- plugin code reads nothing directly.

Per-plugin skill tests cover the rest: error classes, safety rules, absence of raw API
details.

## Adding a plugin

1. Create `plugins/<id>/plugin.yaml` with `name` and whatever else applies.
2. Add `__init__.py` and modules; register tools with `@tool(namespace=...)` and list
   the namespaces and `entry` in the manifest.
3. Gate every call with `require_read`/`require_write`; take credentials, settings and
   cache from `context()`.
4. Write `skills/<name>/SKILL.md` documenting each function as `` `<ns>.<fn>(...)` ``,
   its errors and safety rules.
5. Add `tests/` with `<id>_fakes.py`, `conftest.py` and `test_<id>_*.py`.
6. Write `plugins/<id>/README.md` with the sections described under
   [Directory layout](#directory-layout) (overview, namespaces and functions, credential,
   configuration, how it works, tests), ending with a link to this document; add it to
   `plugins/README.md`.
7. Document the plugin's config in [Configuration](config.md) and, for a credential,
   [Authentication and credentials](auth-credentials.md).
8. Run `uv run pytest`, `uv run ruff check .` and `uv run ruff format --check .`.

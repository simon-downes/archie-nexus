# 070 — Nexus plugins

Status: building
Branch: build-it/070-nexus-plugins (base: main @ 4fa2df9)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/070-nexus-plugins

## Objective

Each SaaS integration's code, skill, credential definition, policy defaults and settings are
currently spread across `agent/`, `shared/`, `cli/`, `persona/` and `tests/`. This build gathers
each one into a plugin under a new top-level `plugins/` directory, with a declarative
`plugin.yaml` and a `plugins:` section in `~/.nexus/config.yaml`. Core tools stay internal.
When done, adding or changing an integration means touching one directory, and plugin tools are
exec-only and get their credentials, policy, settings and cache from a host-supplied context.

## Context

Simon asked to implement everything in the brain document
`~/.nexus/brain/projects/archie/nexus-plugins-design.md` ("Nexus plugins — unifying integration
code, skills and credentials"). That document holds the decisions (1–10), the resolved design
decisions (1–19), the per-integration trace and the "Changes needed" list (1–15). This plan turns
it into milestones and does not repeat its reasoning.

Constraints from Simon:
- Single user, single host. Old config formats may be replaced outright, but no step may leave
  the system broken: every milestone leaves tests passing and config loadable.
- Use subagents for implementation, in small pieces with full context, not whole-integration
  prompts.

Codebase facts found during discovery:
- Integration code: `agent/src/archie_agent/exec/tools/{datadog,incident_io,jira,notion,slack}/`
  packages plus flat Google files (`mail.py`, `calendar.py`, `drive.py`, `meet.py`,
  `discovery.py`, `google_common.py`). Registration via `@tool` in `exec/tools/__init__.py`;
  `get_all_tools()` hard-codes the import list.
- The runner (`exec/runner.py` `_build_namespace`) installs the policy snapshot and builds
  `SimpleNamespace`s for namespaced tools. `exec/tool.py` `_generate_tool_docs()` skips
  namespaced tools. `exec/native.py` `make_native_specs()` builds native specs for `_native` tools.
- Skills: `agent/src/archie_agent/skills.py` `discover_skills()` scans `~/.agents/skills` then
  `persona_dir()/skills`.
- Providers: `shared/credentials/providers.py` `PROVIDERS`, `effective_provider()`;
  credential structs in `shared/credentials/models.py` `CREDENTIAL_TYPES`. Consumers:
  `orchestrator/auth.py`, `orchestrator/credential_refresh.py`, `orchestrator/app.py`,
  `shared/credentials/{runtime,refresh,store}.py`, `cli/auth.py` (hard-coded static set, line 135).
- Config: `shared/schemas.py` `NexusConfig` (`auth: AuthConfig`, `tools: dict[str,
  ProviderToolPolicy]`, `forbid_unknown_fields`). `agent/tool_policy_live.py` re-reads
  `config.yaml` before every exec call; `agent/app.py` lifespan loads config at startup.
- Container: `orchestrator/lifecycle.py` mounts `agent/` (rw), `shared/` (ro), `persona/` (rw)
  and sets `ARCHIE_PERSONA_DIR`. `shared/config.py` `persona_dir()` defaults to
  `<repo>/persona` on the host. `container/bin/gh` is baked into the image and reads
  `.auth.providers.github.work_orgs` with `yq`.
- Simon's live `~/.nexus/config.yaml` uses `auth.providers.{slack,google}.client_id`,
  `auth.providers.github.work_orgs` and `tools.{notion,slack,google,jira}`. This running
  session re-reads it before every exec call.
- Slack cache sizes today: `~/.nexus/slack/users.json` 132,796 bytes (817 records),
  `conversations.json` 75,142 bytes (462 records). The shared `ToolCache` limit is 256 KB.
- About 38 test files reference old integration paths (31 with dotted `archie_agent.exec.tools.<integration>`
  paths, the rest via `from archie_agent.exec.tools import mail/calendar/...`); 95 dotted-string monkeypatches. `tests/conftest.py` loads `tests.datadog_fakes` and
  `tests.incident_io_fakes` as pytest plugins.

## Baseline

- Check commands: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`
- Setup: `uv sync`
- Setup leftovers: none
- Failing before this work: none (1660 passed; ruff check and format clean)
- Not included: `persona/skills/aws-agent-access/` and `plans/060-spec-container-aws-profiles.md`
  (untracked in the main checkout)

## Decisions

The brain document's decisions 1–10 and resolved decisions 1–19 apply and are not repeated.
Rows below are the ones this plan adds or makes concrete.

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | How the old config format is replaced | `archie config migrate` rewrites `tools:`/`auth:` into `plugins:` with a timestamped backup; repeat runs are no-ops; the loader hard-errors on old sections and names the command | Loader accepts both formats; manual edit | Single host, but a running orchestrator must not silently ignore policy | user Q1 |
| D2 | Python import name for plugin code | Each `plugins/<id>/` is the package `archie_plugins.<id>`; a loader installs `archie_plugins` as a package whose `__path__` is the plugins root | Plugin dir on `sys.path` as top-level `datadog`/`slack`/`google` | Top-level `google`/`slack` would shadow real PyPI packages; one prefix keeps dotted paths greppable | self |
| D3 | Manifest `entry` meaning | Full dotted module, e.g. `entry: archie_plugins.datadog`; must start with `archie_plugins.<id>`; absent = no code | Relative module name; a boolean | Explicit and greppable; loader validates the prefix | self |
| D4 | Where manifest parsing lives | `shared/src/archie_shared/plugins/` (new): manifest structs, discovery, config validation, credential/provider generation | Agent package | Orchestrator and CLI must read manifests without importing agent code | self |
| D5 | Where the plugin SDK lives | `agent/src/archie_agent/exec/plugin.py` (new: `tool`, `ToolError`, `context()`, `PluginContext`, `require_read/require_write`) and `exec/plugin_loader.py` (new: package install, entry import, hooks) | `shared/`; a separate `archie_plugin` dist | Tool layer only (Kiro constraint 1); no new package to install in the container venv | self |
| D6 | Plugin tool registration | Separate `exec.plugin.tool(namespace=...)` into a plugin registry; plugin id derived from the defining module's `archie_plugins.<id>` prefix; namespace must be declared in that plugin's manifest. No `guidelines` parameter | Keep one `@tool` with flags | Makes the core/plugin split structural (decision 7); guidelines banned for plugins (resolved 9) | self |
| D7 | Settings/scope schema format | Field map in `plugin.yaml`: `{type: str|int|float|bool|list[str]|dict[str,str]|object, default?, fields? (object), env? (settings only)}`, compiled with `msgspec.defstruct(forbid_unknown_fields=True)` | JSON Schema; Python validators on host | Resolved decision 6; host validates types without agent code | self |
| D8 | Datadog `site` | Becomes `plugins.datadog.settings.site` (default `app.datadoghq.eu`); `site` removed from `StaticProvider` and `StaticProviderResponse` | `plugins.datadog.auth.site` | Only runner code reads it (`client._site()`); "values code reads are settings" (design item 7) | self |
| D9 | Scalr `hostname`/`account` | Settings with `env: SCALR_HOSTNAME`/`SCALR_ACCOUNT`; `runtime_environment()` projects settings with `env` as well as credential fields | Keep as credential fields | Resolved decision 18; no scalr entry exists in the live `credentials.yaml`, so no data moves | self |
| D10 | Bedrock overrides | Bedrock stays a core provider in `providers.py` with no config overrides; `auth:` is removed entirely | Keep a top-level `auth:` for bedrock | Live config has no bedrock override; one config location per thing | self |
| D11 | How runner code receives settings and enabled set | `context.json` becomes `{"policy": <live snapshot>, "settings": {id: {...}}, "enabled": [ids]}`; settings/enabled captured once at agent startup by `LiveToolPolicy`, policy refreshed per exec | Runner reads `config.yaml` itself | Keeps settings startup-only and policy live (design "reload timing") with one producer | self |
| D12 | How a tool call gets its plugin's context | The runner's audit wrapper sets the contextvar to that tool's plugin context for the call | One global context per run | Calls into several plugins in one exec must each see their own policy/credentials | self |
| D13 | Credential access scope | `context().credentials.get()` returns only the plugin's own provider credential (provider id = plugin id) | Any provider by name | A plugin cannot read another integration's secrets | self |
| D14 | Plugin cache | `ToolCache(root=home_dir()/"cache"/<id>, max_bytes=manifest cache.max_bytes or 256 KB)`; Slack sets 1 MB and stores `{"records": [...]}` | Keep Slack `MetadataCache` | Users list is 132,796 bytes (817 records), already over half the 256 KB limit; one implementation (resolved 17) | self |
| D15 | Scope `None` semantics | Absent/`None`/empty means unrestricted (Jira, Notion), deny nothing (Slack); a plugin whose manifest declares no `scope` rejects any non-empty scope at config load | Per-plugin defaults | Verified in each `policy.py` during planning; uniform and matches today | self |
| D16 | Test layout | `plugins/<id>/tests/` has no `__init__.py`; fakes are top-level modules (`datadog_fakes.py`) imported by that dir's `conftest.py`; repo-root `conftest.py` (new) installs `archie_plugins`; `testpaths = ["tests", "plugins"]`; test basenames stay unique | `pytest_plugins` in nested conftest | Prototyped: two plugin conftests and cross-dir imports pass under pytest 9.1.1; nested `pytest_plugins` is unsupported | self |
| D17 | Order of work | Framework + Datadog tracer, then all manifests, then the config switch, then `PluginContext`, then each remaining integration moves and converts to the context in one step | Design order (move everything, then add context) | Each integration's files change once; the config switch can go early because this build doesn't touch the live config | self |
| D18 | `gh` wrapper delivery | Bind-mount `container/bin/gh` at `/usr/local/bin/gh:ro`; keep the Dockerfile `COPY` as a fallback | Rebuild image | Wrapper change ships without `archie build` | user Q1 (context) |
| D19 | Migrate command YAML handling | PyYAML `safe_load`/`safe_dump(sort_keys=False)`; comments are lost, and the backup keeps the original | Add `ruamel.yaml` | No new dependency; live config has no comments | self |
| D20 | Test isolation from the real Nexus home | Root `conftest.py` autouse fixture sets `ARCHIE_HOME_DIR` to a per-test temp dir (tests that set it themselves still override) | Edit the live config; skip affected tests | Reviewer showed 7 tests read the live `~/.nexus/config.yaml`; after M4 its `auth:`/`tools:` would fail them, and the live file must not change in this build | self |
| D21 | AWS-chain login for core `bedrock` and plugin `aws` | `StaticProvider` gains `login: str | None`; bedrock sets `login="aws_chain"` in code, aws via manifest; CLI branches on `provider.login` | Keep `{"bedrock","aws"}` literal | Removes the last literal provider set from `cli/auth.py` without a bedrock manifest (D10) | self |
| D22 | Which registry the harness sees | `exec.tools.get_all_tools()` stays core-only (native specs, exec docs, guidelines); the runner adds plugin tools from `plugin_loader.load_plugins()` separately | One merged registry with flags | Plugin tools can't leak into native specs or guidelines even before M12 removes the flags; the harness never imports plugin code | self |
| D23 | Plugin Python code without tools | Any plugin with Python files has `plugins/<id>/__init__.py`; `entry` names the module imported for tools; hook paths (`module:function`) must also start with `archie_plugins.<id>` | Hooks only allowed in `entry` | AWS has a hook but no tools | self |
| D24 | Where `CredentialSource` reads | Calls `archie_shared.credentials.store.get_credential(plugin_id, type)` on each `get()` | Snapshot at context build | Same freshness as today, so tokens the orchestrator refreshes mid-exec are seen | self |

## Requirements

Framework
- MUST discover plugins from `plugins_dir()` (`ARCHIE_PLUGINS_DIR`, default `<repo>/plugins`), one `plugin.yaml` per directory, and reject a manifest with unknown keys, an id that doesn't match its directory, or an `entry` outside `archie_plugins.<id>`.
  - AC: `shared` unit tests for each rejection, each naming the plugin directory in the error.
- MUST expose plugin tools only inside `exec`, under their declared namespaces; plugin tools never become native tools or appear in the exec description or system-prompt guidelines.
  - AC: test asserts `make_native_specs()` names are exactly the core set; exec description contains no `datadog.`; runner namespace has `datadog.search_logs`.
- MUST reject a plugin tool whose namespace is not declared in its manifest, and a plugin module registering under another plugin's id.
  - AC: loader unit test with a fixture plugin raises with both names in the message.
- MUST load skills from `plugins/<id>/skills/<name>/SKILL.md` for enabled plugins, in addition to `~/.agents/skills` and `persona/skills`.
  - AC: `discover_skills()` test finds `datadog` from `plugins/` and not from `persona/`.
- MUST warn, not fail, when a skill's plugin declares a `context:` brain entry that does not exist (paths are relative to `archie_shared.brain` root; a missing brain root counts as missing).
  - AC: the `skill` tool output for such a skill starts with a warning naming the missing entry.
- MUST mount `plugins/` read-only at `/opt/archie/plugins`, set `ARCHIE_PLUGINS_DIR`, and bind-mount `container/bin/gh` over `/usr/local/bin/gh:ro`.
  - AC: `tests/test_orchestrator_lifecycle.py` asserts the three docker args.

Credentials and config
- MUST build `PROVIDERS` and `CREDENTIAL_TYPES` from manifests plus the core `bedrock` entry, with the same provider set as today minus `linear`.
  - AC: `/auth/providers` test lists `aws, bedrock, datadog, github, google, incident_io, jira, notion, scalr, slack`; Slack `user_scope` and Google scopes equal today's.
- MUST derive the CLI static-login set from provider kind, not a hard-coded set.
  - AC: `archie auth login datadog` takes the static path; `login slack` the OAuth path; no literal provider set in `cli/auth.py`.
- MUST replace `tools:` and `auth:` with `plugins.<id>.{enabled, policy, auth, settings}`, validated against each manifest; unknown plugin ids, unknown settings, wrongly typed values, a `write` block for a plugin without writes, and a `scope` for a plugin without one are errors.
  - AC: `tests/test_nexus_config.py` cases for each.
- MUST fail to load a config that contains `tools:` or `auth:`, with a message naming `archie config migrate`.
  - AC: test on `load_nexus_config()`.
- MUST provide `archie config migrate` that moves `tools.X` → `plugins.X.policy`, `auth.providers.github.work_orgs` → `plugins.github.settings.work_orgs`, `auth.providers.datadog.site` → `plugins.datadog.settings.site`, other `auth.providers.X` fields → `plugins.X.auth`, drops `linear` with a printed note, exits 1 naming the key for any old key it cannot map (e.g. `auth.providers.bedrock.*`, `tools.<unknown>`), validates the result before writing, writes `config.yaml.bak-<UTC YYYYmmddTHHMMSSZ>`, and is a no-op that prints "already migrated" on a second run.
  - AC: CLI tests on a copy of the live config shape: output equals the expected YAML; backup exists and equals the input; second run leaves files and backups unchanged; invalid result writes nothing and exits 1.
- MUST keep policy live (re-read per exec call) and `enabled`/`auth`/`settings` startup-only.
  - AC: `test_tool_policy_live*.py` shows a policy edit applies to the next exec; a settings edit does not.
- MUST, for `enabled: false`, hide the plugin's namespace, skills, `/auth/providers` entry, OAuth refresh, env projection, CLI login and hooks.
  - AC: one test per surface with `plugins.datadog.enabled: false` (and `scalr` for env projection).
- MUST make the `gh` wrapper read `plugins.github.settings.work_orgs`.
  - AC: `tests/test_gh_wrapper.py` passes with the new config shape.

Runtime context
- MUST give plugin code a frozen `PluginContext` (plugin, settings, policy, credentials, cache, workspace, clock) through `context()`; calling it with no context installed raises `PluginContextError`.
  - AC: SDK unit tests for both.
- MUST provide one `require_read()`/`require_write()` keyed by the current context that raises the plugin's own policy error class with today's messages.
  - AC: each migrated plugin's existing disabled-policy tests pass unchanged in message.
- MUST remove direct `get_credential`, `current_policy_snapshot`, `load_nexus_config` and `os.environ` reads from plugin code.
  - AC: `tests/test_plugin_layout.py` greps `plugins/*/` excluding `plugins/*/tests/` for these names and finds none.
- MUST run each enabled plugin's `hooks.startup` once in the agent lifespan, before requests are served; a failure stops startup with the plugin named.
  - AC: app lifespan test with a failing fixture hook exits and logs `plugin 'x' startup hook failed`.

Integrations
- MUST move Datadog, incident.io, Jira, Notion, Slack and Google code, skills and tests into `plugins/<id>/` with unchanged model-visible namespaces and function signatures.
  - AC: each plugin's moved tests pass; generic contract test: every namespace function appears as `` `ns.fn(` `` in that plugin's skills.
- MUST provide manifest-only plugins for `github` and `scalr` (credential, settings, optional skill) and an AWS plugin whose startup hook writes `~/.aws/config` from `plugins.aws.settings.{region, profiles}` per the design's "AWS plugin" rules (name regex, INI-injection rejection, sorted output, `0600`/`0700`, atomic replace, no file when no profiles).
  - AC: `plugins/aws/tests/` cover each rule; the AWS skill is discovered.
- MUST move Jira's `guidelines=` text into the Jira skill, Google's `ARCHIE_TIMEZONE` to `plugins.google.settings.timezone` (default `Europe/London`), and Slack's cache onto the plugin cache.
  - AC: system prompt contains no Jira line; calendar test with `timezone: UTC` setting; Slack cache test writes under `cache/slack/`.
- MUST leave no `archie_agent.exec.tools.<integration>` path in code or tests.
  - AC: `tests/test_plugin_layout.py` greps `agent/ tests/ plugins/` for the integration module names and finds none.
- MUST remove `native`, `exec_enabled` and `exec_docs` from core `@tool`.
  - AC: `inspect.signature(tool)` has none of them; `make_native_specs()` reads the core registry only.

Out of scope: Kiro/MCP entrypoint; external/installed plugins; per-plugin Python dependency installs;
`credential_process` refresh for long AWS sessions; editing the live `~/.nexus/config.yaml`.

## Design

### Overview

A `plugins/<id>/` directory holds `plugin.yaml`, optional Python package code (`archie_plugins.<id>`),
`skills/` and `tests/`. Host code (orchestrator, CLI, shared) reads only manifests and config. The
container runner imports enabled plugin entries, registers their exec-only tools, and runs each call
with that plugin's `PluginContext`. Built in order: framework with Datadog as the tracer, all
manifests, the config switch, the context, then each integration.

### Code structure

New:
- `plugins/<id>/plugin.yaml`, `plugins/<id>/__init__.py` (for plugins with code), `plugins/<id>/skills/<name>/`, `plugins/<id>/tests/` (no `__init__.py`).
- `conftest.py` (repo root): installs `archie_plugins` from `<repo>/plugins` via the loader.
- `shared/src/archie_shared/plugins/__init__.py`, `manifest.py` (structs, `load_manifests(root)`), `schema.py` (field-map → msgspec), `config.py` (`PluginConfig`, `validate_plugins(raw, manifests)`, `resolved_settings`, `policy_defaults`), `migrate.py` (pure function `migrate_config(raw: dict) -> MigrationResult`).
- `shared/src/archie_shared/config.py` `plugins_dir()` (follow `persona_dir()` in the same file).
- `agent/src/archie_agent/exec/plugin.py` (SDK) and `exec/plugin_loader.py` (`install_package(root)`, `load_plugins(manifests, enabled) -> dict[id, PluginRuntime]`, `run_startup_hooks(...)`). Standalone functions, no harness imports.
- `cli/src/archie_cli/config_cmd.py`: `archie config migrate` (follow `cli/auth.py` + `main.add_command` in `cli/cli.py`).
- `tests/test_plugin_layout.py`: dotted-path and forbidden-read guards, generic skill/namespace contract.
- `tests/test_plugin_manifest.py`, `tests/test_plugin_loader.py`, `tests/test_plugin_sdk.py`, `tests/test_config_migrate.py`, `tests/test_plugin_enabled.py`.
- `docs/plugins.md`.

Changed: `exec/tools/__init__.py` (core registry, no integration imports), `exec/runner.py`
(`_build_namespace` adds plugin namespaces and per-call context), `exec/native.py`, `exec/tool.py`,
`skills.py`, `tool_policy_live.py`, `app.py`, `harness.py`/`agents.py` (pass runner context),
`shared/schemas.py`, `shared/tool_policy.py`, `shared/credentials/{providers,models,api,runtime}.py`,
`orchestrator/{auth,credential_refresh,lifecycle,docker}.py`, `cli/auth.py`, `container/bin/gh`,
`agent/environment.py` (`ARCHIE_SELF_PATHS` adds `/opt/archie/plugins`), `pyproject.toml` (`testpaths`).

### Data model

`plugin.yaml` (all keys except `name` optional):

```yaml
name: datadog                     # == directory name, [a-z][a-z0-9_]*
namespaces: [datadog]
entry: archie_plugins.datadog
hooks: {startup: "archie_plugins.aws.profiles:write_config"}
credential:                       # provider id == name
  kind: static                    # static | oauth
  fields: [api_key, app_key]      # static
  env: {api_key: DD_API_KEY}      # static, optional
  set_env: false
  login: aws_chain                # static, optional (aws)
  # oauth: server_url | authorization_endpoint, token_endpoint, registration_endpoint,
  #        scopes, token_path, refresh_token_path, expires_in_path, extra_params
policy:
  read: {enabled: true}
  write: {enabled: false}         # omitted → plugin has no writes
  scope: {type: "list[str]"}      # omitted → any non-empty scope is a config error
settings:
  site: {type: str, default: app.datadoghq.eu}
cache: {max_bytes: 262144}
context: []                       # brain entry paths the skills depend on
```

`config.yaml`: `plugins: {<id>: {enabled: bool=true, policy: {read?, write?}, auth: {<oauth/static override fields minus site/work_orgs>}, settings: {...}}}`.

Runner `context.json`: `{"policy": {id: {read: {enabled, scope}, write: {enabled, scope}}}, "settings": {id: {...}}, "enabled": [id, ...]}`.

`PluginContext` (frozen dataclass): `plugin: str`, `settings: Mapping[str, Any]`, `policy: PluginPolicy(read: PolicyBlock, write: PolicyBlock | None)`, `credentials: CredentialSource`, `cache: ToolCache`, `workspace: Path | None`, `clock: Callable[[], datetime]`.

### Data flow

- Exec call: harness → `LiveToolPolicy.refresh()` (policy re-read; settings/enabled from startup) → `run_exec(..., runner_context=...)` writes `context.json` → runner installs package, imports enabled entries, builds one context per plugin, wraps each plugin tool so the call runs inside `use_context(ctx)` → plugin code calls `context()`.
  - Invalid config on refresh: last valid policy kept, TUI warning (unchanged).
  - Plugin entry import fails: the runner raises `RuntimeError("Failed to load plugin '<id>'")` and the envelope reports it; other plugins are not loaded for that call.
- Startup: `app.lifespan` loads config (hard error on old sections), runs hooks, builds `LiveToolPolicy`.

### Error handling

- Malformed manifest: host and container raise `PluginManifestError` naming the file; the orchestrator fails at start; the agent fails at startup.
- `context()` without a host: `PluginContextError("no plugin context")`.
- Missing credential: each plugin's existing configuration error class (unchanged behaviour).
- Migrate: any failure before the final `os.replace` leaves `config.yaml` untouched.

### Config / infra

Env: `ARCHIE_PLUGINS_DIR` (new). Mounts: `<repo>/plugins:/opt/archie/plugins:ro`,
`<repo>/container/bin/gh:/usr/local/bin/gh:ro`. Deployment (for Simon): containers bind-mount the
host `agent/`, so running sessions pick up new runner code the moment the main checkout changes, and
they lack the plugins mount. Therefore: stop all sessions and the orchestrator **before** merging or
pulling, then run `archie config migrate`, then `archie serve`. The runner does not accept the old
`context.json` shape; the PR body states this sequence.

## Milestones

Implementation is delegated to subagents in pieces of one module or one integration step each.
Every milestone ends with `uv run pytest -q -p no:cacheprovider`, `uv run ruff check .` and
`uv run ruff format --check .` passing.

### [x] M1 — Datadog runs as a plugin (tracer)

- **Approach:** Add `plugins_dir()`, `shared/plugins/manifest.py` and `schema.py`, the SDK `exec/plugin.py` (`tool`, `ToolError` re-export; plugin registry), `exec/plugin_loader.py` (D2/D3/D6). Per D22, `get_all_tools()` stays core-only and the runner merges `load_plugins()` tools when building namespaces (Datadog's `@tool(native=False, ...)` registrations switch to `exec.plugin.tool`). `skills.py` scans plugin skills. Move `exec/tools/datadog/*` → `plugins/datadog/` (imports become relative or `archie_agent.exec.plugin`), `persona/skills/datadog` → `plugins/datadog/skills/datadog`, Datadog tests + `datadog_fakes.py` → `plugins/datadog/tests/` with a local `conftest.py` (D16). Add root `conftest.py` (installs `archie_plugins`; D20 home-isolation fixture), `testpaths`, the plugins mount and `ARCHIE_PLUGINS_DIR` (the `gh` mount is M4), `docker.py` constant, `ARCHIE_SELF_PATHS`. Add `tests/test_plugin_layout.py` with the dotted-path guard for `datadog`. The `context:` warning in the skill tool lands here.
- **Test seam:** `load_manifests()`, `load_plugins()`, runner `_build_namespace`, `discover_skills()`, docker command.
- **Edge cases:** manifest id ≠ dir → error; undeclared namespace → error; entry import error → `RuntimeError` naming the plugin.
- **Deliverable:** `datadog.*` works in exec from `plugins/datadog`, with its skill.
- **Verify:** `uv run pytest -q plugins/datadog tests/test_plugin_manifest.py tests/test_plugin_loader.py tests/test_plugin_layout.py tests/test_runner.py tests/test_skills.py tests/test_native.py` passes; full checks pass.
- **Outcome:** Built `shared/plugins/` (manifest, schema; 52 tests), `exec/plugin.py` SDK, `exec/plugin_loader.py`, runner `_add_plugin_tools` (build failures now write an error envelope), plugin skill discovery with `context:` warnings, plugins mount + `ARCHIE_PLUGINS_DIR`, root `conftest.py` (package install, `pytest_collect_directory` so plugin dirs aren't imported as top-level packages, D20 home isolation), and moved Datadog code/skill/tests to `plugins/datadog/`. Evidence: full suite 1736 passed (232 Datadog tests collected from `plugins/datadog`); ruff check/format clean; a real `run_exec` subprocess lists 16 `datadog.*` functions, the exec description has no `datadog.`, native specs are the 10 core tools, and an unconfigured call raises `DatadogConfigurationError`. Home isolation exposed one test (`test_google_meet.py`) that read the real Google credential; it now stubs `credential` like its siblings. Deviation logged.

### [x] M2 — Trace cleanups (prefactor)

- **Approach:** Remove the `ARCHIE_TOOL_POLICY` fallback and `set_policy_snapshot` shim from `exec/tools/jira/policy.py` and update the tests that set the env var. Remove the top-level `enabled` key from `exec/tools/slack/policy.py`. Drop the `linear` provider and `LinearCredential` (providers, models, `credentials/__init__.py`, CLI set, tests).
- **Test seam:** `require_read/require_write` of each policy module; `/auth/providers`.
- **Deliverable:** No dead policy paths; no `linear`.
- **Verify:** full checks pass; `grep -rn "ARCHIE_TOOL_POLICY\|linear" agent shared cli orchestrator tests --include=*.py` finds nothing.
- **Outcome:** Removed Jira's `ARCHIE_TOOL_POLICY` fallback and shim (7 tests now set the runner snapshot inline; a sync fixture can't reset a contextvar token across pytest-asyncio's copied context), Slack's root `enabled` (also read in `slack/writes.py`), and `linear` everywhere including `docs/auth-credentials.md`. Evidence: the M2 grep returns nothing; full suite 1736 passed; ruff check/format clean.

### [x] M3 — Every integration has a manifest and plugin skill

- **Approach:** Add `plugin.yaml` for incident_io, jira, notion, slack, google (no `entry` yet; code still in `exec/tools`), aws (`login: aws_chain`, `set_env`), scalr, github. Move their skills from `persona/skills/` to `plugins/<id>/skills/`. Generate all non-bedrock `PROVIDERS`/`CREDENTIAL_TYPES` from manifests; delete the hand-written credential classes except `BedrockCredential` and `OAuthCredential`, and update imports (`jira/client.py`, `incident_io/client.py`, tests). Derive the CLI static set from provider kind and the AWS-chain case from `provider.login` (D21). Move Slack scopes and Google scopes into YAML; update skill-path constants in skill tests.
- **Test seam:** `PROVIDERS`, `CREDENTIAL_TYPES`, `/auth/providers`, `archie auth login`, `discover_skills()`.
- **Deliverable:** Credentials and skills come only from manifests; `persona/skills` holds procedure skills only.
- **Verify:** `uv run pytest -q tests/test_providers.py tests/test_credentials_models.py tests/test_orchestrator_auth_discovery.py tests/test_cli_auth_login.py tests/test_skills.py` passes; full checks pass.
- **Outcome:** Manifests for aws, datadog, github, google, incident_io, jira, notion, scalr, slack; generator `shared/plugins/credentials.py`; `PROVIDERS`/`CREDENTIAL_TYPES` = core bedrock + generated (hand-written classes except Bedrock/OAuth deleted); `StaticProvider.login` (D21) and CLI dispatch by provider kind/login with no literal sets; integration skills moved to `plugins/*/skills/`. Datadog `site` bridged from its setting default until M4. Evidence: an exact-match test of every provider against today's values passes; `list(PROVIDERS)` = bedrock, aws, datadog, github, google, incident_io, jira, notion, scalr, slack; full suite 1754 passed; ruff clean.

### [x] M4 — `plugins:` config replaces `tools:` and `auth:`

- **Approach:** `shared/plugins/config.py` validates `plugins:` against manifests (D7, D15). `NexusConfig` drops `tools`/`auth`, adds `plugins`; `load_nexus_config()` checks raw YAML for `tools`/`auth` first and raises `ConfigError` naming `archie config migrate`. `tool_policy` builds snapshots from manifest defaults + `plugins.*.policy`. Orchestrator/CLI/runtime read overrides from `plugins.<id>.auth`. Datadog `site` → setting (D8), read by `client._site()` from config for now (removed in M6). Github `work_orgs` → setting; update `container/bin/gh` and its test; add the `gh` bind mount. Add `shared/plugins/migrate.py` + `cli/config_cmd.py` (D1, D19). Update `docs/config.md` and `docs/auth-credentials.md`.
- **Test seam:** `load_nexus_config()`, `migrate_config()`, `archie config migrate` via Click `CliRunner`, `load_policy_snapshot()`, gh wrapper script.
- **Edge cases:** old + new sections both present → migrate merges when keys don't conflict, else exits 1 naming the key; config absent → migrate prints "no config" and exits 0; validation failure → nothing written.
- **Deliverable:** The new config shape works end to end; the live shape migrates in one command.
- **Verify:** `uv run pytest -q tests/test_nexus_config.py tests/test_config_migrate.py tests/test_gh_wrapper.py tests/test_tool_policy_live.py tests/test_tool_policy_live_exec.py` passes; a migrate run on a temp copy of the live config shape produces the expected `plugins:` YAML; full checks pass.
- **Outcome:** `shared/plugins/config.py` (structs, `validate_plugins`, `resolved_settings`, `policy_snapshot`), `NexusConfig.plugins` replacing `tools`/`auth` with a hard error naming `archie config migrate`, `PluginAuthConfig` overrides via `provider_override()`, Datadog `site` as a setting, gh wrapper on `plugins.github.settings.work_orgs` plus the `container/bin/gh` bind mount, `shared/plugins/migrate.py` + `archie config migrate`, docs rewritten. Fixed while reviewing: `policy_snapshot` omitted disabled plugins, which `resolve_provider_policy` would read as read-enabled; disabled plugins now get both blocks off. Evidence: full suite 1804 passed; ruff clean; migrate on a temp copy of the live config produced the expected `plugins:` YAML, a byte-identical backup, "already migrated" on the second run, and a file that loads with plugins github, google, jira, notion, slack.

### [x] M5 — `enabled: false` and settings-driven env

- **Approach:** Apply `enabled` (startup) in `runtime_environment()`, `credential_refresh.py`, orchestrator `providers()`/`provider()`, CLI login, runner entry import and `discover_skills()` (hooks get the filter in M7). `runtime_environment()` also projects settings fields that declare `env` (D9); move Scalr `hostname`/`account` to settings. Extend `LiveToolPolicy` and `run_exec` to carry `{"policy", "settings", "enabled"}` (D11) through `harness.py` and `agents.py`.
- **Test seam:** each listed surface; `context.json` content written by `run_exec`.
- **Deliverable:** A disabled plugin disappears everywhere; Scalr hostname/account come from settings.
- **Verify:** `uv run pytest -q tests/test_plugin_enabled.py tests/test_credentials_runtime.py tests/test_credential_refresh.py tests/test_exec_tool.py` passes; full checks pass.
- **Outcome:** `context.json` is now `{policy, settings, enabled}` (`runner_context` replaces `policy_snapshot` end to end; old shape → empty policy, all enabled). `LiveToolPolicy` holds startup-only settings/enabled. `enabled: false` hides the plugin from `/auth/providers`, `/auth/status`, `provider()`, OAuth refresh, env projection, CLI login, skills and runner imports. Settings with `env` are projected; Scalr `hostname`/`account` are settings. Choice: if config is invalid when skills are discovered, all plugin skills are shown (tools stay policy-gated). Not-yet-moved integrations (jira, notion, slack, google, incident_io) keep their namespace when disabled until they move, but their policy is off. Evidence: `tests/test_plugin_enabled.py` (14 tests) passes; full suite 1823 passed; ruff clean.

### [x] M6 — `PluginContext`, shared policy gates, Datadog converted

- **Approach:** Implement `PluginContext`, `CredentialSource` (D13), `context()`/`use_context()`, per-plugin `ToolCache` with `max_bytes` (D14; `exec/tools/cache.py` gains the parameter), shared `require_read/require_write(error=...)`. Runner wraps plugin tools per D12 with `workspace=_subprocess.WORKSPACE`. Convert Datadog: `client.py` credential + site via `context()`, `policy.py` uses the shared gates. Datadog tests build contexts with fake credentials (a `plugin_context` helper fixture in root `conftest.py`) instead of patching `get_credential`. Extend the layout test with the forbidden-read guard for `plugins/`.
- **Test seam:** SDK functions; Datadog public tools under a test context.
- **Deliverable:** Datadog takes everything from its context; calling a plugin tool outside a host fails closed.
- **Verify:** `uv run pytest -q plugins/datadog tests/test_plugin_sdk.py tests/test_plugin_layout.py` passes; full checks pass.
- **Outcome:** SDK gained `PluginContext`, `PluginPolicy`/`PolicyBlock`, `CredentialSource` (store read per call, own plugin only), `context()`/`use_context()`/`install_context()`, `require_read/require_write`, `build_context`; `ToolCache(max_bytes=)`; new `exec/plugin_testing.py` `make_plugin_context()` for plugin tests. The runner passes settings explicitly (module global removed) and wraps each plugin tool in its own context. Datadog reads credential/site/policy only from the context, with the same messages. Evidence: `tests/test_plugin_sdk.py` (22 tests, including two plugins called concurrently in one exec, each seeing its own context) passes; the forbidden-read guard passes for datadog; full suite 1847 passed; ruff clean; a real `run_exec` subprocess gives `DatadogConfigurationError: Datadog credentials are incomplete` with no credentials and `DatadogPolicyError: Datadog reads are disabled by policy` with read disabled.

### [x] M7 — AWS plugin with a startup hook

- **Approach:** `plugins/aws/profiles.py` `write_config()` reads `context().settings` and writes `~/.aws/config` per the design rules (port from untracked `plans/060-spec-container-aws-profiles.md` rules quoted in the brain document). `run_startup_hooks()` in the loader, skipping disabled plugins; `app.lifespan` calls it after config load. New short skill `plugins/aws/skills/aws-agent-access/SKILL.md` per the design's "AWS plugin" item 4, written fresh.
- **Test seam:** `write_config()` with a temp home; `run_startup_hooks()`; app lifespan.
- **Edge cases:** bad profile name / newline in ARN → hook error, old file kept; no profiles → no file; existing dir `0755` → tightened to `0700`.
- **Deliverable:** A container with `plugins.aws.settings.profiles` gets matching AWS CLI profiles.
- **Verify:** `uv run pytest -q plugins/aws tests/test_plugin_loader.py tests/test_plugin_enabled.py` passes; full checks pass.
- **Outcome:** `run_startup_hooks()` in the loader (enabled only, sync hooks inside the plugin's context, failure `plugin '<id>' startup hook failed: …`); `app.lifespan` runs hooks before building the harness and exits 1 on failure; `plugins/aws/profiles.py` `write_config()` follows every design rule; new 71-line `aws-agent-access` skill. Evidence: 21 AWS tests + 8 loader/lifespan tests pass; full suite 1877 passed; ruff clean; a smoke run with temp HOME/ARCHIE_HOME_DIR wrote the sorted config (file 600, dir 700), `aws configure list-profiles` read both profiles, and a bad profile name failed startup with the old file unchanged.

### [x] M8 — incident.io moves and uses the context

- **Approach:** Same as the Datadog move + M6 conversion; manifest gains `entry`; no write block.
- **Test seam:** `incident_io.*` public tools under a test context.
- **Deliverable:** `incident_io.*` served from `plugins/incident_io`.
- **Verify:** `uv run pytest -q plugins/incident_io tests/test_plugin_layout.py`; full checks.
- **Outcome:** incident.io code, fakes and tests moved to `plugins/incident_io/`; credential and read gate via the context with unchanged messages; `pytest_plugins` removed from `tests/conftest.py`. Evidence: 99 tests collected before and after the move; full suite 1880 passed; ruff clean; old-path grep empty; a real `run_exec` of `incident_io.reference('severities')` with no credential returns `IncidentIoConfigurationError: incident.io credentials are incomplete`.

### [x] M9 — Jira moves and uses the context

- **Approach:** As M8; `attachments.py` uses `context().workspace`; `cloud_id` regex stays in the client; the three `guidelines=` lines move into the Jira skill; scope `list[str]` in the manifest.
- **Test seam:** `jira.*` public tools; system prompt text.
- **Deliverable:** `jira.*` served from `plugins/jira`; no Jira lines in the system prompt.
- **Verify:** `uv run pytest -q plugins/jira tests/test_prompt.py tests/test_plugin_layout.py`; full checks.
- **Outcome:** Jira code and tests moved to `plugins/jira/`; credential, policy and attachment workspace come from the context (no workspace fails closed); the three guideline lines are now skill rules and the system prompt has none. Evidence: the 15 original tests are still collected, plus 15 new skill/policy/attachment tests; full suite 1899 passed; ruff clean; a real `run_exec` of `jira.get_issue('ABC-1')` gives `JiraConfigurationError: Jira credentials are incomplete`, and with read disabled `JiraPolicyError: Jira reads are disabled by policy`.

### [x] M10 — Notion moves and uses the context

- **Approach:** As M8; scope `object{pages: list[str], databases: list[str]}`; MCP session state stays plugin-internal; OAuth credential through the context.
- **Test seam:** `notion.*` public tools.
- **Deliverable:** `notion.*` served from `plugins/notion`.
- **Verify:** `uv run pytest -q plugins/notion tests/test_plugin_layout.py`; full checks.
- **Outcome:** Notion code and tests moved to `plugins/notion/`; OAuth credential and page/database scope via the context, MCP session state still inside the plugin, policy functions no longer take a snapshot argument (only tests passed one). Evidence: the 26 original tests are still collected, plus 15 new; full suite 1917 passed; ruff clean; a real `run_exec` of `notion.search('x')` gives `NotionConfigurationError: Notion credentials are incomplete`, and with read disabled `NotionPolicyError`.

### [x] M11 — Slack moves, uses the context and the plugin cache

- **Approach:** As M8; scope `object{deny: list[str]}`; delete `slack/cache.py`; users/conversations use `context().cache` keys `slack.users`/`slack.conversations` storing `{"records": [...]}` only when complete; manifest `cache.max_bytes: 1048576`.
- **Test seam:** `slack.*` public tools; cache files under a temp home.
- **Deliverable:** `slack.*` served from `plugins/slack`; one cache implementation.
- **Verify:** `uv run pytest -q plugins/slack tests/test_tool_cache.py tests/test_plugin_layout.py`; full checks.
- **Outcome:** Slack code and tests moved to `plugins/slack/`; credential and deny-list policy via the context; `MetadataCache` deleted and users/conversations cached in the plugin `ToolCache` (`cache/slack/slack.{users,conversations}.json`, 1 MB limit, values over the limit are returned but not cached). The old `~/.nexus/slack/*.json` files are ignored and refill once. Evidence: the 42 original tests are still collected, plus 28 new (policy, cache TTL/refresh/expiry/oversize); full suite 1948 passed; ruff clean; a real `run_exec` of `slack.users()` gives `SlackConfigurationError: Slack credentials are incomplete`.

### [ ] M12 — Google moves and uses the context

- **Approach:** Move `mail.py`, `calendar.py`, `drive.py`, `meet.py`, `discovery.py` and `google_common.py` (→ `common.py`) into `plugins/google/`; five namespaces in the manifest; timezone setting replaces `ARCHIE_TIMEZONE`; mail uses `context().cache`.
- **Test seam:** `mail/calendar/drive/meet/google.*` public tools.
- **Deliverable:** All Google namespaces served from `plugins/google`.
- **Verify:** `uv run pytest -q plugins/google tests/test_plugin_layout.py`; full checks.
- **Outcome:**

### [ ] M13 — Core `@tool` loses plugin flags; contract tests; docs

- **Approach:** Remove `native`/`exec_enabled`/`exec_docs` from `exec/tools/__init__.py` `tool`; `native.py` and `tool.py` read the core registry; generic per-plugin skill/namespace contract test replaces the hand-written ones where they only checked coverage (keep plugin-specific safety-rule assertions). Write `docs/plugins.md`; update `README.md`/`AGENTS.md` layout lines.
- **Test seam:** `tool` signature; `make_native_specs()`; contract test.
- **Deliverable:** The core/plugin split is structural and documented.
- **Verify:** full checks pass; `grep -rn "native=\|exec_docs\|exec_enabled" agent plugins --include=*.py` finds nothing.
- **Outcome:**

## Deviations

- M1: Datadog's provider/credential generation from its manifest moved to M3 with every other provider, so `PROVIDERS` switches source once and Datadog `site` isn't split across two mechanisms. No scope change.

## Risks

- `PROVIDERS`/`CREDENTIAL_TYPES` are built from manifests at import. A process whose `plugins_dir()` is wrong sees bedrock only. Host processes run from the repo; in the container `_REPO_ROOT` is `/opt/archie`, so the default matches the new mount.

- `plugins/` is mounted read-only, so sessions can no longer edit integration skills in place as they can `persona/skills`. Accepted in design resolved decision 3.
- Dropping Jira guidelines from the system prompt relies on the model loading the Jira skill.
- Slack's old cache files under `~/.nexus/slack/` are no longer read or removed; they can be deleted by hand.
- Moving Google's mail cache root to `cache/google/` makes the first label lookup after deploy cold.
- Deploy needs the documented sequence; a session started on new code against an unmigrated config fails at startup with the migrate instruction.

## Review

- Plan review: 1 pass, 16 findings (0 ❌, 7 ⚠️, 9 ℹ️), all accepted. Fixes: tracer moved to M1 and cleanups to M2; D20 test home isolation (reviewer reproduced 7 failures against the live config); D21 `login` field for bedrock/aws; D22 core-only harness registry; D23 hook paths and package for code-without-tools; D24 credential freshness; deploy note says stop sessions before merging because `agent/` is live-mounted; hooks' `enabled` filter moved to M7; gh mount only in M4; layout guard excludes plugin tests; context path root and migrate failure cases stated; test files and M5 Verify named; test-file count corrected.

## Outcome

PR: pending
CI: pending

## Follow-ups

- Mark untracked `plans/060-spec-container-aws-profiles.md` dropped and remove `persona/skills/aws-agent-access/` in the main checkout (both untracked, outside this branch).

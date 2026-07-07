# 003 — Config & Model Catalog Overhaul

## Objective

Replace the current tightly-coupled config and model catalog implementations in
`archie-shared` with a generic YAML config loader backed by msgspec validation,
and a data-driven model catalog with code defaults + user file overrides.

## Context

The various archie iterations (og, nextgen, nexus) each implemented config and
model catalogs differently — bespoke, tightly coupled to their iteration. The
current nexus implementation has a hardcoded `Config` dataclass and a Python-dict
model registry with `ModelInfo` dataclasses. This plan introduces a single generic
config framework (load YAML → validate against schema → return typed object) and a
model catalog that's code-defaults + user-overridable via `~/.nexus/models.yaml`.

This is a breaking change to `~/.archie/nexus.yaml` format (flat → sectioned).
Since we're the only user, existing files can be deleted and recreated.

**AMENDMENT — config-directory migration (this plan OWNS it).** In addition to
the format change, this plan migrates the config *location* so that plan 004
(credentials) can build on a single, stable convention:

- Archie home directory: `~/.archie` → `~/.nexus`. This is a HOME dir, not just
  config — it holds config, the model catalog, credentials (plan 004), and
  session logs. (Prior art: kiro `~/.kiro`, nextgen `~/.archie/sessions`.)
- Application config file: `nexus.yaml` → `config.yaml` (under the dir).
- Model catalog: `models.yaml` (under the dir, unchanged name).
- **Single env var `ARCHIE_HOME_DIR`** replaces the per-file `ARCHIE_CONFIG`.
  Host default `~/.nexus`; container `/home/${USERNAME}/.nexus` (symmetric with
  the host — `/opt/archie` is the archie install tree, code+venv, so the home
  dir must NOT live there). All files (`config.yaml`, `models.yaml`, later
  `credentials.yaml`, and `sessions/`) resolve *relative* to this directory.
- Container mount: bind the host home dir at `/home/${USERNAME}/.nexus`; the
  container gets `ARCHIE_HOME_DIR=/home/${USERNAME}/.nexus` set as an ENV. Works
  because host UID == container UID (already guaranteed via `USER_UID` build arg).
- Clean break, **no migration shim**. Delete old `~/.archie` and recreate.
- NOTE: this plan does the `ARCHIE_HOME_DIR` rename + mount ONLY. Session-log
  path work (sessions resolve at `home_dir()/sessions`, drop `ARCHIE_SESSIONS_DIR`,
  `"unknown"` session_id fallback fix, session.py/app.py docstrings) is a SEPARATE
  later plan.

## Requirements

### Config Framework

- MUST provide `load_config(path: str | Path, schema: type[T]) -> T` that loads
  YAML and validates against a msgspec Struct schema
- MUST raise `ConfigError` (custom exception) on missing files, malformed YAML,
  and validation failures — always includes file path in message
- MUST support default values on optional Struct fields
- MUST support nested Structs
- MUST support `dict[str, StructType]` for dynamic-key mappings

### Application Config (`config.yaml`)

- MUST define `NexusConfig` schema with sections: `global`, `cli`, `agent`, `web`
- MUST have each section be optional (empty/missing config → all defaults)
- MUST support `global.model` (active model key), `global.project_root`, and
  `global.region` (session default region; D2)
- MUST reject unknown fields in all config structs (`forbid_unknown_fields=True`; D4)
- Breaking change from flat format is acceptable (documented)

### Model Catalog

- MUST define default models in code as msgspec Structs
- MUST load user overrides/additions from `<ARCHIE_HOME_DIR>/models.yaml`
- MUST merge by key — user entries fully replace defaults for that key; new keys added
- MUST use `provider-model` key format (e.g. `bedrock-claude-opus-4-6`)
- MUST support fields: `name`, `context`, `max_output_tokens`, `context_warning_threshold`,
  `can_cache`, `cost` (nested: input, output, cache_read, cache_write), `provider` (nested:
  name, model_id, region, endpoint)
- MUST provide `calculate_cost(cost: CostConfig, ...) -> float`

### Integration

- MUST update agent `app.py` to use new config + catalog APIs
- MUST update CLI `start` command config handling
- MUST update `BedrockClient` to accept `can_cache` from model config, seeding
  `_cache_supported`; the runtime cachePoint-rejection fallback is retained (D1)
- MUST update `session.py` to use `ModelEntry` + new `calculate_cost` signature
- MUST NOT change wire protocol, events, or WebSocket handling
- MUST add `msgspec>=0.19` to `archie-shared`

---

## Technical Design

### Dependencies

- Add `msgspec>=0.19` to `shared/pyproject.toml`

### Generic Loader (`config.py`)

```python
def load_config(path: str | Path, schema: type[T]) -> T:
    """Load YAML file, validate against schema, return typed object."""
    # yaml.safe_load → msgspec.convert(data, schema, strict=False)
    # Empty file → treat as {} (all defaults)
    # Wraps all errors in ConfigError with file path
```

### Config Schema (`schemas.py`)

All config structs use `forbid_unknown_fields=True` so typos/unknown keys are
rejected loudly (D4 — catch issues sooner). Unknown keys → msgspec
ValidationError → wrapped in `ConfigError`.

```python
class GlobalConfig(msgspec.Struct, forbid_unknown_fields=True):
    model: str = "bedrock-claude-sonnet-4-6"
    project_root: str = "~/dev"
    region: str = "eu-west-1"  # session default region (D2 fallback)

class AgentConfig(msgspec.Struct, forbid_unknown_fields=True):
    pass  # future fields

class CliConfig(msgspec.Struct, forbid_unknown_fields=True):
    pass  # future fields

class WebConfig(msgspec.Struct, forbid_unknown_fields=True):
    pass  # future fields

class NexusConfig(msgspec.Struct, rename="lower", forbid_unknown_fields=True):
    global_: GlobalConfig = msgspec.field(default_factory=GlobalConfig, name="global")
    cli: CliConfig = msgspec.field(default_factory=CliConfig)
    agent: AgentConfig = msgspec.field(default_factory=AgentConfig)
    web: WebConfig = msgspec.field(default_factory=WebConfig)
```

**Region resolution (D2):** `global.region` provides the session default that
geo-inference Claude models rely on. Consumers resolve effective region as
`model.provider.region or config.global_.region`.

`load_nexus_config(path=None)` resolves path relative to `ARCHIE_HOME_DIR`
(default `~/.nexus`) as `<dir>/config.yaml`. Missing file at default path →
return all defaults. An explicit `path=` that is missing → raise ConfigError.

### Model Catalog (`models.py`)

```python
class CostConfig(msgspec.Struct):
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0

class ProviderConfig(msgspec.Struct):
    name: str                      # "bedrock" | "ollama"
    endpoint: str                  # bedrock: inference profile ID, ollama: host:port
    region: str | None = None      # bedrock region override

class ModelEntry(msgspec.Struct):
    name: str
    context: int
    provider: ProviderConfig
    can_cache: bool = False        # D1: default False; runtime fallback still applies
    cost: CostConfig = msgspec.field(default_factory=CostConfig)
    max_output_tokens: int = 32_768
    context_warning_threshold: float = 0.8

DEFAULT_MODELS: dict[str, ModelEntry] = { ... }  # all current models ported

def load_models(overrides_path: Path | None = None) -> dict[str, ModelEntry]:
    """Load defaults + merge user overrides file."""

def get_model(catalog: dict[str, ModelEntry], key: str) -> ModelEntry:
    """Lookup by key, raises KeyError with available keys."""

def calculate_cost(cost: CostConfig, input_tokens, output_tokens,
                   cache_read_tokens=0, cache_write_tokens=0) -> float:
    """Calculate USD cost from CostConfig rates."""
```

### Key Format

Current keys (`eu.anthropic.claude-sonnet-4-6`) become provider-model format
(`bedrock-claude-sonnet-4-6`). The actual Bedrock inference profile ID lives in
`provider.endpoint`. For ollama models, `provider.endpoint` is the host:port.
The `provider.name` field tells consumers how to interpret `endpoint`. Ollama
models are ported as catalog data only (D5); nexus has no ollama client yet, so
`app.py` constructs a `BedrockClient` regardless of `provider.name`. There is
**no guard** — selecting an ollama model before model-switching exists would send
its `host:port` as a Bedrock `modelId`. Accepted: these entries are inert config
until a client + model switching land.

### What Gets Removed

- `Config` dataclass, `ensure_default_config`, old `load_config()`, `DEFAULT_CONFIG` string
- `ModelInfo` dataclass, `MODELS` dict, `get_model_info()`
- `config.py`'s `from archie_shared.models import get_model_info` import and its
  model-key validation at load time (D3) — validation moves to `get_model()` at
  agent startup, so the config loader no longer depends on the catalog
- All replaced by new implementations in same files

**Config-dir resolution in `config.py`:** replace the old
`ARCHIE_DIR = Path.home() / ".archie"` constant with a resolver
`home_dir() -> Path` that reads `ARCHIE_HOME_DIR` (default `~/.nexus`,
expanduser). The old flat `credentials.py` also uses this; plan 004 rebuilds
credentials on top of the same `ARCHIE_HOME_DIR` resolution.

### What Stays Unchanged

- `credentials.py` — separate concern (fully rebuilt in plan 004). During this
  plan, apply only a minimal patch so it keeps importing cleanly after
  `ARCHIE_DIR` is removed: swap `from archie_shared.config import ARCHIE_DIR` /
  `ARCHIE_DIR / "nexus.creds.yaml"` to use `home_dir()`. No behavioural change.
- `events.py`, `types.py` — wire protocol (future work)
- `__init__.py` — updated re-exports only

---

## Milestones

### 1. Generic config loader with msgspec

**Approach:**
- Add `msgspec>=0.19` to `shared/pyproject.toml`
- Rewrite `shared/src/archie_shared/config.py`:
  - Add `home_dir() -> Path` resolving `ARCHIE_HOME_DIR` (default `~/.nexus`,
    expanduser). Remove old `ARCHIE_DIR = Path.home() / ".archie"` constant
    (callers switch to `home_dir()`; credentials rebuilt in plan 004)
  - New: `ConfigError(Exception)` with path + message
  - New: `load_config(path: str | Path, schema: type[T]) -> T`
  - Implementation: `yaml.safe_load` → handle None as `{}` → `msgspec.convert(data, schema, strict=False)`
  - Wrap `FileNotFoundError`, `yaml.YAMLError`, `msgspec.ValidationError` in `ConfigError`
- `strict=False` allows int→float coercion (cost fields specified as `5` not `5.0`)

**Edge cases:**
- File doesn't exist → `ConfigError("Config file not found: {path}")`
- File is empty (YAML → None) → treat as `{}`, all schema defaults apply
- Non-mapping root where schema expects Struct → msgspec raises ValidationError → wrapped
- Schema is `dict[str, X]` and file is empty → returns `{}`

**Tasks:**
- Add `msgspec>=0.19` to `shared/pyproject.toml`, run `uv sync`
- Rewrite `config.py` (remove old content, implement generic loader)
- Write `tests/test_config_loader.py`: valid load, defaults, nested structs,
  dict[str, Struct] schema, missing file, bad YAML, validation error, empty file

**Deliverable:** `load_config(path, schema)` works for arbitrary msgspec Struct schemas.

**Verify:** `uv run pytest tests/test_config_loader.py -v` — all pass.

---

### 2. Model catalog with code defaults + file overrides

**Approach:**
- Rewrite `shared/src/archie_shared/models.py`
- Define `CostConfig`, `ProviderConfig`, `ModelEntry` as msgspec Structs
- Port all current models to `DEFAULT_MODELS` dict with new key format:
  - `eu.anthropic.claude-opus-4-6-v1` → key `bedrock-claude-opus-4-6`, `provider.endpoint = "eu.anthropic.claude-opus-4-6-v1"`
  - `qwen3.6:35b` → key `ollama-qwen3-6-35b`, `provider.name = "ollama"`, `provider.endpoint = "localhost:11434"`
- `load_models(overrides_path=None)`: start with `DEFAULT_MODELS.copy()`, if path exists
  load via `load_config(path, dict[str, ModelEntry])` and `defaults.update(overrides)`
- `get_model(catalog, key)`: simple lookup with helpful KeyError
- `calculate_cost(cost, input_tokens, output_tokens, cache_read_tokens=0, cache_write_tokens=0)`:
  rates are per-million tokens

**Wiring:**
- Catalog is a plain dict — not a module-level singleton
- Agent `app.py` calls `load_models()` at startup, stores reference
- `get_model(catalog, key)` used to look up the active model

**Edge cases:**
- User `models.yaml` doesn't exist → defaults only, no error
- New key in user file → added to catalog
- Existing key in user file → fully replaces default entry
- Key not in catalog → `KeyError` listing available keys

**Tasks:**
- Define `CostConfig`, `ProviderConfig`, `ModelEntry` structs
- Port all models from current `MODELS` dict to new format
- Implement `load_models()`, `get_model()`, `calculate_cost()`
- Write `tests/test_models_catalog.py`: defaults, override merge, new model,
  missing model lookup, cost calculation

**Deliverable:** Model catalog loads defaults, merges user file, provides typed lookup.

**Verify:** `uv run pytest tests/test_models_catalog.py -v` — all pass.

---

### 3. Application config schema (`NexusConfig`)

**Approach:**
- New file `shared/src/archie_shared/schemas.py`
- `GlobalConfig`: `model: str = "bedrock-claude-sonnet-4-6"`, `project_root: str = "~/dev"`
- `AgentConfig`, `CliConfig`, `WebConfig`: empty for now (placeholder for future fields)
- `NexusConfig`: all sections optional, default to empty struct instances
- Handle `global` keyword: use msgspec field rename (`name="global"`)
- `load_nexus_config(path: Path | None = None) -> NexusConfig`:
  - If path given → load it (raise on missing)
  - Otherwise → `home_dir() / "config.yaml"` (dir from `ARCHIE_HOME_DIR`,
    default `~/.nexus`), return `NexusConfig()` if the file doesn't exist
- Path expansion for `project_root` via property or helper function

**Edge cases:**
- No config file, no env var → `NexusConfig()` with all defaults (valid)
- Explicit `path=` given, file missing → `ConfigError` (explicit path must exist)
- Empty file → all defaults
- Only `global:` section present → other sections get defaults

**Tasks:**
- Create `schemas.py` with config structs + `load_nexus_config()`
- Write `tests/test_nexus_config.py`: full config, partial, missing file,
  env var, defaults, project_root expansion

**Deliverable:** `load_nexus_config()` returns typed config with section access.

**Verify:** `uv run pytest tests/test_nexus_config.py -v` — all pass.

---

### 4. Integration — update consumers

**Approach:**
- Update `agent/src/archie_agent/app.py` lifespan:
  - Replace `from archie_shared.config import load_config` with `from archie_shared.schemas import load_nexus_config`
  - Replace `from archie_shared.models import get_model_info` with `from archie_shared.models import load_models, get_model`
  - `config = load_nexus_config()` → `config.global_.model` for active model key
  - `catalog = load_models(home_dir() / "models.yaml")` → `model = get_model(catalog, config.global_.model)`
    (this lookup is now the sole model-key validation — raises at startup; D3)
  - Pass `model.provider.endpoint` to BedrockClient (the inference profile ID)
  - Resolve region as `model.provider.region or config.global_.region` (D2 —
    `global.region` is the session default, replacing the old `config.region`)
  - Pass `model.can_cache` to BedrockClient to seed `_cache_supported` (D1)
- Update `BedrockClient.__init__` to accept `can_cache: bool` (no `True` default —
  matches catalog default of `False`; D1). Seed `self._cache_supported = can_cache`.
  **Retain** the runtime cachePoint-rejection fallback (bedrock.py:273-277) that
  flips `_cache_supported = False` when Bedrock rejects a cachePoint (D1).
- Update `agent/src/archie_agent/session.py`:
  - Replace `ModelInfo` with `ModelEntry`; rename the `model_info` attribute to
    `model` (D6)
  - Update `calculate_cost` calls to pass `model.cost` instead of the whole model
  - Replace `model_info.max_context_tokens` with `model.context`
  - Replace `model_info.context_warning_threshold` with `model.context_warning_threshold`
- Update `agent/src/archie_agent/agent.py`: `ModelInfo` → `ModelEntry`; rename
  `model_info`/`_model_info` param+attr to `model`/`_model` (D6; agent.py:62,67)
- Update `agent/src/archie_agent/app.py`: pass `model=` (not `model_info=`) to both
  `Session` and `AgentLoop` (app.py:67,80; D6)
- Update `cli/src/archie_cli/cli.py`:
  - Replace `ensure_default_config` with path resolution that creates a new-format default
    if no file exists. **The generated default must use the new model key**
    (`bedrock-claude-sonnet-4-6`), not the old dotted inference-profile ID (#5)
  - Docker mount: bind the whole home **directory** (`home_dir()`) at
    `/home/${USERNAME}/.nexus` (single dir mount, not per-file), and set container
    ENV `ARCHIE_HOME_DIR=/home/${USERNAME}/.nexus`. Replace the old
    `ARCHIE_CONFIG=/archie/config/nexus.yaml` env + file mount. **Leave the
    existing conditional `nexus.creds.yaml` file-mount block (cli.py ~263-272)
    untouched here** — plan 004 removes it and folds credentials into this dir
    mount (and manages `:ro`/`:rw`).
- Update `shared/src/archie_shared/__init__.py` re-exports
- Fix existing tests (`test_agent_loop.py`, `test_bedrock.py`, `test_ws_integration.py`)

**⚠️ Gotchas:**
- `BedrockClient` currently takes `model_id` which is the inference profile ID — now
  sourced from `model.provider.endpoint`, required for all models
- `Session.model_info` is renamed to `model` (type `ModelEntry`; D6) — multi-file
  rename touching session.py, agent.py:62,67, app.py:67,80
- Container config path is now `/home/${USERNAME}/.nexus/config.yaml` (dir mount +
  `ARCHIE_HOME_DIR`), replacing the old `/archie/config/nexus.yaml` file mount
- Old `~/.archie/nexus.yaml` with a dotted model key will `KeyError` at startup —
  expected (breaking change; delete old `~/.archie` & recreate under `~/.nexus`)

**Tasks:**
- Update `app.py` lifespan
- Update `BedrockClient.__init__` (add `can_cache` param)
- Update `session.py` (ModelEntry, `model` attr rename, calculate_cost signature)
- Update `agent.py` type annotations + `model` rename
- Update `cli.py` config handling (dir mount at `/home/${USERNAME}/.nexus` + set
  container ENV `ARCHIE_HOME_DIR`; see docker-run mount edit at cli.py ~260-270)
- Patch `credentials.py`: `ARCHIE_DIR` \u2192 `home_dir()` (line 23/25) so it still
  imports after the constant is removed (behaviour unchanged; superseded by 004)
- Update `shared/__init__.py` exports (drop `ModelInfo`/`get_model_info`; add
  `ModelEntry`/`load_models`/`get_model`/`load_nexus_config` etc.)
- Fix all existing tests \u2014 note `test_agent_loop.py:58-66` fixture uses
  `get_model_info(\"eu.anthropic.claude-sonnet-4-6\")` (feeds 4 tests via `model_info`
  fixture); rewrite to new key `bedrock-claude-sonnet-4-6` + catalog `get_model`
- Run `uv run ruff format && uv run ruff check && uv run pytest -v`

**Deliverable:** Agent starts correctly with new config/catalog. All tests pass, lint clean.

**Verify:** `uv run ruff check && uv run pytest -v` — clean lint, all tests pass.

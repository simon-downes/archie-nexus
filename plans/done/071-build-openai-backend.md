# 071 — Direct OpenAI API backend

Status: done
Branch: build-it/071-openai-backend (base: main @ e885114)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/071-openai-backend

## Objective

Archie can only reach OpenAI models through Bedrock, which does not yet offer the Daybreak
model `gpt-5.6-cyber`. Add a direct OpenAI Responses API backend, authenticated with an
OpenAI API key, and catalog GPT-6 Astra, GPT-6.1 Sol, GPT-6 Luna and GPT-5.6 Cyber on it.

## Context

The user asked to "implement openai as a backend. models should ideally be astra 6, sol 6.1,
luna 6 and gpt-5.6 cyber (daybreak)", and to check how `_research/pi` does OpenAI login. The
user wants a separate backend first and deduplication of shared Responses logic later.

Discovery:

- `agent/src/archie_agent/llm/bedrock_openai.py` already implements Responses API
  translation, explicit prompt-cache breakpoints (`prompt_cache_options` mode explicit, TTL
  30m), streaming and usage normalisation; only `__init__` (BedrockOpenAI client, token
  provider) and the log line are Bedrock-specific.
- `create_llm_client` in `agent/src/archie_agent/llm/__init__.py` dispatches on the tagged
  `ProviderConfig` union in `shared/src/archie_shared/models.py`; the catalog is code-defined
  (`DEFAULT_MODELS`).
- Every non-Bedrock credential comes from a plugin manifest's `credential` block
  (`plugins/aws/plugin.yaml` is a credential-only plugin). A static provider with an `env`
  mapping makes `archie auth login <id>` read those env vars.
- pi supports `OPENAI_API_KEY` and a "Sign in with ChatGPT" OAuth flow. The OAuth flow needs a
  fixed `127.0.0.1:<port>/auth/callback` loopback and drops `max_output_tokens` and cache
  options; it is out of scope (user Q1).
- OpenAI model pages (developers.openai.com/api/docs/models/<id>.md, fetched for this build):
  - gpt-6-astra: max input 922K, max output 128K; $10 / cached $1 / write $12.5 / out $50.
  - gpt-6.1-sol: max input 922K, max output 128K; $2 / $0.1 / $2.5 / $10.
  - gpt-6-luna: max input 922K, max output 128K; $0.1 / $0.01 / $0.125 / $0.5.
  - gpt-5.6-cyber: max input 272K, max output 128K; $12.5 / $1.25 / out $75; writes 1.25x
    input ($15.625). Requires Daybreak approval on the OpenAI org.
  - Prompts over 272K input: 2x input and cache rates, 1.5x output.
  - Prompt-caching guide: explicit breakpoints and `prompt_cache_options.ttl` "30m" are
    supported for GPT-5.6 and later.
- No OpenAI API key is available in this environment, so no live request can be made.

## Baseline

- Check commands: `env -u ARCHIE_PLUGINS_DIR -u ARCHIE_PERSONA_DIR uv run pytest -q`,
  `uv run ruff check <files>`, `uv run ruff format <files>`. The build container sets
  `ARCHIE_PLUGINS_DIR`/`ARCHIE_PERSONA_DIR` to the running agent's own install, so tests must
  run with them unset to see the repo's `plugins/`.
- Setup: `uv sync` in the worktree
- Setup leftovers: none
- Failing before this work (base e885114, env unset): 1 failed, 2087 passed —
  `tests/test_plugin_layout.py::test_persona_skills_holds_only_procedure_skills` (the committed
  `persona/skills/inbox-triage`). With the env vars set, 31 failed / 9 errors, all env-caused.
- Not included: `persona/skills/aws-agent-access/`, `plans/060-spec-container-aws-profiles.md`,
  `plans/070-build-nexus-plugins.md` (untracked in the user's checkout)

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Authentication | OpenAI API key | Sign in with ChatGPT OAuth | Fits the existing static-credential path; Daybreak access is per API org; OAuth needs a custom loopback flow | user Q1 |
| D2 | Client code shape | New `llm/openai_api.py` `OpenAIClient` importing the module-level helpers from `bedrock_openai.py`, with its own request/stream/invoke methods | Subclass `BedrockOpenAIClient`; full copy of the module | User wants a separate backend first; importing helpers keeps the duplication to the client methods (~130 lines) | self (user preference) |
| D3 | Credential location | Credential-only plugin `plugins/openai/plugin.yaml`, static, field `api_key`, `env: {api_key: OPENAI_API_KEY}`, `set_env: false` | Hand-written core provider like bedrock | Repo rule: credentials live in plugin manifests; bedrock is the single core exception (plan 070 D10) | self |
| D4 | Key resolution in the agent | Stored `openai.api_key`, falling back to the `OPENAI_API_KEY` env var; resolved lazily on first request | Resolve at construction and raise | `create_llm_client` runs at startup and on model switch; a raise there would break the agent instead of one request | self |
| D5 | Model keys and provider tag | Keys `openai-gpt-6-astra`, `openai-gpt-6-1-sol`, `openai-gpt-6-luna`, `openai-gpt-5-6-cyber`; provider struct `OpenAIProvider(tag="openai", model_id)` | Reuse `bedrock-openai` tag with a base URL | Key prefix drives `_infer_backend` in metrics; a separate tag keeps the dispatch explicit | self |
| D6 | `context` value | Max input tokens (922_000; 272_000 for cyber) | Full context window (1.05M / 400K) | `context` is used as the input limit for compaction and context-percent; output is reserved separately | self |
| D7 | Cyber long-context tier | Single rate, no >272K tier | Add the tier | Max input is 272K, so the tier can never apply | self |
| D8 | Client construction | Build the SDK client on the first request that finds a key and keep it; a request with no key builds nothing, so the next request re-reads the store | Re-read the store on every request; build once at init | `archie auth login openai` then takes effect without restarting after a no-key failure, without per-request store reads after that | self |
| D9 | Display names | Suffix ` (OpenAI)`: "GPT-6 Astra (OpenAI)", "GPT-6.1 Sol (OpenAI)", "GPT-6 Luna (OpenAI)", "GPT-5.6 Cyber (OpenAI)" | Same names as Bedrock entries | `DEFAULT_MODELS` already has "GPT-6 Luna" and "GPT-6.1 Sol"; pickers would show duplicates | self |

## Requirements

- MUST provide an `openai` static credential provider with field `api_key`, filled from
  `OPENAI_API_KEY` by `archie auth login openai`, and not exported into containers.
  - AC: `PROVIDERS["openai"]` is a `StaticProvider` with `fields == ["api_key"]`,
    `env == {"api_key": "OPENAI_API_KEY"}`, `set_env is False`.
- MUST route catalog entries with `OpenAIProvider` to an OpenAI client that calls
  `https://api.openai.com/v1` Responses with the stored API key.
  - AC: `create_llm_client` on `openai-gpt-6-luna` returns `OpenAIClient`; its SDK client has
    base URL `https://api.openai.com/v1` and the stored key.
- MUST send the same Responses request as the Bedrock OpenAI client: developer message,
  explicit cache breakpoints and `prompt_cache_options` when caching, `store: false`,
  `max_output_tokens`, tools, and image parts when the model can see.
  - AC: request-kwargs tests for `OpenAIClient` assert these fields.
- MUST stream text, tool calls, usage and stop reasons the same way as the Bedrock OpenAI
  client, and retry once without cache metadata on a 4xx cache validation error.
  - AC: stream tests with fake SDK events yield `TextDelta`, `ToolUseStart`, `ToolUseEvent`,
    `Usage`, `Done` as expected; the cache-fallback test retries without cache fields.
- MUST fail a request, not agent startup or model switch, when no API key is available, with
  a message naming `archie auth login openai`.
  - AC: constructing `OpenAIClient` with no credential and no env var succeeds; iterating
    `stream(...)` and calling `invoke(...)` each raise `RuntimeError` whose message contains
    `archie auth login openai`; after a key is stored, the next request succeeds.
- MUST prefer the stored `openai.api_key` over `OPENAI_API_KEY`.
  - AC: with both set, the SDK client is built with the stored key.
- MUST ship `plugins/openai/README.md` and a `plugins/README.md` row, as every plugin does.
  - AC: `uv run pytest -q tests/test_plugin_layout.py` passes.
- MUST catalog the four models with the limits and prices in Context: `max_output_tokens =
  128_000`, `can_cache = True`, `can_see = True`, `cache_ttl_seconds = 1800`, names per D9.
  - AC: catalog tests assert model ID, name, context, max output, `can_cache`, `can_see`,
    `cache_ttl_seconds` and rates for each key.
- MUST record `backend = "openai"` in metrics for `openai-` model keys.
  - AC: `_infer_backend("openai-gpt-6-luna") == "openai"`.
- MUST document the provider and models in `docs/auth-credentials.md` (Supported providers
  table, Startup environment projection table, and a note that requests go to OpenAI rather
  than staying in AWS) and `docs/architecture.md` (the backend list).
  - AC: both files mention the `openai` provider / OpenAI API backend.

Out of scope: Sign in with ChatGPT OAuth; configurable base URL, organisation or project
headers; reasoning-effort settings; deduplicating the Responses logic; GPT-5.6 Sol or other
OpenAI models; changing the default model.

## Design

### Overview

A credential-only `openai` plugin supplies the API key. A new `OpenAIClient` reuses the
Bedrock OpenAI module's Responses helpers with a standard `openai.OpenAI` SDK client, and the
catalog gains four `OpenAIProvider` entries that `create_llm_client` routes to it.

### Stack

- `openai` Python SDK — reused (already a dependency; `BedrockOpenAI` comes from it). Use
  `openai.OpenAI(api_key=..., base_url="https://api.openai.com/v1", max_retries=2)`.

### Code structure

- `plugins/openai/plugin.yaml` (new): `name: openai`, `credential: {kind: static, fields:
  [api_key], env: {api_key: OPENAI_API_KEY}, set_env: false}` (explicit, as in
  `plugins/aws/plugin.yaml`).
- `plugins/openai/README.md` (new): follow `plugins/scalr/README.md` (credential-only plugin;
  names `api_key`, links `../../docs/plugins.md`). Add a row to `plugins/README.md`.
  `docs/config.md` is not changed: the plugin has no settings or policy.
- `agent/src/archie_agent/llm/openai_api.py` (new): `OpenAIClient` with `__init__(model_id,
  max_output_tokens, can_cache, can_see)`, lazy `_client()`, `_request_kwargs`,
  `_create_with_cache_fallback`, `stream`, `invoke`. Imports `_turns_to_responses_input`,
  `_tool_config_to_responses`, `_apply_conversation_boundary`, `_developer_message`,
  `_remove_cache_metadata`, `_is_cache_validation_error`, `_response_usage`,
  `prompt_cache_key`, `_CACHE_OPTIONS` from `bedrock_openai.py`.
- `agent/src/archie_agent/llm/__init__.py`: add `OpenAIProvider` case and export.
- `shared/src/archie_shared/models.py`: `OpenAIProvider`, union member, four entries.
- `shared/src/archie_shared/__init__.py`: export `OpenAIProvider`.
- `orchestrator/src/archie_orchestrator/metrics.py`: `_infer_backend` `openai-` → `openai`.
- Tests: new `tests/test_openai_api.py` (follow `tests/test_bedrock_openai.py`), updates to
  `tests/test_models_catalog.py`, `tests/test_providers.py`,
  `tests/test_credentials_models.py`, `tests/test_orchestrator_credentials.py`,
  `tests/test_orchestrator_metrics.py`.

### Error handling

- Malformed stored `openai` entry → the store's `ValueError` propagates at request time
  (no env fallback), so a broken credential is visible rather than silently replaced.
- No API key → `RuntimeError("No OpenAI API key: run `archie auth login openai` or set
  OPENAI_API_KEY")` at request time; the agent loop already reports request exceptions.
- 4xx cache validation error → one retry without cache metadata, caching disabled for the
  client (same as Bedrock).
- Other SDK errors → propagate unchanged (SDK retries twice).

### Non-functional

- The API key is read from the 0600 credential store or the env, passed only to the SDK, and
  never logged.

## Milestones

### [x] M1 — Requests to one OpenAI model work end to end

- **Approach:** Add the plugin manifest and README (+ `plugins/README.md` row),
  `OpenAIProvider`, `OpenAIClient`, factory case, and the `openai-gpt-6-luna` catalog entry
  with tiered rates following `bedrock-openai-gpt-6-luna` in `shared/src/archie_shared/models.py`
  (`criteria=("total_input", "min", 0)` and `("total_input", "min", 272_001)`). Update the
  provider-set tests and, in `tests/test_models_catalog.py`, `test_default_models_all_have_required_fields`
  (provider isinstance), `test_default_model_key_format` (allow `openai-`) and
  `test_cache_ttl_matches_cache_capability` (OpenAIProvider → 1800). Follow
  `bedrock_openai.py` for the client methods; read the key with
  `archie_shared.credentials.get_credential("openai")` then `os.environ` (D4, D8).
- **Test seam:** `create_llm_client`, `OpenAIClient.stream`/`invoke`/`_request_kwargs` with
  a fake SDK client; `PROVIDERS`/`CREDENTIAL_TYPES`.
- **Edge cases:** no key → request-time error; stored key wins over env; cache validation
  4xx → retry without cache fields.
- **Deliverable:** selecting `openai-gpt-6-luna` builds an OpenAI client that sends the
  expected Responses request and parses the stream.
- **Verify:** `uv run pytest -q tests/test_openai_api.py tests/test_providers.py
  tests/test_credentials_models.py tests/test_orchestrator_credentials.py
  tests/test_models_catalog.py tests/test_plugin_layout.py` passes.
- **Outcome:** Added `plugins/openai/` (manifest, README, `plugins/README.md` row),
  `OpenAIProvider`, `agent/src/archie_agent/llm/openai_api.py` (`OpenAIClient`), factory case,
  and `openai-gpt-6-luna`. New `tests/test_openai_api.py` (11 tests: factory, stored-over-env
  key, env fallback, missing key on `stream`/`invoke` then recovery, request shape with cache,
  tools and images, no-cache shape, stream mapping, max_tokens, cache fallback, invoke).
  `test_providers.py::EXPECTED_PROVIDERS` snapshot gained `openai`. Verify command: 117 passed,
  1 failed (the baseline `inbox-triage` failure). Full suite: 1 failed (baseline), 2100 passed.
  Ruff check/format clean on touched files.

### [x] M2 — Full model set, metrics and docs

- **Approach:** Add the Astra and 6.1 Sol entries with tiered rates (same pattern as M1;
  over-272K rates 2x input/cache, 1.5x output) and Cyber with a single rate (D7); update
  `_infer_backend` and change the existing `_infer_backend("openai-gpt-4o") is None`
  assertion in `tests/test_orchestrator_metrics.py`; document per the docs requirement.
- **Test seam:** `DEFAULT_MODELS`, `MetricsWriter._infer_backend`.
- **Deliverable:** four OpenAI models are selectable and priced; metrics tag them `openai`.
- **Verify:** `uv run pytest -q tests/test_models_catalog.py tests/test_orchestrator_metrics.py`
  passes; full suite shows no new failures.
- **Outcome:** Added `openai-gpt-6-astra`, `openai-gpt-6-1-sol` (tiered) and
  `openai-gpt-5-6-cyber` (single rate, cache write $15.625). `test_openai_api_catalog_entries`
  asserts all four and that no other `openai-` keys exist. `_infer_backend` maps `openai-` →
  `openai`. Docs: `docs/auth-credentials.md` (provider table, env-projection table, OpenAI
  paragraph incl. data leaving AWS and Daybreak approval), `docs/architecture.md` (topology
  and provider list). Verify: 48 passed. Full suite: 1 failed (baseline), 2100 passed.

## Deviations

## Risks

- No live OpenAI request was possible here; request shape is verified against the Bedrock
  client's tested behaviour and OpenAI docs only.
- `gpt-5.6-cyber` explicit-cache support is inferred from "GPT-5.6 and later"; the cache
  fallback covers a rejection.
- Using the OpenAI API sends prompts outside AWS; data-handling approval may apply.
- pr.py mismatch: local `main` (base e885114) is 7 commits ahead of `origin/main`, so the PR
  diff also contains those unpushed, unreviewed-by-this-build commits (4eb4f86..e885114).
  Noted in the PR body.

## Review

- Plan review: 1 pass. Fixed both ❌ (missing plugin README/`plugins/README.md` row; catalog test updates moved into M1) and every ⚠️/ℹ️: generator-raise AC, key-precedence AC, `test_plugin_layout.py` in Verify, `can_cache`/`can_see`/128_000 stated, display names (D9), client caching rule (D8), docs locations named, explicit `set_env`, metrics assertion flip, `docs/config.md` skip reason, malformed-credential handling, tiered-rate pattern named. No rejections.

- Code review: 1 round, roles `general` and `risk` (risk run: new credential, secret
  handling, `ProviderConfig` union is a cross-package contract). `risk`: no findings.
  `general`: 4 ℹ️ + 1 ❓, all agreed and fixed — key-precedence and recovery tests now go
  through `stream`/`invoke` with the `OpenAI` constructor patched (no `_sdk()` calls); cache
  fallback test asserts a later request also has no cache metadata instead of reading
  `_cache_enabled`; added tests for `ResponseFailedEvent` (raises, stream closed), argument
  deltas with malformed JSON (`input_truncated`), and malformed stored entry → `ValueError`
  with no env fallback; `docs/plugins.md` code-free plugin list gains `openai`. ❓ (disabled
  plugin still authenticates): intended — the agent's Bedrock path doesn't check `enabled`
  either; documented in `plugins/openai/README.md`. No rejections.
- Spec check: each MUST traced — provider (`test_openai_reads_api_key_from_env_and_is_not_exported`),
  routing and endpoint (`test_factory_routes_openai_provider_to_openai_client`,
  `test_sdk_client_uses_stored_key_and_public_endpoint`), request shape
  (`test_request_shape_with_cache_tools_and_images`, `test_request_without_cache_has_no_cache_metadata`),
  stream/fallback (`test_stream_maps_*`, `test_incomplete_*`, `test_cache_validation_*`,
  `test_failed_event_*`), missing key (`test_missing_key_fails_the_request_not_construction`),
  precedence (`test_sdk_client_uses_stored_key_*`), README (`tests/test_plugin_layout.py`),
  catalog (`test_openai_api_catalog_entries`), metrics (`test_infer_backend`), docs (diff).
- Merge risk: Low: the change adds new opt-in models and a provider; existing models,
  default model and Bedrock clients are untouched, and a missing or wrong key fails only
  requests to `openai-*` models.

## Outcome

Delivered a direct OpenAI Responses backend: `plugins/openai` credential (API key from
`OPENAI_API_KEY`), `OpenAIClient` in `agent/src/archie_agent/llm/openai_api.py`, and four
catalog models (`openai-gpt-6-astra`, `openai-gpt-6-1-sol`, `openai-gpt-6-luna`,
`openai-gpt-5-6-cyber`), with metrics backend `openai` and docs. Evidence:
`env -u ARCHIE_PLUGINS_DIR -u ARCHIE_PERSONA_DIR uv run pytest -q` → 1 failed (baseline
`inbox-triage` layout test), 2103 passed; ruff check/format clean on touched files.
Not verified: a live request to api.openai.com (no key in this environment), and that the
Daybreak org accepts explicit cache breakpoints for `gpt-5.6-cyber`.

PR: https://github.com/simon-downes/archie-nexus/pull/9
CI: pending

## Follow-ups

- Sign in with ChatGPT OAuth login (pi: `packages/ai/src/auth/oauth/openai-chatgpt.ts`).
- Deduplicate the Responses client logic shared by `bedrock_openai.py` and `openai_api.py`.

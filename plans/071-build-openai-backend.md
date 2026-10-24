# 071 — Direct OpenAI API backend

Status: planning
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

- Check commands: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check <files>`
- Setup: `uv sync` in the worktree
- Setup leftovers: none
- Failing before this work: `uv run pytest -q` → 31 failed, 9 errors, 2048 passed
  (mainly `tests/test_ws_integration.py`, `tests/test_native.py::...every_plugin_namespace`,
  plugin `test_runner_exposes_*_namespace`)
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
  - AC: constructing `OpenAIClient` with no credential and no env var succeeds; calling
    `stream` raises `RuntimeError` whose message contains `archie auth login openai`.
- MUST catalog the four models with the limits and prices in Context.
  - AC: catalog tests assert model ID, context, max output, `can_cache`, `can_see`,
    `cache_ttl_seconds == 1800` and rates for each key.
- MUST record `backend = "openai"` in metrics for `openai-` model keys.
  - AC: `_infer_backend("openai-gpt-6-luna") == "openai"`.
- MUST document the provider and models in `docs/auth-credentials.md` and
  `docs/architecture.md`.
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
  [api_key], env: {api_key: OPENAI_API_KEY}}`. Follow `plugins/aws/plugin.yaml`.
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

- No API key → `RuntimeError("No OpenAI API key: run `archie auth login openai` or set
  OPENAI_API_KEY")` at request time; the agent loop already reports request exceptions.
- 4xx cache validation error → one retry without cache metadata, caching disabled for the
  client (same as Bedrock).
- Other SDK errors → propagate unchanged (SDK retries twice).

### Non-functional

- The API key is read from the 0600 credential store or the env, passed only to the SDK, and
  never logged.

## Milestones

### [ ] M1 — Requests to one OpenAI model work end to end

- **Approach:** Add the plugin manifest, `OpenAIProvider`, `OpenAIClient`, factory case, and
  the `openai-gpt-6-luna` catalog entry. Update the provider-set tests. Follow
  `bedrock_openai.py` for the client methods; read the key with
  `archie_shared.credentials.get_credential("openai")` then `os.environ`.
- **Test seam:** `create_llm_client`, `OpenAIClient.stream`/`invoke`/`_request_kwargs` with
  a fake SDK client; `PROVIDERS`/`CREDENTIAL_TYPES`.
- **Edge cases:** no key → request-time error; stored key wins over env; cache validation
  4xx → retry without cache fields.
- **Deliverable:** selecting `openai-gpt-6-luna` builds an OpenAI client that sends the
  expected Responses request and parses the stream.
- **Verify:** `uv run pytest -q tests/test_openai_api.py tests/test_providers.py
  tests/test_credentials_models.py tests/test_orchestrator_credentials.py
  tests/test_models_catalog.py` passes.
- **Outcome:**

### [ ] M2 — Full model set, metrics and docs

- **Approach:** Add the Astra, 6.1 Sol and Cyber entries with tiered rates (Cyber single
  rate, D7); update `_infer_backend`; update `test_default_model_key_format` and the
  cache-TTL test; document the provider in `docs/auth-credentials.md` (provider table,
  data leaves AWS) and the backend in `docs/architecture.md`.
- **Test seam:** `DEFAULT_MODELS`, `MetricsWriter._infer_backend`.
- **Deliverable:** four OpenAI models are selectable and priced; metrics tag them `openai`.
- **Verify:** `uv run pytest -q tests/test_models_catalog.py tests/test_orchestrator_metrics.py`
  passes; full suite shows no new failures.
- **Outcome:**

## Deviations

## Risks

- No live OpenAI request was possible here; request shape is verified against the Bedrock
  client's tested behaviour and OpenAI docs only.
- `gpt-5.6-cyber` explicit-cache support is inferred from "GPT-5.6 and later"; the cache
  fallback covers a rejection.
- Using the OpenAI API sends prompts outside AWS; data-handling approval may apply.

## Review

## Outcome

PR: pending
CI: pending

## Follow-ups

- Sign in with ChatGPT OAuth login (pi: `packages/ai/src/auth/oauth/openai-chatgpt.ts`).
- Deduplicate the Responses client logic shared by `bedrock_openai.py` and `openai_api.py`.

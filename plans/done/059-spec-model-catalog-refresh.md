# 059 — Model Catalog Refresh and Inference Scope Selection

## Objective

Refresh Archie Nexus's default Bedrock model catalog to the requested current model versions, remove superseded entries, and use the best available inference scope per model. Scope preference is global first, EU second, and US third; pricing must match the selected Bedrock inference profile rather than a different provider or geography.

GPT-5.6 Luna remains the current default model and must not be renamed, removed, or repriced by this change.

## Context

The catalog in `shared/src/archie_shared/models.py` currently contains older Claude, Kimi, and OpenAI entries, including EU Anthropic profiles and superseded GPT Sol/Terra models. The rate-list pricing API from plan 058 already supports request-level short/long tiers and four billable token categories. This work is a catalog-only migration: it preserves the shared pricing API and runtime accounting semantics while changing built-in model keys, provider IDs, regions, and prices. User-supplied `models.yaml` catalog loading is intentionally removed; the built-in catalog is authoritative.

The requested replacements are:

- Opus 5.5
- Sonnet 5.5
- Fable 5.1
- Kimi K3
- Sol 6.1

The user explicitly requested removal of previous Opus, Fable, and Kimi versions; removal of Sol 5.6, Sol 6, and Terra 5.6; retention of GPT-6 Terra as the latest Terra version; and retention of GPT-5.6 Luna as the current default.

Authoritative sources verified:

- [Claude Opus 5.5 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-opus-5-5.html): global profile `global.anthropic.claude-opus-5-5`, 1M context, 128K output, global scope available.
- [Claude Sonnet 5.5 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-5-5.html): global profile `global.anthropic.claude-sonnet-5-5`, 1M context, 128K output, global scope available.
- [Claude Fable 5.1 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-fable-5-1.html): global profile `global.anthropic.claude-fable-5-1`, 1M context, 128K output, global scope available.
- [Kimi K3 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-moonshot-ai-kimi-k3.html): global profile `global.moonshotai.kimi-k3`, 1M context, global standard rates `$3/$15/$0.30/$3.75`.
- [GPT-6.1 Sol model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-1-sol.html): no global or EU profile; runtime profile `us.openai.gpt-6.1-sol`, 1M context, 131,072 max output, US CRIS rates `$2.20/$11/$0.11/$2.75` short and `$4.40/$16.50/$0.22/$5.50` long.
- [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing): standard token rates for Opus 5.5 `$4/$20/$0.20/$5`, Sonnet 5.5 `$2/$10/$0.20/$2.50`, and Fable 5.1 `$10/$50/$0.25/$12.50`.

In every rate tuple below, values are ordered as `input, output, cache_read, cache_write`, matching `Rate` fields. Anthropic global profiles use the global/standard rates. Kimi K3 has no tier threshold in this catalog because its AWS card provides one global rate. GPT-6.1 Sol uses the existing 272,001 threshold convention; long rates apply to the full request above 272,000 input tokens.

## Requirements

### Catalog membership

- MUST add exactly these five replacement catalog entries (in addition to retained unrelated models):
  - `bedrock-claude-opus-5-5`
  - `bedrock-claude-sonnet-5-5`
  - `bedrock-claude-fable-5-1`
  - `bedrock-kimi-k3`
  - `bedrock-openai-gpt-6-1-sol`
- MUST remove these superseded entries:
  - `bedrock-claude-fable-5`
  - `bedrock-claude-opus-4-6`
  - `bedrock-claude-opus-4-8`
  - `bedrock-kimi-k2-5`
  - `bedrock-openai-gpt-5-6-sol`
  - `bedrock-openai-gpt-5-6-terra`
  - `bedrock-openai-gpt-6-sol`
- MUST retain `bedrock-openai-gpt-5-6-luna` unchanged as the default model.
- MUST retain `bedrock-openai-gpt-6-terra` as the latest Terra version.
- MUST retain unrelated catalog entries, including existing Sonnet 4.6, Haiku, GLM, Qwen, and other non-requested models.

### Inference profiles and scope

- Opus 5.5 MUST use `global.anthropic.claude-opus-5-5`.
- Sonnet 5.5 MUST use `global.anthropic.claude-sonnet-5-5`.
- Fable 5.1 MUST use `global.anthropic.claude-fable-5-1` and its catalog documentation MUST state the AWS `aws_review` Data Retention API opt-in prerequisite.
- Kimi K3 MUST use `global.moonshotai.kimi-k3`; it has no documented EU profile, so the preference order is global, then US for Kimi if global is unavailable.
- Sol 6.1 MUST use `us.openai.gpt-6.1-sol`; AWS does not offer a global or EU profile for this launch.
- New Anthropic and Kimi entries MUST use `region=None` unless an existing provider convention requires a source region; Sol 6.1 MUST retain `region="us-east-1"` for the US runtime profile.

### Pricing

- Opus 5.5 MUST use one fallback rate with `(4.00, 20.00, 0.20, 5.00)`.
- Sonnet 5.5 MUST use one fallback rate with `(2.00, 10.00, 0.20, 2.50)`.
- Fable 5.1 MUST use one fallback rate with `(10.00, 50.00, 0.25, 12.50)`.
- Kimi K3 MUST use one fallback rate with `(3.00, 15.00, 0.30, 3.75)`.
- Sol 6.1 MUST use a short rate with criteria `("total_input", "min", 0)` and `(2.20, 11.00, 0.11, 2.75)`.
- Sol 6.1 MUST use a long rate with criteria `("total_input", "min", 272_001)` and `(4.40, 16.50, 0.22, 5.50)`.
- Pricing fields MUST remain expressed as USD per million tokens and preserve separate cache-read/cache-write categories.

### Metadata and compatibility

- New Anthropic and Kimi entries MUST preserve the 1,000,000-token context and caching capability documented by AWS.
- New Opus, Sonnet, and Fable entries MUST use `max_output_tokens=128_000`.
- New Kimi K3 MUST retain the repository's conservative `max_output_tokens=16_000` because the AWS card does not establish a higher limit.
- New Sol 6.1 MUST use `context=1_000_000`, `max_output_tokens=131_072`, and `can_cache=False`.
- The global default in `shared/src/archie_shared/schemas.py` MUST remain `bedrock-openai-gpt-5-6-luna`.
- Existing runtime accounting, model switching, historical event costs, and the rate-selection API MUST remain unchanged.
- `load_models()` MUST return the validated built-in catalog and MUST no longer read or merge `models.yaml`; retain a compatible optional path parameter only if needed by existing callers, but ignore it.
- `agent/src/archie_agent/app.py` MUST stop constructing or passing a `models.yaml` path when loading the catalog.
- Existing `models.yaml` files MUST be ignored rather than partially applied or treated as model overrides.
- A configured `global.model` value that names a removed model MUST fail clearly during model lookup; configuration MUST NOT be silently rewritten.

## Technical Design

Modify the built-in catalog in `shared/src/archie_shared/models.py`, remove the `models.yaml` merge path from `load_models()`, and update catalog-facing tests and documentation. Continue using `BedrockProvider` for Anthropic/Kimi and `BedrockOpenAIProvider` for Sol 6.1. Set Sol 6.1 `can_cache=False` because AWS does not support explicit prompt caching for that model. Retain Kimi K3's conservative `max_output_tokens=16_000` because the AWS card does not establish a higher limit. Keep the existing Converse adapter for Kimi K3; migrating Kimi to the OpenAI-compatible API is deferred and outside this catalog change. Represent each non-tiered replacement with `CostConfig(rates=[Rate(...)])`; represent Sol 6.1 with two threshold rates using the established 272,001 boundary.

The catalog key names are stable application-facing identifiers for the new versions. Provider `model_id` values are the exact AWS inference profile IDs. No new provider type, dynamic endpoint negotiation, pricing engine, or fallback-at-runtime behavior is introduced. The scope preference is applied during catalog authoring because AWS availability is known per model; it is not a runtime failover mechanism. Existing `config.yaml` values naming removed models fail clearly at model lookup rather than being rewritten. The existing total-input calculation, including cache-read and cache-write categories, is retained for tier selection.

Tests must assert catalog membership, removed keys, exact provider IDs and regions, context/output metadata, exact pricing tuples, Sol boundary selection at 272,000 versus 272,001, and unchanged Luna default configuration. Existing catalog-wide validation tests must continue to validate every returned model.

## Milestones

### Milestone 1 — Catalog membership and replacement metadata

**Approach**

Update `DEFAULT_MODELS` in `shared/src/archie_shared/models.py` using the existing `ModelEntry`, provider, and rate-list patterns. Remove only the explicitly superseded keys and add the five replacement entries with the AWS profile IDs and metadata in the Requirements. Do not alter the default model setting or unrelated entries. Test through `DEFAULT_MODELS`, `load_models()`, and the public config defaults.

**Tasks**

- Remove the seven explicitly superseded catalog entries.
- Add Opus 5.5, Sonnet 5.5, Fable 5.1, Kimi K3, and Sol 6.1.
- Set global profiles for the first four replacements and US CRIS for Sol 6.1.
- Set context windows, cache capability, and max output metadata from the AWS cards; set Sol 6.1 `can_cache=False` and retain Kimi K3 at 16,000 max output tokens.
- Remove the `models.yaml` merge path from `load_models()`, update `agent/src/archie_agent/app.py` to use the built-in catalog path, and replace contradictory override/addition/path/legacy-cost tests with a test proving an existing `models.yaml` is ignored.
- Add tests for exact retained and removed keys, provider IDs, regions, metadata, Fable's prerequisite documentation, and Luna default preservation.

**Deliverable**

The default catalog contains the requested current versions and no explicitly removed legacy entries, with GPT-5.6 Luna still selected by default.

**Verify**

Run `uv run pytest tests/test_models_catalog.py tests/test_nexus_config.py -q` and inspect the catalog assertions for exact key/profile/default membership.

### Milestone 2 — Scope-correct pricing and tier boundaries

**Approach**

Encode the verified global standard rates for Anthropic/Kimi and the US CRIS rates for Sol 6.1 using `Rate`. Keep Sol's request-level pricing behavior and existing threshold boundary. Add independent literal expected-value tests so pricing assertions do not derive expected values from catalog implementation.

**Edge Cases**

- Sol request at exactly 272,000 total input tokens → short US CRIS rate.
- Sol request at 272,001 total input tokens → long US CRIS rate for all four categories.
- Kimi cache categories → one global fallback rate prices input, output, cache-read, and cache-write independently.
- No global Sol profile → US profile and US pricing are used; do not use global base pricing.

**Tasks**

- Add exact fallback rates for Opus 5.5, Sonnet 5.5, Fable 5.1, and Kimi K3.
- Add short/long US CRIS rates for Sol 6.1.
- Test every replacement rate tuple and criteria tuple.
- Test `get_cost()` for Sol at both threshold boundaries and verify the selected rate applies to all token categories.
- Confirm catalog-wide `CostConfig.validate()` succeeds.

**Deliverable**

Every replacement model reports pricing for the selected inference scope, and Sol 6.1 calculates correct request-level costs at the context boundary.

**Verify**

Run `uv run pytest tests/test_models_catalog.py -q` and `uv run ruff check shared/src/archie_shared/models.py tests/test_models_catalog.py`.

### Milestone 3 — Consumer regression and complete verification

**Approach**

Verify that model switching, TUI discovery, runtime accounting, and historical event semantics consume the catalog without assumptions about the removed keys or old model IDs. Do not change runtime accounting because pricing continues through the existing `CostConfig` API. Update only assertions or fixtures that intentionally refer to removed catalog entries.

**Tasks**

- Search for all removed model keys and old provider IDs across source, tests, docs, and defaults.
- Replace stale catalog tests that expect removed keys or `models.yaml` behavior; preserve historical-event fixtures that intentionally use old model IDs.
- Document Fable 5.1's `aws_review` Data Retention API prerequisite wherever the built-in catalog's model notes are maintained.
- Verify TUI discovery displays the new models without flat-field assumptions.
- Run focused model-switch, harness, session, task-tool, and canonical event tests.
- Run the complete test and lint suites; report unrelated baseline failures separately.

**Deliverable**

The refreshed catalog integrates cleanly with model selection and accounting, with no accidental references to removed defaults.

**Verify**

Run `uv run pytest tests/test_models_catalog.py tests/test_model_switch.py tests/test_task_tool.py tests/test_native_dispatch.py tests/test_harness.py tests/test_canonical_session_events.py tests/test_session.py -q`, then `uv run pytest -q` and `uv run ruff check .`.

## Risks and Assumptions

- Global inference can route worldwide; this is intentional and follows the user's scope preference. Organizations with residency restrictions must change the built-in catalog or deployment configuration; `models.yaml` is no longer supported.
- AWS's GPT-6.1 Sol card explicitly offers no global or EU profile, so US CRIS is the highest available scope for that model.
- Anthropic's official pricing page supplies the standard token rates; the implementation must verify the matching AWS Bedrock global pricing before encoding rates, while regional profiles would carry a premium. This plan selects global profiles for the Anthropic models.
- The catalog does not implement automatic retry/fallback between inference profiles if a profile is unavailable in the caller's source Region.
- Historical events retain their persisted model key and `cost_usd`; removing a catalog entry does not reprice or rewrite historical records.

## Status

- [ ] Milestone 1 — Catalog membership and replacement metadata
- [ ] Milestone 2 — Scope-correct pricing and tier boundaries
- [ ] Milestone 3 — Consumer regression and complete verification

## Post-implementation fixes

A validation pass against the Bedrock model cards and pricing found that this plan's assumptions had drifted from current AWS data. The plan is now superseded where it conflicts with the following changes. `BedrockOpenAIClient` now targets the `bedrock-runtime` OpenAI endpoint instead of the SDK default `bedrock-mantle`, which only accepts bare model IDs. As a result, GPT-5.6 Luna, GPT-6 Luna and GPT-6.1 Sol now use `global.openai.*` profiles with Global CRIS pricing. Sol 6.1 also enables caching, which its card documents. GPT-6 Terra had no Bedrock card or pricing and was billed at $0, so it was replaced by `bedrock-openai-gpt-5-6-terra` (`global.openai.gpt-5.6-terra`, global rates). Fable 5.1 uses an assumed global rate 10% below the published Geo/In-region price (`10/50/0.25/12.50`), pending confirmation. Qwen3 Coder Next now uses its eu-west-2 (London) price (`0.78/1.86`). Every Bedrock catalog entry links to its model card. Catalog tests now assert the provider IDs and regions, literal per-scope rates, and Sol's tier boundary.

# Bedrock global inference catalog migration

## Status

Spec-level plan — independent of plan 032. Requires an AWS model/profile/pricing
verification checkpoint before catalog values are changed.

## Objective

Update archie-nexus's Bedrock model catalog and provider clients to use AWS global
inference profiles where supported, with the correct global model IDs, endpoint
semantics, context capabilities, prompt-cache behavior, and pricing. Models that
do not support global inference MUST remain explicitly regional/geographic or be
excluded with a documented reason.

## Context

The catalog is defined in `shared/src/archie_shared/models.py`. Bedrock entries
currently contain regional or geography-prefixed model IDs and some explicit
regions. `agent/src/archie_agent/llm/__init__.py` passes the selected/provider
region to both clients.

`BedrockClient` uses a regional `bedrock-runtime` boto3 client and sends the catalog
ID as Converse `modelId`. `BedrockOpenAIClient` uses the OpenAI-compatible Bedrock
client and sends the catalog ID as the Responses `model`.

The requested change is not a simple string replacement: AWS global inference
support, IDs, source-region availability, endpoint/API support, cache support, and
prices differ by model. Global inference can route prompts across commercial AWS
Regions and may require IAM/SCP permissions in destination Regions.

Historical `llm_request.cost_usd` values are authoritative and MUST NOT be
repriced after this change. Updated catalog prices apply only to new requests.

AWS sources to verify at implementation time:

- AWS, “Supported Regions and models for inference profiles”.
- The current AWS model-card page for every catalog Bedrock model.
- The current AWS pricing table for each selected global profile.

The Luna model card currently documents `global.openai.gpt-5.6-luna` for
`bedrock-runtime`, with 1M context and global pricing of $0.20 input, $1.20 output,
$0.02 cache read, and $0.25 cache write per million tokens; verify these values
before implementation because AWS pricing and availability can change.

## Requirements

### 1. Build and approve the model matrix

Before editing catalog values, produce a checked-in implementation note or plan
appendix containing one row for every Bedrock catalog entry:

- canonical model key and display name;
- provider/client (`BedrockClient` Converse or `BedrockOpenAIClient` Responses);
- current model ID and region;
- global inference profile ID, if supported;
- supported source regions and whether the configured region remains relevant;
- supported endpoint/API;
- context window and max output;
- global input, output, cache-read, and cache-write prices;
- prompt-caching support and the meaning of each usage category;
- required IAM actions and model-specific caveats;
- explicit disposition: migrate globally, retain regional/geo, or exclude.

The matrix MUST use current AWS documentation rather than assumptions. Do not
label a model global merely because a similarly named profile exists.

### 2. Update the catalog

For entries approved for global inference:

- Update `DEFAULT_MODELS` to the verified global profile ID.
- Update prices to the verified global prices.
- Set provider `region` to `None` when the model ID is globally selected and the
  configured region is only a source-region fallback; retain a valid source region
  when the SDK/endpoint requires it.
- Preserve each model's verified context, max-output, cache capability, and
  cache-price fields.
- For unsupported models, retain the current regional/geographic ID and document
  the reason; do not silently convert them.

Luna MUST use the verified runtime global profile ID rather than the base in-region
ID. Confirm whether the default source region remains suitable for the global
profile.

### 3. Update provider construction and requests

- Keep the boto3 runtime client source region as a valid AWS Region. Never use a
  fictional `global` region as the SDK region.
- Ensure `BedrockClient` sends the global profile ID as Converse `modelId`.
- Verify the OpenAI-compatible wrapper's endpoint and authentication behavior for
  global runtime inference. Ensure `BedrockOpenAIClient` sends the correct global
  profile ID as `model` and uses the documented runtime base URL/API.
- If the current OpenAI wrapper cannot target the required endpoint, introduce the
  smallest explicit provider configuration needed. Do not silently change endpoint
  families or authentication semantics.
- Preserve region fallback behavior for entries that remain regional or require a
  source Region.
- Document IAM permissions, SCP requirements, and data-routing implications for
  global inference.

### 4. Preserve accounting semantics

- Historical `llm_request` records MUST retain their stored `cost_usd` values.
- New requests MUST use the updated catalog prices.
- Cache-read and cache-write prices MUST be non-zero only where the selected model
  and API actually report/support those categories.
- Do not change the canonical event accounting contract solely to perform this
  migration.

### 5. Tests and verification

Add tests for:

- every migrated catalog entry's profile ID, pricing, context, and cache flags;
- every retained regional/geo entry's explicit disposition;
- `create_llm_client` passing the correct source region and provider settings;
- mocked Converse requests asserting the global `modelId`;
- mocked OpenAI Responses requests asserting the global `model` and endpoint
  configuration;
- price calculation for input, output, cache-read, and cache-write categories;
- historical event costs remaining unchanged after catalog updates;
- invalid or unsupported matrix entries failing clearly rather than silently
  falling back to an unintended model.

Where credentials are available, perform an authenticated smoke test for each
provider family. Otherwise document the unverified operational prerequisites.

## Implementation sequence

1. Verify AWS model cards, inference-profile IDs, endpoint/API support, pricing,
   source-region requirements, cache semantics, and IAM requirements.
2. Record and review the complete model matrix. No catalog edits proceed before
   unsupported/retained entries have an explicit disposition.
3. Update `shared/src/archie_shared/models.py` and related model tests.
4. Update Bedrock client construction/request configuration and provider tests.
5. Run focused model/provider/accounting tests, then the full test suite and Ruff.
6. Perform authenticated smoke testing if possible and document any unavailable
   validation.

## Acceptance criteria

- Every Bedrock catalog entry has a verified and documented global/retained/
  excluded disposition.
- Every migrated entry sends the verified global profile ID through the correct API
  and uses verified global pricing.
- Runtime SDK clients continue to use valid source AWS Regions.
- Unsupported models are not incorrectly labeled or priced as global.
- New costs use the new catalog pricing; historical costs are unchanged.
- Provider tests and the full project checks pass.
- Operational IAM, SCP, source-region, and cross-Region data-routing requirements
  are documented.

## Expected files

- `shared/src/archie_shared/models.py`
- `agent/src/archie_agent/llm/__init__.py`
- `agent/src/archie_agent/llm/bedrock.py`
- `agent/src/archie_agent/llm/bedrock_openai.py`
- model, provider, integration, and accounting tests
- documentation or the verified model matrix appendix

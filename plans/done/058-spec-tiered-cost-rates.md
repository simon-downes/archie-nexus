# 058 — Tiered Model Cost Rates

## Objective

Replace Archie Nexus's flat model pricing fields with a list of `Rate` records owned by `CostConfig`. A request selects one rate using a supported `total_input` minimum criterion, allowing Bedrock GPT models to apply short- or long-context pricing while preserving the existing request accounting and historical event-cost behavior.

## Context

`shared/src/archie_shared/models.py` currently stores four scalar prices directly on `CostConfig` and exposes module-level `calculate_cost()`. The catalog constructs those fields for every model, while Ollama and partial overrides rely on `CostConfig()` as a zero-cost default.

Request costs are calculated independently at several existing seams: `EventFactory.request()`, `Session.record_usage()`, the loop request-cost factory, the harness request-cost factory, and child-agent request-cost factories. Canonical request events persist the resulting `cost_usd`; session logs and orchestrator metrics aggregate that persisted value. Those persistence semantics must remain unchanged.

Bedrock GPT pricing is request-level: total input context is the sum of uncached input, cache-read, and cache-write tokens. Requests at or below 272,000 total input tokens use short-context rates; requests above 272,000 use long-context rates. The selected rate applies to all four billable categories for the request. This is not progressive pricing.

The design deliberately supports only one criterion field (`total_input`) and one operation (`min`). It does not introduce a general pricing-rule engine or composable pricing mechanisms.

## Requirements

### Rate model

- MUST represent model pricing as a non-empty list of `Rate` records owned by `CostConfig`
  - AC: `CostConfig` has a `rates` field and no flat `input`, `output`, `cache_read`, or `cache_write` fields
  - AC: `CostConfig()` remains a valid zero-cost configuration with one zero-valued fallback rate
  - AC: Existing non-tiered models have exactly one fallback rate with `criteria=None`

- MUST represent each rate with input, output, cache-read, cache-write, and optional criteria values
  - AC: `Rate` contains `input`, `output`, `cache_read`, and `cache_write` floats defaulting to `0.0`
  - AC: `Rate.criteria` is either `None` or a three-element `(field, operation, threshold)` tuple

### Criteria selection

- MUST support only the `total_input` field and `min` operation
  - AC: Criteria `("total_input", "min", N)` matches when total input is at least `N`
  - AC: Any other field or operation raises a clear configuration error

- MUST calculate `total_input` as uncached input plus cache-read plus cache-write tokens
  - AC: `input_tokens=200_000`, `cache_read_tokens=80_000`, and `cache_write_tokens=0` selects a 280,000-token tier
  - AC: The four token categories remain separately available for cost calculation and persistence

- MUST select the matching rate with the greatest threshold
  - AC: Selection is independent of list order
  - AC: `criteria=None` is the fallback rate
  - AC: Duplicate thresholds and duplicate fallback rates are rejected
  - AC: No matching rate without a fallback raises a clear error

- MUST use request-level pricing rather than progressive pricing
  - AC: Once a rate is selected, its four prices apply to all four token categories in the request
  - AC: A request over the threshold does not split tokens between short and long rates

### Cost API and accounting

- MUST expose cost selection and calculation as behavior on `CostConfig`
  - AC: `CostConfig.get_rate(...)` accepts all four token categories and returns the selected `Rate`
  - AC: `CostConfig.get_cost(...)` accepts all four token categories and returns USD
  - AC: The existing `calculate_cost(...)` export delegates to `CostConfig.get_cost(...)`

- MUST preserve existing token sanitization behavior
  - AC: Negative, non-integer, boolean, and malformed token values are sanitized as before
  - AC: Sanitized values are used consistently for both tier selection and cost calculation

- MUST preserve accounting and historical-cost semantics
  - AC: Event, session, loop-limit, harness, and child-agent accounting use the same selected rate
  - AC: Existing `LLMRequest.cost_usd` values remain authoritative after catalog changes
  - AC: Metrics and replay do not recalculate historical costs from the current catalog

### Catalog migration

- MUST migrate every default catalog entry to the rate-list representation
  - AC: Every default model has at least one rate
  - AC: Existing non-GPT prices are numerically unchanged
  - AC: Ollama models retain zero-cost defaults where no provider price exists

- MUST configure GPT global-profile pricing with short and long rates
  - AC: Each GPT tiered catalog entry has a short rate with `("total_input", "min", 0)`
  - AC: Each GPT tiered catalog entry has a long rate with `("total_input", "min", 272_001)`
  - AC: GPT-5.6 Luna uses global short/long rates `(0.20, 1.20, 0.02, 0.25)` and `(0.40, 1.80, 0.04, 0.50)`
  - AC: GPT-6 Luna uses global short/long rates `(0.10, 0.50, 0.01, 0.125)` and `(0.20, 0.75, 0.02, 0.25)`
  - AC: GPT-6 Sol uses global short/long rates `(2.00, 10.00, 0.20, 2.50)` and `(4.00, 15.00, 0.40, 5.00)`
  - AC: GPT-6 Terra remains explicitly unpriced with a zero-valued fallback rate because no authoritative pricing source was verified

- MUST validate model pricing configuration when catalogs load
  - AC: Default catalog entries are validated even when no override file exists
  - AC: Empty rates, malformed criteria, negative thresholds, boolean thresholds, invalid rates, duplicate thresholds, and duplicate fallbacks fail before the catalog is returned
  - AC: Removed flat pricing keys in YAML fail rather than being silently ignored
  - AC: YAML criteria use sequence syntax such as `criteria: [total_input, min, 272001]`
  - AC: Overrides omitting `cost` retain the zero-cost default behavior

## Technical Design

### Shared pricing structs

Add `Rate` beside `CostConfig` in `shared/src/archie_shared/models.py`:

```python
class Rate(msgspec.Struct):
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    criteria: tuple[str, str, int] | None = None


class CostConfig(msgspec.Struct):
    rates: list[Rate] = msgspec.field(default_factory=lambda: [Rate()])
```

`CostConfig` owns validation, rate selection, and cost calculation. Methods remain pure and operate only on struct data. No provider calls or external lookups are added.

`get_rate()` computes:

```python
total_input = input_tokens + cache_read_tokens + cache_write_tokens
```

It validates each criteria tuple, matches supported minimum thresholds, treats `None` as fallback, and returns the highest-threshold match. `get_cost()` applies the selected rate to the four separately sanitized token categories.

### Compatibility boundary

Keep `calculate_cost(cost, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)` exported from `archie_shared.models` and `archie_shared.__init__`. It delegates to `cost.get_cost(...)` so runtime callers can migrate without changing the public function signature.

Expose `Rate` from `archie_shared.__init__` alongside `CostConfig`.

### Catalog loading and validation

`load_models()` must validate `DEFAULT_MODELS` before returning it, including the no-override path. Override mappings must reject removed flat cost keys despite the current `strict=False` conversion path. Semantic validation occurs after conversion and is also repeated by `get_rate()`/`get_cost()` for programmatically constructed structs.

Existing override entries that omit `cost` continue to use the zero-valued `CostConfig` default. Legacy flat cost fields are not supported.

### UI summary

`cli/src/archie_cli/tui/models_provider.py` currently reads `model.cost.input` and `model.cost.output`. Replace those reads with the rate whose criterion is `("total_input", "min", 0)`. If more than one rate exists, append a concise `tiered` indicator. The UI does not gain tier selection or full pricing-rule display.

### Accounting flow

Provider usage continues to use four separate categories. `CostConfig` derives `total_input` only for rate selection. The selected rate prices all categories, and the resulting USD value continues through the existing event, session, loop, child-agent, JSONL, SQLite, and TUI flows.

The following call sites MUST use the new behavior consistently:

- `agent/src/archie_agent/event_factory.py`
- `agent/src/archie_agent/session.py`
- `agent/src/archie_agent/loop.py`
- `agent/src/archie_agent/harness.py`
- `agent/src/archie_agent/agents.py`

## Milestones

### Milestone 1 — Shared rate-list cost model

**Approach**

Implement `Rate`, the list-based `CostConfig`, validation, `get_rate()`, and `get_cost()` in `shared/src/archie_shared/models.py`. Keep the existing `calculate_cost()` wrapper and export surface. Test through the public `CostConfig` and `calculate_cost()` interfaces using literal rates and token counts.

**Edge Cases**

- Empty rate list → clear configuration error.
- `criteria=None` → fallback rate.
- Unsupported field, operation, tuple length, threshold type, or negative threshold → clear configuration error.
- Duplicate thresholds or fallbacks → clear configuration error.
- Exact threshold 272,000 → short rate; 272,001 → long rate.
- Cache tokens → included in `total_input` but priced through their own selected-rate categories.
- Invalid token values → existing sanitization behavior is preserved.

**Tasks**

- Add `Rate` and replace flat `CostConfig` fields with `rates`.
- Implement semantic validation and deterministic highest-threshold selection.
- Implement `CostConfig.get_rate()` and `CostConfig.get_cost()`.
- Make `calculate_cost()` delegate to `CostConfig.get_cost()`.
- Export `Rate` from `archie_shared`.
- Add unit tests for fallback, threshold selection, cache-inclusive totals, validation, and backward-compatible calculation behavior.

**Deliverable**

The shared cost API selects and calculates a valid rate from a list using `total_input` minimum criteria.

**Verify**

Run `uv run pytest tests/test_models_catalog.py -q` and `uv run ruff check shared/src/archie_shared/models.py shared/src/archie_shared/__init__.py tests/test_models_catalog.py`. Observe passing tests for exact threshold boundaries, cache-inclusive selection, invalid configuration rejection, and unchanged single-rate calculations.

### Milestone 2 — Catalog migration and model-facing behavior

**Approach**

Convert `DEFAULT_MODELS` and all model fixtures to `CostConfig(rates=[Rate(...)])`. Add the verified GPT short/long rates and preserve all non-pricing metadata. Update catalog loading validation and the TUI model-provider summary to use the threshold-0 rate.

**Tasks**

- Convert every default catalog price to a single fallback `Rate`.
- Add GPT-5.6 Luna, GPT-6 Luna, and GPT-6 Sol short/long rates with threshold `272_001`.
- Keep GPT-6 Terra explicitly zero-priced.
- Reject removed flat pricing keys in override YAML and test list-based criteria decoding.
- Update all `CostConfig` test fixtures, including `tests/test_task_tool.py`, `tests/test_native_dispatch.py`, `tests/test_model_switch.py`, `tests/test_harness.py`, and `tests/test_session.py`.
- Update `cli/src/archie_cli/tui/models_provider.py` to display the threshold-0 rate and a `tiered` indicator.
- Update catalog and TUI assertions for the new representation.

**Deliverable**

The default and override catalogs load with list-based pricing, and model selection displays pricing without flat-field assumptions.

**Verify**

Run `uv run pytest tests/test_models_catalog.py tests/test_model_switch.py tests/test_task_tool.py tests/test_native_dispatch.py -q` and `uv run ruff check .`. Observe that every default model loads, GPT boundary rates are present, legacy flat override fields fail, and TUI model discovery does not raise attribute errors.

### Milestone 3 — Runtime accounting integration

**Approach**

Update every runtime accounting seam to pass the same four usage categories through the new `CostConfig` behavior. Preserve the existing event and session data structures, rounding, cost-limit semantics, child accounting, JSONL persistence, and metrics aggregation.

**Wiring**

- Provider adapters produce the existing four usage categories.
- `EventFactory`, `Session.record_usage()`, the loop request-cost factory, the harness request-cost factory, and child-agent factories call the same cost API.
- `CostConfig` derives `total_input` and returns one request cost.
- Existing event/session/metrics consumers receive the same cost fields as before.

**Edge Cases**

- Request crosses 272,000 → long rate is applied to all four categories.
- Cache tokens push an otherwise short request over the threshold → long rate is selected.
- Event and live session totals → use the same selected rate.
- Historical event replay → uses persisted `cost_usd` without current-catalog recalculation.

**Tasks**

- Update `EventFactory.request()` and `Session.record_usage()`.
- Update the loop's `request_cost_factory` and harness request-cost factory.
- Update child-agent request-cost factories.
- Add tests for event cost, session totals, loop turn-limit accounting, and cache-inclusive threshold selection.
- Add a regression test proving persisted historical costs remain unchanged after catalog rates change.
- Run a targeted search for remaining flat-field cost access and migrate all results.

**Deliverable**

All runtime accounting paths agree on rate selection and preserve canonical request-cost behavior.

**Verify**

Run `uv run pytest tests/test_harness.py tests/test_model_switch.py tests/test_canonical_session_events.py tests/test_session.py tests/test_task_tool.py -q`, then `uv run pytest -q`. Compare event cost, session total, and loop-limit test values for requests below and above the threshold; any unrelated pre-existing failure must be reported separately.

## Risks and Assumptions

- The threshold is evaluated against `input_tokens + cache_read_tokens + cache_write_tokens`, matching the existing context-token calculation and the agreed Bedrock pricing basis.
- Rates are selected per request; this plan does not model progressive token bands.
- GPT prices are tied to the existing global OpenAI profile IDs. A provider/profile override is responsible for its own pricing.
- GPT-6 Terra remains zero-priced until authoritative pricing is available; this avoids inventing a billable rate.
- Historical costs remain stable because persisted event `cost_usd` is not recalculated.
- The TUI displays only the short/default summary rate and does not attempt to present every tier.

## Status

- [ ] Milestone 1 — Shared rate-list cost model
- [ ] Milestone 2 — Catalog migration and model-facing behavior
- [ ] Milestone 3 — Runtime accounting integration

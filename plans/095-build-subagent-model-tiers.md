# 095 — Subagent model tiers

Status: planning
Branch: build-it/095-subagent-model-tiers (base: build-it/093-scratch-workspace @ 48f5d35)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/095-subagent-model-tiers

## Objective

Subagents run on the session's model, which wastes money when a premium session delegates
simple work and under-powers reviews delegated from a cheap session. The calling agent will
pick a tier (`light`, `standard`, `deep`) for each delegated task, and config maps each tier
to a concrete model per backend, so model names can change without changing the tool
contract.

## Context

The user's words: "add a parameter to the subagent tool that can select the underlying
model. I don't want to allow just any model and models will change over time … the values
would then map to explicit model names (via config)." They approved a design with a
required three-value tier, config mapping per backend, and agent definitions that can pin a
tier, and chose this config shape:

```yaml
subagents:
  bedrock: {light: luna 6.1, standard: sonnet 5.5, deep: opus 5.5}
  kiro:    {light: luna, standard: sonnet 5.5, deep: opus 5.5}
```

Codebase facts:

- Native backend: the `task` tool (`agent/src/archie_agent/agents.py`, `create_task_tool`)
  takes `tasks: [{agent, prompt}]`. `_resolve_model` uses an agent definition's `model:`
  frontmatter (a catalogue key) and otherwise the session model.
- `agent.subagents` (`SubagentsConfig` in `shared/src/archie_shared/schemas.py`) holds only
  `max_concurrent`. `agent.compaction.model` is the precedent for a config-chosen catalogue
  key checked at startup (`app.py` `_backend_lifespan`, `AgentHarness.__init__`).
- Kiro backend: delegation uses Kiro's built-in `subagent` tool, which takes only an agent
  name. The harness writes one worker agent, `archie-worker`
  (`agent/src/archie_agent/kiro/agent_files.py`). Kiro agent JSON has a `model` field
  ("the model ID to use for this agent", kiro.dev configuration reference), so a tier on
  Kiro is a separate worker agent with a pinned model.
- Catalogue keys: `bedrock-openai-gpt-6-luna`, `bedrock-claude-sonnet-5-5`,
  `bedrock-claude-opus-5-5` exist (no GPT 6.1 Luna). Kiro ids: `gpt-5.6-luna`,
  `claude-sonnet-5.5`, `claude-opus-5.5`.

## Baseline

- Check commands: `scripts/dev test`, `scripts/dev ruff check .`, `scripts/dev ruff format --check <files>`
- Setup: `scripts/dev sync`
- Setup leftovers: none (`.venv-archie/` and caches are gitignored)
- Failing before this work: 32 tests, all in `tests/test_question_tool.py` (10),
  `tests/test_ws_integration.py` (13), `tests/test_host_config.py` (2),
  `tests/test_local_api.py` (1) plus 6 teardown errors; list in `/tmp/095-baseline.txt`.
  Ruff check passes.
- Not included: `.DS_Store`, `plans/060-spec-container-aws-profiles.md`,
  `plans/070-build-nexus-plugins.md` (untracked in the user's checkout)

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Name of the native tier map | `native` | `bedrock` | The catalogue also has `openai-*` and `ollama-*` keys, and the docs call the non-Kiro loop "native" | user Q1 |
| D2 | Where the tier maps live | `agent.subagents.native` and `agent.subagents.kiro`, next to `max_concurrent` | a new top-level `subagents:` section | `agent.subagents` already exists; a second section would split subagent settings | self |
| D3 | Tier names and count | `light`, `standard`, `deep` | `effort`/`complexity` wording; five levels | `kiro.effort` already means reasoning effort; three levels are what the user approved and models pick reliably among | self (approved design) |
| D4 | Tier map types | Two structs, `NativeSubagentTiers` and `KiroSubagentTiers`, each with all three fields defaulted | one struct with required fields | Per-field defaults let a user override one tier (`deep:` only) | self |
| D5 | Defaults | native: `bedrock-openai-gpt-6-luna`, `bedrock-claude-sonnet-5-5`, `bedrock-claude-opus-5-5`; kiro: `gpt-5.6-luna`, `claude-sonnet-5.5`, `claude-opus-5.5` | GPT 6.1 Luna | The user's choices mapped to real ids; there is no GPT 6.1 Luna in the catalogue | self |
| D6 | Native tier selection | A required `tier` enum on each `task` item | optional with a default; one tier per call | Required forces a choice per task; tasks in one call can need different tiers (a survey and a review) | self (approved design) |
| D7 | Missing or invalid `tier` at run time (providers don't enforce the enum) | Use `standard` and prefix a warning to that task's result | fail the task | Matches the unknown-agent fallback in `run_child` | self |
| D8 | Precedence on native | definition `model:` > definition `tier:` > caller's `tier` | caller overrides the pin; pin as a minimum | A cheap caller underestimates reviews; a pin doesn't depend on its judgement. A minimum adds a concept | self (approved design) |
| D9 | Unknown native tier key | Startup check in `app.py` `_backend_lifespan` (log, exit 1), like `agent.compaction.model`; at run time an unknown key fails that task | check in `AgentHarness.__init__` | Many harness tests build `AgentHarness` with a one-model catalogue; the app boundary is where config is validated | self |
| D10 | Kiro tier mechanism | Three worker agents, `archie-worker-light`, `archie-worker-standard`, `archie-worker-deep`, each with `model` set and the tier's guidance in `description`; they share the `archie-worker.md` prompt. `archie-worker` is no longer written | keep `archie-worker` beside them | An unpinned worker brings back session-model inheritance | self |
| D11 | Kiro tier model not offered by Kiro | Startup fails closed: `Kiro does not offer agent.subagents.kiro.<tier> '<id>'` | warn and drop the pin | Matches `kiro.default_model`; dropping silently brings back the cost problem. Tier ids need not be in `kiro.models` (that is the switch allowlist) | self |
| D12 | Kiro dispatch log shape | The translator adds `tier` to the logged `task` input when the agent name is a tier worker | log only the name | Gives both backends the same `{agent, prompt, tier}` shape | self |
| D13 | Where tier names and guidance live | `SUBAGENT_TIERS` (names) in `shared/schemas.py`; guidance text in new `agent/src/archie_agent/tiers.py`, used by the task description and the Kiro agent files | duplicating text in each | One source for the wording | self |
| D14 | Pinned agents | `build-it-reviewer` gets `tier: deep`; the build-it skill's Reviewers section says to use `deep` (`archie-worker-deep` on Kiro) | leave reviewers to the caller | Reviews are the case the user named | self |

## Requirements

- MUST accept `agent.subagents.native.{light,standard,deep}` and `agent.subagents.kiro.{light,standard,deep}` with the D5 defaults, allow overriding a single tier, and reject unknown tier keys and empty values.
  - AC: `NexusConfig().agent.subagents.native.deep == "bedrock-claude-opus-5-5"`; YAML with only `kiro: {deep: x}` loads with `light == "gpt-5.6-luna"`; `native: {huge: x}` and `native: {light: ""}` raise `ConfigError`.
- MUST give each native `task` item a required `tier` property with enum `light|standard|deep`, and describe each tier in the tool description.
  - AC: the `task` schema's item `required` is `["agent", "prompt", "tier"]`; the description names all three tiers with their guidance.
- MUST run a native child on the model its tier maps to.
  - AC: a `deep` task with tiers `{deep: "big"}` creates its LLM client with catalogue entry `big`, and its `llm_request` events carry `model_key` `big`.
- MUST fall back to `standard` with a warning when `tier` is missing or invalid.
  - AC: a task with `tier: "huge"` runs on the `standard` model and its result starts with `[0] <agent>: Warning: unknown tier 'huge'`.
- MUST let an agent definition pin a tier with `tier:` frontmatter, and keep `model:` winning over it; the description marks pinned agents.
  - AC: a definition with `tier: deep` runs on the `deep` model when called with `light`; one with `model: m` runs on `m`; an invalid `tier:` value is ignored with a warning; the description shows `(fixed tier: deep)`.
- MUST exit a native session at startup when a native tier maps to a key not in the catalogue.
  - AC: the startup helper reports `agent.subagents.native.deep: unknown model 'nope'`; `_backend_lifespan` logs it and exits 1.
- MUST write the three Kiro tier worker agents with their models, let the root start and trust all three, and fail Kiro startup when Kiro doesn't offer a tier model.
  - AC: `~/.kiro/agents/archie-worker-deep.json` has `"model": "claude-opus-5.5"` and the `question`-free tool list; `archie.json` rules match all three names; with `kiro: {light: gpt-9}` `start()` raises `KiroStartupError` naming `agent.subagents.kiro.light 'gpt-9'`.
- MUST log a Kiro dispatch to a tier worker as `task` input `{agent, prompt, tier}`.
  - AC: a dispatch with `rawInput.name == "archie-worker-deep"` logs `tasks[0].tier == "deep"`.
- MUST pin `build-it-reviewer` to `deep`, and update the delegation, Kiro prompt, config, Kiro and architecture docs.
  - AC: `discover_agents()["build-it-reviewer"].tier == "deep"`; `tests/test_kiro_agent_files.py` and the docs grep for `archie-worker-` show no stale `archie-worker.json` references.

Out of scope: reasoning effort per tier; showing the tier in the TUI or insights; per-subagent accounting on Kiro (Kiro reports one credit figure per turn).

## Design

### Overview

Config maps tier → model per backend. Native: `run_child` picks the model by definition `model:`, then definition `tier:`, then the caller's `tier`, through the tier map. Kiro: one worker agent per tier with `model` pinned; Kiro's own `subagent` tool chooses among them by description.

### Code structure

- `shared/src/archie_shared/schemas.py`: `SUBAGENT_TIERS`, `NativeSubagentTiers`, `KiroSubagentTiers` (each `__post_init__` rejects empty values), `SubagentsConfig.native` and `.kiro`.
- New `agent/src/archie_agent/tiers.py`: `TIER_GUIDANCE: dict[str, str]`, `DEFAULT_TIER = "standard"`, `unknown_native_tiers(tiers, catalog) -> list[str]` (error strings).
- `agent/src/archie_agent/agents.py`: `AgentEntry.tier: str | None = None` (last field, so positional construction keeps working); `_parse_agent_file` reads `tier`; `create_task_tool(..., tiers: Mapping[str, str] | None = None)`. `tiers=None` keeps today's behaviour (session model, no tier check), for direct callers and tests; `AgentHarness` always passes `self._subagents.native`.
- `agent/src/archie_agent/app.py`: after the compaction check, exit 1 on `unknown_native_tiers`.
- `agent/src/archie_agent/kiro/agent_files.py`: `WORKER_AGENTS = {tier: f"archie-worker-{tier}"}`, `worker_agent_config(mcp_url, tools, *, name, model, description)`, `write_agent_files(..., worker_models: Mapping[str, str])`; `tier_of(agent_name) -> str | None`.
- `agent/src/archie_agent/kiro/harness.py`: pass the kiro tier map to `write_agent_files`; check tier models after `_wait_for_models`.
- `agent/src/archie_agent/kiro/translate.py`: add `tier` via `tier_of`.
- Persona: `persona/agents/build-it-reviewer.md`, `persona/prompts/delegation.md`, `persona/prompts/kiro.md`, `persona/skills/build-it/SKILL.md`.
- Docs: `docs/config.md`, `docs/kiro.md`, `docs/architecture.md`.

## Milestones

### [ ] M1 — Native tasks run on their tier's model

- **Approach:** Add the config structs and `SubagentsConfig` fields (follow `CompactionConfig`). Add `tiers.py`. In `create_task_tool`, add `tier` to the item schema and `required`, the tier lines to `_task_description`, and resolution in `run_child` before `_resolve_model`: when `tiers` is given, pick the tier (warning + `standard` on missing/invalid) and look its key up in `model_catalog` (KeyError → task error, as `_resolve_model` does). Wire `tiers=msgspec.structs.asdict(self._subagents.native)` in `AgentHarness`; give harness tests that run tasks a catalogue/tier map they can resolve. Add the startup check in `_backend_lifespan`. Update `persona/prompts/delegation.md`, `docs/config.md` and the `docs/architecture.md` config block.
- **Test seam:** `create_task_tool(...).handler` (`tests/test_task_tool.py`, capturing the model passed to the monkeypatched `create_llm_client`); `load_nexus_config` (`tests/test_subagents_config.py`); `unknown_native_tiers`.
- **Edge cases:** tier missing / not a string / not a tier name → `standard` + warning; tier key not in catalogue → that task returns `KeyError: unknown model '<key>'`, siblings unaffected.
- **Deliverable:** a native `task` call with `tier: deep` runs the child on the configured deep model.
- **Verify:** `scripts/dev test tests/test_task_tool.py tests/test_subagents_config.py tests/test_harness.py tests/test_tiers.py` passes.
- **Outcome:**

### [ ] M2 — Agent definitions pin a tier

- **Approach:** `_parse_agent_file` reads `tier` with `_optional_string`, then drops values not in `SUBAGENT_TIERS` with a warning. Resolution order D8. `_task_description` appends `(fixed tier: <t>)` or `(fixed model)`. Add `tier: deep` to `persona/agents/build-it-reviewer.md`; add the `deep` instruction to `persona/skills/build-it/SKILL.md` Reviewers.
- **Test seam:** `discover_agents` with `ARCHIE_PERSONA_DIR` pointed at a temp dir, and the task handler.
- **Deliverable:** a pinned agent runs on its pinned tier regardless of the caller's tier.
- **Verify:** `scripts/dev test tests/test_task_tool.py tests/test_agents.py` passes, including a test that loads the real `build-it-reviewer`.
- **Outcome:**

### [ ] M3 — Kiro tier workers

- **Approach:** Replace the single worker in `agent_files.py` with the three tier workers (D10), each `model` from `agent.subagents.kiro` and `description` built from `TIER_GUIDANCE`; root permission rule and `toolsSettings` list all three. Harness: pass `config.agent.subagents.kiro`; after `_wait_for_models`, raise `KiroStartupError` for each tier model not in `model_options()`. Translator: `tier_of(name)` adds `tier`. Update `persona/prompts/kiro.md` (pick the worker whose tier fits the work) and `docs/kiro.md`. Update `tests/test_kiro_agent_files.py`, `tests/test_kiro_harness.py::test_question_tool_is_root_only`, `tests/test_kiro_translate.py`.
- **Test seam:** `write_agent_files`; `KiroHarness.start()` against `tests/fake_kiro.py`; `TurnTranslator` with a dispatch frame.
- **Edge cases:** a tier model Kiro doesn't offer → startup fails closed; a dispatch to a non-tier agent name → no `tier` key.
- **Deliverable:** a Kiro session writes three pinned workers and refuses to start when one's model isn't offered.
- **Verify:** `scripts/dev test tests/test_kiro_agent_files.py tests/test_kiro_harness.py tests/test_kiro_translate.py` passes (apart from baseline failures, of which these files have none).
- **Outcome:**

## Deviations

## Risks

- Kiro is assumed to run a subagent on its agent's `model` pin. The configuration reference documents the field for agents, but this build can't run real Kiro, so that's not verified.
- `kiro.effort` is set for the session; whether Kiro applies it to subagents on another model is unknown.
- Stacked on the unmerged 093 branch: this PR targets `build-it/093-scratch-workspace` until #40 merges.

## Review

## Outcome

PR: pending
CI: pending

## Follow-ups

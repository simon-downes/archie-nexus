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
- Setup leftovers: `.venv-archie/`
- Failing before this work: 32 tests, all in `tests/test_question_tool.py` (10),
  `tests/test_ws_integration.py` (13), `tests/test_host_config.py` (2),
  `tests/test_local_api.py` (1) plus 6 teardown errors; list in `/tmp/095-baseline.txt`.
  Ruff check passes.
- Not included: `.DS_Store`, `plans/060-spec-container-aws-profiles.md`,
  `plans/070-build-nexus-plugins.md` (untracked in the user's checkout)

# 070 — Nexus plugins

Status: planning
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
- About 62 test files reference old `archie_agent.exec.tools.<integration>` paths (95 dotted
  string monkeypatches). `tests/conftest.py` loads `tests.datadog_fakes` and
  `tests.incident_io_fakes` as pytest plugins.

## Baseline

- Check commands: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`
- Setup: `uv sync`
- Setup leftovers: none
- Failing before this work: none (1660 passed; ruff check and format clean)
- Not included: `persona/skills/aws-agent-access/` and `plans/060-spec-container-aws-profiles.md`
  (untracked in the main checkout)

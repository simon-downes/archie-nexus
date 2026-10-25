# 072 — Cloudflare plugin using the cf CLI

Status: planning
Branch: build-it/072-cloudflare-cli (base: main @ fa87bf0)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/072-cloudflare-cli

## Objective

Give agent containers read-only access to Cloudflare through Cloudflare's new `cf` CLI
(open beta), authenticated with a scoped API token. When done, a user stores a
read-only token with `archie auth login cloudflare`, new sessions get it in the
environment, `cf` is installed in the image, and a `cloudflare` skill teaches the agent
how to inspect the resource areas the user cares about without changing anything.

## Context

User request: "implement a cloudflare plugin that uses the new cloudflare cli (beta) to
provide access to cloudflare resources. We should use api tokens for auth so we can scope
permissions properly." The skill should focus on workers, queues, workflows, VPC, R2,
Workers KV, domains, DNS, SSL/certs, WAF, Turnstile and tunnels. The token is entirely
read-only; narrow write scopes may come later, but most resources are managed by
Terraform.

Discovery:

- `cf` is the npm package `cf` (1.0.0-beta.21 at discovery). It mirrors the public API
  (~2,900 generated commands), prints JSON on stdout, errors on stderr, and has
  `cf cli search "<task>"` and `cf schema <cmd>` for discovery. It authenticates
  non-interactively from `CLOUDFLARE_API_TOKEN` (which beats any stored login) and reads
  `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_ZONE_ID` for targeting. `CF_SEND_TELEMETRY=false`
  or `DO_NOT_TRACK=1` turns telemetry off. Non-interactive destructive commands abort
  without `--force` and exit 0. Docs: https://developers.cloudflare.com/cf/.
- Plugins live in `plugins/<id>/` (`docs/plugins.md`). Credential-only plugins with env
  projection follow `plugins/scalr/` (`credential.env` + `set_env: true`; settings with
  `env`). CLI-driven skills without exec tools follow `plugins/aws/skills/aws-agent-access`.
  Providers and credential types are derived from manifests
  (`shared/src/archie_shared/credentials/providers.py`, `models.py`), and several tests
  enumerate the provider set.
- The image (`Dockerfile`) installs Node LTS and global npm tools (`agent-browser`); it does
  not install `cf`. `agent/src/archie_agent/environment.py` lists probed CLIs.

## Baseline

- Check commands: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`
  (pytest run with `ARCHIE_PLUGINS_DIR`, `ARCHIE_PERSONA_DIR`, `ARCHIE_HOME_DIR` unset,
  because this agent's own container sets them and the suite then loads the wrong plugins).
- Setup: `uv sync`.
- Setup leftovers: none (`.venv` is ignored).
- Failing before this work: `tests/test_plugin_layout.py::test_persona_skills_holds_only_procedure_skills`
  (`persona/skills/inbox-triage` is not a procedure skill). Ruff check and format pass.
- Not included: the user's untracked `openai.key`, `persona/skills/aws-agent-access/`,
  `plans/060-spec-container-aws-profiles.md`, `plans/070-build-nexus-plugins.md`.

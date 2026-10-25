# 072 — Cloudflare plugin using the cf CLI

Status: done
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
  without `--force` (exit 1 under beta.21, see Deviations). Docs: https://developers.cloudflare.com/cf/.
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
## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | How the agent reaches Cloudflare | `cf` run directly in the shell, authenticated by `CLOUDFLARE_API_TOKEN`; the token's permissions are the only permission layer | A wrapper enforcing a command allowlist; Python exec tools under a `cloudflare` namespace | Two layers that disagree (wrapper allows, token denies, or the reverse) are confusing; `cf` has ~2,900 generated commands, so an allowlist or exec tools would be large and track a moving beta | user Q1 |
| D2 | Plugin shape | Credential-and-settings plugin with one skill and no Python code (`plugins/cloudflare/`), like `plugins/scalr/` plus a skill like `plugins/aws/skills/aws-agent-access` | A plugin with an `entry` and tool policy | No exec tools (D1), so `policy` and `namespaces` have nothing to gate | self |
| D3 | Credential field name | `api_token` → `CLOUDFLARE_API_TOKEN`, `set_env: true` | `token` (Scalr's name) | Cloudflare also has a global API key (`CLOUDFLARE_API_KEY`) that `cf` reads; naming the field `api_token` makes it clear which one to store | self |
| D4 | Account targeting | Optional setting `account_id` → `CLOUDFLARE_ACCOUNT_ID` | Also a `zone_id` setting; no settings | Workers, Queues, R2, KV, Workflows, VPC, Turnstile and Tunnels are account-scoped and need an account id; zones are many per account and `cf` takes `-z <domain>` per call, so a fixed default zone would mislead | self |
| D5 | How `cf` is installed | Pinned `npm install --global cf@1.0.0-beta.21` in the existing Node layer of `Dockerfile` | `cf@latest`; `npx cf` on demand | The skill names exact commands, and a beta can rename them between releases; pinning keeps the skill and CLI in step, and `npx` would download on every cold session | self |
| D6 | Telemetry | `ENV CF_SEND_TELEMETRY=false` in `Dockerfile` | Leave the default (on); a plugin setting | Containers are ephemeral, so each session would look like a new install; an image-level env var is the mechanism `cf` documents and needs no config | self |
| D7 | Write and secret-bearing commands | The skill tells the agent to run only read commands, never pass `--force`, and not to read secret values (`cf tunnels token get`, KV values, R2 object bodies) unless the user asks for that specific value | Rely on the token alone with no guidance | D1 rules out a second enforcement layer, but guidance is not enforcement: it stops the agent trying writes that the token will deny anyway, and keeps secrets out of model context when the token's read scope does allow them | self |
| D8 | Agent discovery of `cf` | Add `cf` to `Cloud/IaC` in `agent/src/archie_agent/environment.py` and a one-line entry in `persona/prompts/tool-notes.md` pointing at the `cloudflare` skill | Skill only | The environment section is how the agent learns which CLIs exist; `cf` is an unusual name and could be mistaken for something else without the note | self |
| D9 | Checking the skill's commands | A pytest in `plugins/cloudflare/tests/test_cloudflare_skill.py` that extracts each `cf …` command from the skill and runs it with `--help`, skipped when `cf` is not on PATH; run here with the pinned CLI from `/tmp/cfcli` on PATH | A manual, uncommitted check; installing npm packages from pytest | `cf` is not installed in the dev or CI environment, but it is in the agent image, so the test runs wherever `cf` exists and the check is repeatable on a version bump (D5) | self |
| D10 | Token permissions documented | README lists the read permission groups per area, split account vs zone, including `Account Settings Read` (needed for `cf accounts list`); groups not in Cloudflare's permissions reference (Workers VPC, Registrar) are marked unconfirmed | No list; only "a read-only token" | The user scopes the token by hand in the dashboard, and the skill's missing-account-id path depends on `Account Settings Read` | self |

## Requirements

- MUST register a static `cloudflare` provider with one field `api_token`.
  - AC: `PROVIDERS["cloudflare"]` is a `StaticProvider` with `fields == ["api_token"]`,
    `env == {"api_token": "CLOUDFLARE_API_TOKEN"}`, `set_env is True`;
    `CREDENTIAL_TYPES["cloudflare"].__struct_fields__ == ("api_token",)`.
- MUST let `archie auth login cloudflare` read the token from `CLOUDFLARE_API_TOKEN`.
  - AC: a test like `test_scalr_login_uses_environment` stores `{"api_token": <value>}`.
- MUST export the stored token as `CLOUDFLARE_API_TOKEN` and `plugins.cloudflare.settings.account_id`
  as `CLOUDFLARE_ACCOUNT_ID` into new containers, and neither when the plugin is disabled.
  - AC: `runtime_environment()` returns both variables when enabled; neither when
    `enabled: false`; only the token when `account_id` is unset.
- MUST install `cf` 1.0.0-beta.21 in the agent image with telemetry off.
  - AC: `Dockerfile` installs `cf@1.0.0-beta.21` and runs `cf --version` in the same
    layer; `ENV CF_SEND_TELEMETRY=false` is set.
- MUST ship a `cloudflare` skill that covers the requested areas with read commands
  that exist in the pinned CLI.
  - AC: `test_cloudflare_skill.py` asserts a `### <area>` heading for each of: `Workers`,
    `Queues`, `Workflows`, `Workers VPC`, `R2`, `Workers KV`, `Zones and domains`, `DNS`,
    `SSL and certificates`, `WAF`, `Turnstile`, `Tunnels`.
  - AC: the D9 test runs every `cf …` command in the skill with `--help` and each exits 0
    under `cf@1.0.0-beta.21`. A command is extracted from inline code or a fenced line
    starting `cf `, as the leading tokens matching `[a-z0-9-]+`, stopping at the first
    token that starts with `-` or `<` or contains other characters.
- MUST state these rules in the skill's `## Principles` section, each asserted as an exact
  phrase by `test_cloudflare_skill.py`:
  - "managed by Terraform" and "Never create, change or delete Cloudflare resources";
  - "A 403 or authentication error means the token's scope is working" and "don't work around it";
  - "Never pass `--force`";
  - "Create and update commands don't ask: only the token stops them" (see Deviations);
  - "Don't read secret values" naming `cf tunnels token get`, `cf kv keys get` and `cf r2 objects get`.
- MUST list `cf` among the agent's probed CLIs with a tool note.
  - AC: a new assertion in `tests/test_environment.py` checks `"cf"` is in
    `TOOL_CATEGORIES["Cloud/IaC"]` and that the shipped tool notes include a `` `cf` `` entry.
- MUST document the plugin: `plugins/cloudflare/README.md` (passing
  `test_plugin_has_readme_covering_its_manifest`) with the token permissions (D10); a row
  in `plugins/README.md`; in `docs/auth-credentials.md` the provider table row, a login
  paragraph next to Scalr's, the `set_env` table row and the settings projection note; a
  per-plugin note in `docs/config.md`; the plugin list in `docs/architecture.md`; and in
  `docs/plugins.md` the "Plugins without code" line, which must stop implying no-code
  plugins have no skills.
  - AC: the layout test passes; `rg -n -i cloudflare` hits each named file, and
    `docs/auth-credentials.md` at least four times.

Out of scope: write access or write scopes; exec tools; a command wrapper or allowlist;
`cf auth login` OAuth; multiple accounts or profiles; Terraform integration.

## Design

### Overview

A no-code plugin: `plugin.yaml` declares the API-token credential and an `account_id`
setting, both projected as environment variables. The image ships the pinned `cf` CLI. A
skill tells the agent how to use `cf` read-only across the requested resource areas.

### Stack

- `cf` (npm, new to the image), pinned at `1.0.0-beta.21` (D5). Installed with the existing
  global npm install of `agent-browser`.

### Code structure

- `plugins/cloudflare/plugin.yaml` (new): `name`, static `credential`
  (`fields: [api_token]`, `env: {api_token: CLOUDFLARE_API_TOKEN}`, `set_env: true`),
  `settings.account_id: {type: str, env: CLOUDFLARE_ACCOUNT_ID}`. Follows `plugins/scalr/plugin.yaml`.
- `plugins/cloudflare/README.md` (new): follows `plugins/scalr/README.md`, plus a Skill
  section and a token permissions list (read-only permission groups per area).
- `plugins/cloudflare/skills/cloudflare/SKILL.md` (new): follows the structure of
  `plugins/aws/skills/aws-agent-access/SKILL.md` (How access works, Principles, Workflow,
  per-area commands, Errors).
- `plugins/cloudflare/tests/test_cloudflare_skill.py` (new): follows `plugins/jira/tests/test_jira_skill.py`.
- `plugins/cloudflare/tests/` holds only the skill test, with no `conftest.py` or fakes:
  there is no code to fake, as in `plugins/aws/tests/`.
- Provider-set tests updated: in `tests/test_providers.py` both hand-written sets and the
  ordered `EXPECTED_PROVIDERS` dict (`cloudflare` goes between `bedrock` and `datadog`,
  matching manifest sort order); `tests/test_credentials_models.py`;
  `tests/test_orchestrator_credentials.py`; plus new cases in
  `tests/test_credentials_runtime.py`, `tests/test_plugin_enabled.py`, `tests/test_cli_auth_login.py`.

### Config / infra

- Env vars into containers: `CLOUDFLARE_API_TOKEN` (credential), `CLOUDFLARE_ACCOUNT_ID`
  (setting, when set).
- Image: `CF_SEND_TELEMETRY=false`; `cf` on PATH from the global npm install.

### Error handling

`cf` exits 1 and prints a boxed error on stderr. The skill maps the cases the agent will meet:

- No token → "No authentication token found": the user needs `archie auth login cloudflare`
  and a new session; stop.
- Bad or expired token → `[9106]`/`[10000]` authentication errors: report, stop.
- 403 / missing permission → report which command and area was denied; don't try another
  command to reach the same data.
- Missing account id → run `cf accounts list` (needs `Account Settings Read`, D10) and
  pass the id; if that is denied, ask the user to set `plugins.cloudflare.settings.account_id`.
- A destructive command run without `--force` prints "Aborted." and exits 1 → nothing
  changed; tell the user and don't retry with `--force`.

## Milestones

### [x] M1 — Cloudflare token and account id reach new containers

- **Approach:** Add `plugins/cloudflare/plugin.yaml` and a minimal `README.md` (credential,
  configuration, link to `docs/plugins.md`). Add `cloudflare` to the provider sets in the
  three enumerating tests, and add projection, disabled and login tests next to the Scalr
  ones. ⚠️ Run pytest with `ARCHIE_PLUGINS_DIR`, `ARCHIE_PERSONA_DIR`, `ARCHIE_HOME_DIR` unset.
- **Test seam:** `PROVIDERS`, `CREDENTIAL_TYPES`, `runtime_environment()`, `_set_static_from_login`.
- **Deliverable:** a stored Cloudflare token and account id are exported into new containers.
- **Verify:** `env -u ARCHIE_PLUGINS_DIR -u ARCHIE_PERSONA_DIR -u ARCHIE_HOME_DIR uv run pytest -q -p no:cacheprovider tests/test_providers.py tests/test_credentials_models.py tests/test_orchestrator_credentials.py tests/test_credentials_runtime.py tests/test_plugin_enabled.py tests/test_cli_auth_login.py tests/test_plugin_layout.py` passes except the baseline failure.
- **Outcome:** Added `plugins/cloudflare/plugin.yaml` and a README, plus provider-set,
  projection, disabled and login tests. The M1 Verify set gave 141 passed, 1 failed (the
  baseline failure). The full suite gave 2108 passed, 1 failed (baseline); ruff check and
  format pass.

### [x] M2 — The agent image has `cf` and the agent knows it is there

- **Approach:** Pin `cf@1.0.0-beta.21` in the existing `npm install --global` line in
  `Dockerfile`, add `cf --version` to that layer's checks, and set `ENV CF_SEND_TELEMETRY=false`
  next to `AGENT_BROWSER_EXECUTABLE_PATH`. Add `cf` to `TOOL_CATEGORIES["Cloud/IaC"]` in
  `agent/src/archie_agent/environment.py` and a `` `cf` `` bullet to
  `persona/prompts/tool-notes.md` that names the `cloudflare` skill. Add the assertion to
  `tests/test_environment.py`.
- **Test seam:** `environment.TOOL_CATEGORIES`, the shipped tool notes; `Dockerfile` by inspection.
- **Deliverable:** the image definition installs `cf`, and the agent's environment section
  lists it when present.
- **Verify:** `env -u ARCHIE_PLUGINS_DIR -u ARCHIE_PERSONA_DIR -u ARCHIE_HOME_DIR uv run pytest -q -p no:cacheprovider tests/test_environment.py`
  passes; `npm install --prefix /tmp/cfcli cf@1.0.0-beta.21 && /tmp/cfcli/node_modules/.bin/cf --version`
  prints `1.0.0-beta.21`.
- **Outcome:** `Dockerfile` pins `cf@1.0.0-beta.21` in the Node layer, adds `cf --version`
  to that layer and sets `CF_SEND_TELEMETRY=false`. `cf` is now in `Cloud/IaC`, with a tool
  note. `tests/test_environment.py` gave 9 passed. The npm install of the pin printed
  `1.0.0-beta.21`. The image build was not run (no Docker here).

### [x] M3 — A `cloudflare` skill for read-only use, and the docs

- **Approach:** Write `plugins/cloudflare/skills/cloudflare/SKILL.md` following the
  structure of `plugins/aws/skills/aws-agent-access/SKILL.md`: How access works,
  Principles (the exact phrases in Requirements), Workflow (whoami, account, `-z <domain>`,
  `cf cli search`, `cf schema`), one `###` section per area with list/get commands taken
  from `cf <area> --help` under the pinned version, and Errors (Design: Error handling).
  Write `test_cloudflare_skill.py` with the phrase, heading and D9 command tests. Finish
  the README (Skill and Token permissions sections, D10) and the docs entries.
- **Test seam:** the skill file text; `cf <command> --help` exit codes.
- **Deliverable:** a session with the plugin enabled lists and can load the `cloudflare`
  skill, and every command it names exists.
- **Verify:** `PATH=/tmp/cfcli/node_modules/.bin:$PATH env -u ARCHIE_PLUGINS_DIR -u ARCHIE_PERSONA_DIR -u ARCHIE_HOME_DIR uv run pytest -q -p no:cacheprovider plugins/cloudflare tests/test_plugin_layout.py`
  passes (layout test: baseline failure only) with the D9 test run, not skipped; then the
  full suite, `uv run ruff check .` and `uv run ruff format --check .` show no new failures.
- **Outcome:** Added the skill, `test_cloudflare_skill.py` (headings, rule phrases, no
  `--force` examples, secret reads only in Principles, an extraction test, and a `--help`
  check per command), the README's Skill, Token permissions and Tests sections, and the
  docs entries. With `cf` 1.0.0-beta.21 on PATH, `plugins/cloudflare` gave 81 passed, and
  all 56 command checks ran. Without `cf` on PATH it gave 25 passed, 56 skipped. The full
  suite gave 2192 passed, 1 failed (baseline). Ruff check and format pass. `rg -ci
  cloudflare` hits each doc, `docs/auth-credentials.md` 5 times.

## Deviations

- M3: the skill does not say "write commands exit 0 without doing anything". Under
  1.0.0-beta.21, probes showed that destructive commands (`delete`) print "Aborted." and exit 1
  without `--force`, and create/update commands don't prompt at all. That contradicts the
  discovery note made before the plan review. The rule phrase became "Create and update
  commands don't ask: only the token stops them", Errors handles "Aborted.", and the
  Requirements phrase list and Error handling were updated. Scope is unchanged.
- M3: the D9 command check also asserts that the help header names the full command path,
  because `cf <group> <unknown> --help` prints the group's help and exits 0.

## Risks

- The image build is not run here (no Docker in this container); the `Dockerfile` change is
  checked by installing the same pinned package with npm and running `cf --version`.
- No real Cloudflare token is available, so no command is run against the live API; the
  skill's commands are checked only for existence and help output.
- Cloudflare's permissions reference lists no token group for Workflows, Workers VPC or
  Registrar. The Workflows API docs accept `Workers Scripts Read`, and the Workers VPC docs
  name a `Connectivity Directory Read` role; the README marks the VPC and Registrar groups
  as unconfirmed.
- `cf` is beta: a version bump may rename commands; D9's check must be re-run then.
- `cf@1.0.0-beta.21` adds about 210 MB to the image (138 MB is `workerd`, pulled in through
  `miniflare` for `--local` simulation, which the read-only skill never uses). Every new
  container host pulls it once.

## Review

- Plan review: 1 pass, 0 ❌, 4 ⚠️, 9 ℹ️, all accepted. The skill phrases and area
  headings are now named exactly; the command check is a committed pytest (D9 revised), with
  the extraction rule stated; the `cf` tool-category AC has a new assertion; the old M2
  split into M2 (image and environment) and M3 (skill and docs); the doc edits are listed
  per file, including the `docs/plugins.md` no-code line; the ordered
  `EXPECTED_PROVIDERS` dict is named; the tests dir without fakes is stated; a no-`--force`
  error case was added (exit code corrected in Deviations); token permissions were added as D10 and tied to the
  account-id fallback.
- Code review: one round, roles `general` and `risk` (risk ran because the change touches
  secrets, the container environment and a CLI-facing skill). No ❌. Fixed: ⚠️ account-id
  `export` lost between shell calls (Workflow step 2 now prefixes
  `CLOUDFLARE_ACCOUNT_ID=<id>` per command); ℹ️ missing Deviations section and stale exit-0
  wording in the plan; ℹ️ `cf schema <cmd>` examples now also check the named command;
  ℹ️ build-time `cf --version` now runs with `CF_SEND_TELEMETRY=false`; ℹ️ image size added
  to Risks. ❓ `cf accounts list` output shape: answered by running `cf` against a local
  fake API, which printed the unwrapped `result` array, so `jq '.[]'` is right. ❓ login
  storing an Edit token from the host shell: accepted per D1/requirement 2, with a warning
  added to the README and `docs/auth-credentials.md`. Rejected: none.
- Merge risk: Medium: the plugin hands the agent's shell a Cloudflare token, and the only
  limit on `cf` is that token's permissions, so a token stored with Edit scope would let a
  mistaken command change production; this is limited by the documented read-only scopes,
  the skill's no-change rules, and destructive commands needing `--force`.

## Outcome

Delivered a no-code `cloudflare` plugin. It stores a static `api_token` credential projected as
`CLOUDFLARE_API_TOKEN` and an `account_id` setting projected as `CLOUDFLARE_ACCOUNT_ID`. It
ships a `cloudflare` skill with read commands for the 12 requested areas and read-only rules.
`cf@1.0.0-beta.21` is pinned in the image with telemetry off, `cf` is listed in the agent's
environment, and the docs are updated.

Evidence: the full pytest run with `cf` on PATH gave 2192 passed, 1 failed (the baseline
`test_persona_skills_holds_only_procedure_skills`). Ruff check and format pass. All 56 skill
commands resolve under the pinned CLI. `cf accounts list` against a local fake API printed
the unwrapped array.

Not verified: the Docker image build (no Docker here), and any command against the live
Cloudflare API (no token).

PR: https://github.com/simon-downes/archie-nexus/pull/10
CI: pending

## Follow-ups

- Narrow write scopes (for example queue purge or cache purge) once wanted; they would come
  from a second token or extra permission groups, per D1.
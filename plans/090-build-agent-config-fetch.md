# 090 — Agent config and credentials from the orchestrator

Status: planning
Branch: build-it/090-agent-config-fetch (base: main @ 8cdad07)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/090-agent-config-fetch

## Objective

Agent containers stop reading `config.yaml` and `credentials.yaml` from the mounted
`~/.nexus`. The agent fetches config and credentials from the orchestrator over an
authenticated HTTP route, keeps them in an in-memory pull-through cache, and serves
subprocesses (the exec runner, the `gh` wrapper, the AWS `credential-process`) through a
Unix socket. A missed call site fails loudly instead of silently reading the still-mounted
files.

## Context

The user's goal is to prevent incidental exposure of config and credentials to the model
(a model `cat`-ing or editing the files while debugging, or following an injected
instruction), not to defend against a determined model. It is a single-user local system;
ease of implementation is the priority. Design agreed in discussion (2026-11-15):

- The orchestrator issues a per-session token, `ARCHIE_AGENT_TOKEN`, and stores it in the
  `sessions` table. The agent sends it as a bearer token on `/api/agent/*`.
- The agent keeps config and credentials in memory: a credential miss fetches from the
  orchestrator (absence is cached too); a sync every 5 minutes asks "anything newer than
  these versions?"; versions are content hashes. A cached OAuth credential whose
  `expires_at` is under 3 minutes away is synced when used.
- Processes other than the agent reach the cache through a Unix socket served by the
  agent with GET-only routes, never through the agent's `0.0.0.0:8080` server (published
  to host loopback).
- The token is removed from the agent's `os.environ` at startup so shells don't see it.
- A council (app developer, QA, SRE, pragmatist; 4/4) chose: `shared` stays file-based for
  host callers; agent call sites switch explicitly to an agent-owned cache module; a
  fail-loud trip-wire in `shared` (`ARCHIE_AGENT_CONTAINER=1`) raises when a shared file
  reader runs in a container; the root `conftest.py` clears the flag and targeted tests set
  it; the agent refuses to start without the flag.
- The `~/.nexus` mount stays as it is; removing the two files from the container is later
  work. `archie.db`, the brain, and the `agent/`/`persona/` mounts are out of scope.

Codebase facts:

- Containers reach the host orchestrator at `http://host.docker.internal:7600` (verified
  from this container on colima, about 1 ms per request). `prepare_start`
  (`orchestrator/.../lifecycle.py`) builds the `docker run` command; `run_container`
  (`docker.py`) runs it and never logs argv.
- Agent readers of the files: `app.py` lifespan (`load_nexus_config`),
  `tool_policy_live.py` (policy before each exec), `skills.py` (enabled plugins),
  `exec/tools/_runtime_env.py` (calls `shared` `runtime_environment()`, which calls
  `get_credential`), `llm/bedrock.py`, `llm/bedrock_openai.py`, `llm/openai_api.py`,
  `kiro/harness.py` (`resolve_api_key`), `exec/plugin.py` `CredentialSource.get()` (runs
  in the exec runner subprocess). Bash: `container/bin/gh`,
  `plugins/aws/bin/credential-process`. The Kiro MCP server runs inside the agent process.
- `/run` is root-owned and not writable by the container user; `/tmp` is.
- OAuth refresh on the host: every 60 s, for tokens expiring within 5 minutes
  (`credential_refresh.py`).

## Baseline

- Check commands: `env -u ARCHIE_BACKEND scripts/dev test -q -p no:cacheprovider`;
  `scripts/dev ruff check .`; `scripts/dev ruff format --check <touched files>`
- Setup: `scripts/dev sync`
- Setup leftovers: `.venv-archie` (created by sync; gitignored)
- Failing before this work: none with `ARCHIE_BACKEND` unset (5531 passed). This session
  runs on Kiro, so its environment has `ARCHIE_BACKEND=kiro`, which makes 14 tests fail and
  9 error in `tests/test_question_tool.py` and `tests/test_ws_integration.py`
  (pre-existing, unrelated); checks run with it unset.
- Not included: `.DS_Store`, `plans/060-spec-container-aws-profiles.md`,
  `plans/070-build-nexus-plugins.md` (untracked in the user's checkout)

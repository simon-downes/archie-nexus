# 067 — incident.io read-only tooling and skill

Status: planning
Branch: build-it/067-incident-io-tooling (base: main @ a1d97e6)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/067-incident-io-tooling

## Objective

Give the agent read-only access to incident.io through an exec-only `incident_io` namespace and a
matching `persona/skills/incident_io/SKILL.md`, following the existing Jira/Slack tool patterns.
When done, model code can list and read incidents, their timelines and related records, and the
skill documents the full public surface, limits and errors.

## Context

The user asked: "let's build the incident.io tool and skill; follow the same structure/patterns as
the existing tools (google, slack, jira, etc); credentials should already be available — they
provide read-only access; use them for testing as needed but with suitable care."

The design scope is the brain entry `projects/archie/datadog-incident-operations-tools.md`
(incident.io section): read-only wrapper, no writes of any kind (declare, update, timeline,
escalations, follow-ups, status pages, catalog, config).

Codebase facts:

- Commit a1d97e6 added the `incident_io` static provider (`api_key` field) and
  `IncidentIoCredential` in `shared/src/archie_shared/credentials/models.py`. API host is
  `https://api.incident.io/`, Bearer auth (`docs/auth-credentials.md`).
- Closest pattern: `agent/src/archie_agent/exec/tools/jira/` (static credential, httpx client,
  sanitized typed errors, `policy.py` using `archie_shared.tool_policy.resolve_provider_policy`,
  `@tool(native=False, exec_docs=False, namespace="jira")`). Slack's `errors.py` shows a shared
  error base class and `retry_after_seconds` on rate limits.
- Tool packages are registered by import in `agent/src/archie_agent/exec/tools/__init__.py`
  `get_all_tools()`.
- `tools:` config is an open provider map (`shared/src/archie_shared/schemas.py`), so
  `tools.incident_io` needs no schema change. Missing policy = read enabled, write disabled.
- Live credential check: `/v1/identity` returned 200 with viewer-only roles; rate limit header
  shows 1200 requests per 60s window.
- The public OpenAPI spec is at `https://api.incident.io/v1/openapiV3.json` (147 GET endpoints).

## Baseline

- Check commands: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`
- Setup: `uv sync`
- Setup leftovers: none (`.venv` is ignored)
- Failing before this work: none (1328 passed; ruff clean)
- Not included: `persona/skills/aws-agent-access/`, `plans/060-spec-container-aws-profiles.md`
  (untracked in the user's checkout, unrelated)

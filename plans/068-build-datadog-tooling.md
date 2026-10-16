# 068 — Datadog tooling and skill

Status: planning
Branch: build-it/068-datadog-tooling (base: main @ 89cf27a)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/068-datadog-tooling

## Objective

Give the agent Datadog access through an exec-only `datadog` namespace and a matching
`persona/skills/datadog/SKILL.md`, following the existing Jira and incident.io tool patterns.
When done, model code can query metrics, logs, spans, events, monitors, dashboards and the
software catalog for incident investigation and service health checks, and can make bounded,
policy-gated monitor and dashboard writes. No delete operation exists.

## Context

The user asked: "let's build the datadog tool and skill. follow the existing patterns/structures
like for the slack, notion, jira, etc tools and skills." They noted that app-key scopes can't
restrict deletions, "but that's why we expose a tool and not the raw credentials to the model".

The design scope is the brain entry `projects/archie/datadog-incident-operations-tools.md`
(Datadog section): read-heavy wrapper; bounded monitor and dashboard writes behind an ownership
boundary; no deletes; no SLOs; service checks and event posting deferred. Its open decisions
(ownership tag, naming convention, raw query language, limits) are resolved in Decisions below.

Codebase facts:

- Commit a1d97e6 added the `datadog` static provider (`api_key`, `app_key`) with non-secret
  `site` defaulting to `app.datadoghq.eu` (`shared/src/archie_shared/credentials/providers.py`),
  and `DatadogCredential` (`shared/src/archie_shared/credentials/models.py`). The API host is
  derived from the site (`https://api.datadoghq.eu`; `docs/auth-credentials.md`).
- Closest patterns: `agent/src/archie_agent/exec/tools/jira/` (static credential, httpx client,
  policy with read/write gates, guarded mutations with `...MutationIndeterminateError`) and the
  unmerged incident.io build on branch `build-it/067-incident-io-tooling` (PR #5, open):
  `incident_io/client.py` (error base class, `parse_retry_after`, 60 s deadline),
  `validation.py`, `policy.py`, and its skill and skill contract test.
- Tool packages are registered by import in `agent/src/archie_agent/exec/tools/__init__.py`
  `get_all_tools()`. `tools:` config is an open provider map, so `tools.datadog` needs no schema
  change. Missing policy = read enabled, write disabled
  (`shared/src/archie_shared/tool_policy.py`).
- Exec results are truncated at 16,000 characters.

Live API facts (checked with the user's key on `api.datadoghq.eu`, status codes and shapes only):

- 200: `/api/v1/validate`, `/api/v1/monitor`, `/api/v1/monitor/search` (774 monitors),
  `/api/v1/dashboard`, `/api/v1/metrics`, `/api/v2/catalog/entity`, `/api/v2/team`,
  `/api/v1/logs/config/indexes`, `POST /api/v2/logs/events/search`,
  `POST /api/v2/spans/events/search`, `/api/v2/events`, `POST /api/v2/query/timeseries`,
  `/api/v1/query`. The v2 timeseries query works even though `timeseries_query` is not in the
  key's listed scopes.
- 403: `/api/v2/downtime` (key lacks `monitors_downtime`).
- Rate limits are per endpoint and tight on some: spans search 5/60 s, logs search 10/10 s,
  metrics list 10/30 s, dashboards 60/60 s.

## Baseline

- Check commands: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`
- Setup: `uv sync`
- Setup leftovers: none (`.venv` is ignored)
- Failing before this work: none (1329 passed; ruff clean)
- Not included: `persona/skills/aws-agent-access/`, `plans/060-spec-container-aws-profiles.md`
  (untracked in the user's checkout, unrelated)

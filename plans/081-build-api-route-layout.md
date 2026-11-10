# 081 — Orchestrator API route layout

Status: building
Branch: build-it/081-api-route-layout (base: main @ a1d18e4)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/081-api-route-layout

## Objective

The orchestrator mixes JSON routes and web pages at the root, with pages under `/ui`. Move
every JSON route under `/api`, serve the web pages at the root without `/ui`, and split the
route table into an API module and a page module. When done, code clients (CLI, TUI, the
auth page's script) call `/api/...`, browsers use `/sessions`, `/insights/...` and `/auth`.

## Context

Roadmap item 1 (brain: `projects/archie/nexus-roadmap/api-route-layout.md`). The user asked
to implement it with three fixed points: `/`, `/health` and `/auth/callback/{provider}` stay
exactly as they are. Keeping the callback means no OAuth redirect URI re-registration
(`auth.py:140` builds it; `plugins/google/README.md:30` documents it) and the callback log
filter at `app.py:65` stays valid. Backward compatibility doesn't matter (single-user
system), so the old paths are removed rather than redirected.

Codebase facts:

- All routes are in one list at `orchestrator/src/archie_orchestrator/app.py:509-544`.
  JSON handlers live in `app.py`; proxy handlers in `proxy.py`; page handlers in `web.py`
  and `insights_web.py` (which imports from `web.py`).
- `/ui/sessions` already returns its `#results` fragment for `HX-Request` (`web.py:250`).
- Clients build URLs by hand: `cli/src/archie_cli/cli.py` (`_profile_url` plus literal
  paths; `ws_url`/`api_url` for the TUI), `cli/src/archie_cli/auth.py` (`_orchestrator_url`
  plus literal paths), `templates/auth_status.html` (`fetch('/auth/...')`).
- Templates and `insights_web.VIEWS` hard-code `/ui/...` links; `templates/viewer/detail.html`
  carries the live SSE URL in `data-live-url`.
- Tests patch `archie_orchestrator.app.{list_sessions,start_session,stop_session,home_dir}`
  and import `_query_metrics` and `unhandled_exception_handler` from `app`. Lifespan tests
  patch `app.list_sessions` (`test_orchestrator_bounce.py:48,83`,
  `test_orchestrator_logging.py:168`) and keep doing so; `GET /sessions` tests need
  `api.list_sessions` (`test_orchestrator_bounce.py:65`, `test_orchestrator_remote.py:56,72`,
  `test_orchestrator_app.py:49,73,92`). `app.config_source.get` patches keep working because
  they patch the shared object.
- No test sends a request to `/auth/login`, `/auth/refresh`, `/auth/flow` or
  `/auth/callback`; `AuthService` tests call it directly.
- Docs listing routes: `docs/architecture.md`, `docs/auth-credentials.md`,
  `docs/session-protocol-walkthrough.md`, `docs/config.md`, `docs/kiro.md`, and
  `README.md:52` (a `/ui/insights` link). `insights.py:1` names `/ui/insights` in a docstring.

## Baseline

- Check commands: `env -u ARCHIE_BACKEND scripts/dev test`, `scripts/dev ruff check .`,
  `scripts/dev ruff format --check <touched files>`
- Setup: `scripts/dev sync`
- Setup leftovers: none (`git status --short` clean after sync and tests)
- Failing before this work: none with `ARCHIE_BACKEND` unset (4783 passed). This Kiro session
  exports `ARCHIE_BACKEND`, which makes 23 agent tests in `tests/test_question_tool.py` and
  `tests/test_ws_integration.py` fail; that is environmental, so checks unset it.
  `ruff format --check .` already flags `persona/skills/python/assets/output.py`.
- Not included: `plans/060-spec-container-aws-profiles.md`, `plans/070-build-nexus-plugins.md`
  (untracked in the user's checkout)
## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Which root paths are exempt from the move | `/`, `/health` and `/auth/callback/{provider}` stay exactly as they are | moving `/health` to `/api/health`; moving the callback and re-registering the redirect URI | the user fixed these; keeping the callback avoids re-registering OAuth redirect URIs | user (request) |
| D2 | What happens to the old paths (`/ui/*`, root JSON routes) | removed; they return 404 (or the new page where a page now owns the path) | redirects or aliases | single-user system, so no compatibility window is needed (roadmap note) | self |
| D3 | Where the route tables live | new `api.py` (the JSON handlers moved out of `app.py`, plus `routes`) and new `pages.py` (the page `routes`, plus the OAuth callback handler); `app.py` keeps the lifespan, `/health`, the exception handler and composes `[*pages.routes, /health, Mount(API_PREFIX, api.routes), /static]` | route lists inside `web.py` | `insights_web.py` imports from `web.py`, so a page table in `web.py` would need a circular import; two modules make the table read as two blocks, as the note asks | self |
| D4 | Which module owns `auth_callback` | `pages.py` | `api.py` | it returns HTML and a browser lands on it; API handlers never render HTML | self |
| D5 | Where the `/api` prefix is defined | `API_PREFIX = "/api"` in `shared/src/archie_shared/protocol.py`, used by the orchestrator mount and the CLI; the auth page's inline script keeps a literal `/api/auth/...` | a literal in each Python package | the CLI and the orchestrator are separate packages that must agree, and `shared` is the only package both import; `protocol.py` is the closest existing home for client/server wire constants | self |
| D6 | How the CLI builds request URLs | keep `_profile_url()` / `_orchestrator_url()` returning the orchestrator base (used in error messages) and build each request as `f"{url}{API_PREFIX}/..."` | a helper that returns the API base | error messages keep naming the orchestrator address, and tests that patch `_orchestrator_url` still check the prefix | self |
| D7 | How templates refer to pages | literal root paths, as now | Starlette `url_for` with named routes | `_render_viewer` renders in a worker thread without a request, so `url_for` is not available there | self |
| D8 | Error body for unhandled exceptions on page routes | unchanged (JSON 500 for every path) | HTML 500 for pages | not part of the route move; listed as a follow-up | self |

## Requirements

- MUST serve every JSON route under `/api`, with unchanged request and response bodies:
  `GET /api/sessions`, `POST /api/sessions`, `DELETE /api/sessions/{id}`,
  `GET /api/sessions/{id}/status`, `GET /api/sessions/{id}/events`,
  `GET /api/sessions/{id}/metrics`, `GET /api/metrics`, `GET /api/auth/providers`,
  `GET /api/auth/status`, `GET /api/auth/flow/{flow_id}`, `POST /api/auth/login/{p}`,
  `POST /api/auth/refresh/{p}`, `PUT /api/auth/credential/{p}`,
  `DELETE /api/auth/credential/{p}`.
  - AC: the existing orchestrator tests for these handlers pass against the `/api` paths.
  - AC: new tests in `tests/test_orchestrator_routes.py`, with `google` configured
    (`client_id`), get the handlers' own JSON answers: `POST /api/auth/refresh/google` with no
    stored credential → 400 JSON; `GET /api/auth/flow/unknown` → 400 JSON.
  - AC: the former root JSON routes (`GET /metrics`, `GET /auth/status`,
    `GET /auth/providers`, `POST /auth/login/google`, `POST /auth/refresh/google`,
    `GET /auth/flow/x`, `DELETE /sessions/x`, `GET /sessions/x/status`) return 404 or 405
    with a non-JSON body, i.e. Starlette's no-route response, not a handler's JSON 404.
- MUST serve the session WebSocket relay at `/api/sessions/{id}/stream`.
  - AC: the WS proxy tests connect at `/api/sessions/{id}/stream`; a connection to
    `/sessions/{id}/stream` is refused.
- MUST serve the pages at the root: `/sessions`, `/sessions/{id}`, `/sessions/{id}/live`,
  `/insights`, `/insights/sessions`, `/insights/sessions/{id}`, `/insights/models`,
  `/insights/tools`, `/insights/subagents`, `/insights/errors`, `/auth`.
  - AC: the web tests pass at these paths and get `text/html` (or `text/event-stream` for
    `/live`); every former `/ui/...` path returns 404.
- MUST keep `/`, `/health` and `/auth/callback/{provider}` unchanged.
  - AC: `GET /` renders the running-sessions page and `GET /health` returns
    `{"status":"ok"}` (existing tests).
  - AC (new, M1): `POST /api/auth/login/google` (google configured with `client_id`) returns
    a `redirect_uri` of `http://test/auth/callback/google`.
  - AC (new, M1): a round trip on one app — login through `/api/auth/login/google`, then
    `GET /auth/callback/google?state=<state from the authorization URL>&code=x` with a fake
    token transport — returns 200 "Authentication successful", and
    `GET /api/auth/flow/{flow_id}` then reports `succeeded`.
  - AC (new, M2): `GET /auth/callback/google?state=bad` returns 400 `text/html`
    ("Invalid or expired flow").
- MUST keep htmx fragments with their page: `GET /sessions` with `HX-Request: true` returns
  only the results fragment.
  - AC: the session-list fragment test passes at `/sessions`.
- MUST point every link, form action, `hx-get`, `fetch` and live URL in the templates and
  static JS at the new paths, including the auth page's "Refresh page" link (`/auth`) and its
  `fetch` (`/api/auth/...`).
  - AC: the existing web tests (`tests/test_web_console.py`, `test_web_auth_status.py`,
    `test_web_session_list.py`, `test_web_session_detail.py`, `test_web_insights.py`), using
    their own fixtures, assert the rendered page contains no `/ui/`;
    `rg -n '/ui/' orchestrator/src` finds nothing.
- MUST make the CLI and TUI call the `/api` paths: `archie start`, `ls`, `attach`, `stop`,
  the `--kiro` check, `archie auth login|refresh|status` and static credential pushes; the TUI
  gets `ws_url`/`api_url` under `/api/sessions/{id}`.
  - AC: CLI tests assert the requested URLs include `/api/`.
- MUST compose the app from the two route modules: JSON routes only in `api.py` under
  `Mount(API_PREFIX)`, page routes only in `pages.py`.
  - AC (M1): a route-table test checks that `app.routes` contains a `Mount` at `API_PREFIX`
    whose routes are `api.routes`.
  - AC (M2): the same test checks that the remaining top-level routes are `pages.routes`,
    `/health` and `/static`, and that none starts with `/ui`.
- MUST update the docs that list routes.
  - AC: `rg -n '/ui/' docs README.md orchestrator/src cli/src` finds nothing, and the
    `docs/architecture.md` route table lists the `/api` routes.

Out of scope: authentication and Origin/Host checks (roadmap item 2), new routes, HTML error
pages for unhandled exceptions, redirects for old paths.

## Design

### Overview

A mechanical move plus a module split. `app.py` becomes composition only; `api.py` holds the
JSON handlers and their table, mounted at `/api`; `pages.py` holds the page table (handlers
stay in `web.py` and `insights_web.py`) and the OAuth callback. Clients and templates switch
to the new paths, and tests move with them.

### Code structure

- `shared/src/archie_shared/protocol.py`: add `API_PREFIX = "/api"`.
- `orchestrator/src/archie_orchestrator/api.py` (new): `sessions_get`, `sessions_post`,
  `session_delete`, `_query_metrics`, `get_metrics`, `get_session_metrics`, the `auth_*` JSON
  handlers, and `routes` (including `proxy_status`, `proxy_events` and the `proxy_stream`
  `WebSocketRoute` from `proxy.py`). Paths in `routes` are relative to the mount
  (`/sessions`, `/auth/status`, ...).
- `orchestrator/src/archie_orchestrator/pages.py` (new): `auth_callback` (moved from
  `app.py`) and `routes` for `/`, the viewer, insights, `/auth` and `/auth/callback/{p}`.
- `orchestrator/src/archie_orchestrator/app.py`: lifespan, metrics loop, `CallbackAccessFilter`,
  `health`, `unhandled_exception_handler`, and the composed `Starlette(routes=...)`.
- `orchestrator/src/archie_orchestrator/__init__.py`: add a service colour for
  `archie_orchestrator.api` and `archie_orchestrator.pages` loggers.
- Clients: `cli/src/archie_cli/cli.py`, `cli/src/archie_cli/auth.py`.
- Templates/static: `templates/base.html`, `templates/auth_status.html`,
  `templates/viewer/{base,list,_results,detail,not_found}.html`,
  `templates/insights/{_macros,models,overview,session,sessions,not_found}.html`,
  `insights_web.VIEWS` and docstrings in `web.py`, `insights_web.py` and `insights.py`.
- Docs: `README.md`, `docs/architecture.md`, `docs/auth-credentials.md`,
  `docs/session-protocol-walkthrough.md`, `docs/config.md`, `docs/kiro.md`.

## Milestones

### [ ] M1 — JSON API under /api, with its clients

- **Approach:** Add `API_PREFIX`. Create `api.py` by moving the JSON handlers out of
  `app.py` unchanged; build `routes` from the current JSON entries with the prefix stripped.
  In `app.py`, replace those entries with `Mount(API_PREFIX, routes=api.routes)`; keep the
  page routes and `/auth/callback` inline for now. Update `cli.py` and `auth.py` request URLs
  to `f"{url}{API_PREFIX}/..."` (D6), the TUI `ws_url`/`api_url` built in `cli.py`, and the
  auth page `fetch` to `/api/auth/...`. Move test patch targets from
  `archie_orchestrator.app.<name>` to `archie_orchestrator.api.<name>` where the name moved
  (`list_sessions` stays patched in `app` for lifespan tests; see Context for the files).
  Update test paths. Add `tests/test_orchestrator_routes.py` for the old-path 404/405 checks,
  the mount check (the `Mount` only; the no-`/ui` part waits for M2), the refresh/flow
  checks, the login `redirect_uri` check and the login→callback round trip. Set app state
  with `monkeypatch.setattr(app.state, ..., raising=False)` as in
  `tests/test_plugin_enabled.py:57-58`, with `ARCHIE_HOME_DIR` at `tmp_path` and
  `AuthService(NexusConfig(plugins={"google": PluginConfig(auth=PluginAuthConfig(client_id="client"))}), transport=FakeTransport())`
  following `tests/test_orchestrator_auth_oauth.py:22-28,80-93`. Login itself makes no HTTP
  call for google; only the callback's token exchange uses the transport.
  Update the API rows in `docs/architecture.md`, `docs/auth-credentials.md`,
  `docs/session-protocol-walkthrough.md`, `docs/config.md` and `docs/kiro.md`.
  ⚠️ Starlette matches routes in order; the `Mount` must not shadow a page route (no page
  path starts with `/api`).
- **Test seam:** the ASGI app through `httpx.AsyncClient(ASGITransport)` / `TestClient`;
  CLI commands through `CliRunner` with `httpx` patched.
- **Deliverable:** the JSON API answers only under `/api`, and the CLI/TUI use it.
- **Verify:** `env -u ARCHIE_BACKEND scripts/dev test` passes (no new failures);
  `scripts/dev ruff check .` passes; `scripts/dev ruff format --check <touched files>` passes.
- **Outcome:**

### [ ] M2 — Pages at the root without /ui

- **Approach:** Create `pages.py` with the page `routes` and move `auth_callback` into it.
  Page paths: `/`, `/sessions`, `/sessions/{session_id}`, `/sessions/{session_id}/live`,
  `/insights` (+ the six sub-pages), `/auth`, `/auth/callback/{provider}`. `app.py` uses
  `*pages.routes`. Replace every `/ui/...` link in the templates and `insights_web.VIEWS`;
  point the auth page's "Refresh page" link at `/auth` (it pointed at the JSON route). Update
  docstrings (`web.py`, `insights_web.py`, `insights.py`), the web tests' paths, the page rows
  in `docs/architecture.md`, and the `README.md:52` link. Extend
  `tests/test_orchestrator_routes.py`: former `/ui/...` paths return 404; `GET /sessions`
  returns HTML; `GET /auth/callback/google?state=bad` returns 400 HTML; the route-table test
  checks the top-level routes are `pages.routes`, `/health` and `/static` with no `/ui`. Add a
  "no `/ui/`" assertion to one rendering test in each of `tests/test_web_console.py`,
  `test_web_auth_status.py`, `test_web_session_list.py`, `test_web_session_detail.py` and
  `test_web_insights.py` (`test_web_session_live.py` only streams, so it just gets new paths).
  ⚠️ `/sessions/{session_id}` (page) and `/api/sessions/{id}` no longer collide because the
  API is mounted; `/auth` (page) and `/auth/callback/{p}` are distinct paths.
- **Test seam:** the ASGI app through `httpx.AsyncClient(ASGITransport)` / `TestClient`.
- **Deliverable:** every page is served at its root path and links only to root paths.
- **Verify:** `env -u ARCHIE_BACKEND scripts/dev test` passes; `scripts/dev ruff check .`
  passes; `scripts/dev ruff format --check <touched files>` passes;
  `rg -n '/ui/' docs README.md orchestrator/src cli/src` finds nothing.
- **Outcome:**

## Deviations

## Risks

- An orchestrator started before this change and a CLI after it (or the reverse) disagree on
  every path: the CLI gets 404s until `archie serve` is restarted. Accepted (single user;
  both come from the same checkout).
- Browser bookmarks to `/ui/...` stop working (D2).

## Review

- Plan review: 1 pass. Fixed: no HTTP test covered the OAuth callback or login (❌, added
  new tests in M1/M2); login/refresh/flow had no `/api` route test (⚠️, added reachability
  checks); `README.md:52` and `insights.py:1` were missing (⚠️, added); format check added to
  Verify; "no `/ui/`" assertions placed in the existing web tests; full paths spelled out; D5
  reason restated and the template literal accepted; Context lists the patch-target files.
  Pass 2: old-path checks must tell Starlette's no-route 404 from a handler's JSON 404
  (⚠️, ACs now require a non-JSON body); refresh/flow checks name inputs and expected codes
  (⚠️); the "patched token endpoint" phrase was wrong for login (fixed: the transport only
  serves the callback); app-state setup named; the five web test files named; the route-table
  AC split between M1 and M2; a login→callback round-trip test added. Stopped after two passes.

## Outcome

PR: pending
CI: pending

## Follow-ups

- Return an HTML error page, not JSON, for unhandled exceptions on page routes (D8).
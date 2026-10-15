# 066 — Owner-aware gh wrapper with work and personal GitHub tokens

Status: reviewing
Branch: build-it/066-gh-account-wrapper (base: main @ 23b01dd)
Worktree: /workspace/archie-nexus/.git/build-it-worktrees/066-gh-account-wrapper
Updated: 2026-10-15

## Objective

Archie stores one GitHub token and exports it to every container as `GH_TOKEN`, so `gh`
can reach either Tillo repositories or personal (`simon-downes`) repositories, not both.
When done, the credential store holds a work token and a personal token, and a `gh`
wrapper at `/usr/local/bin/gh` picks one per call from the target repository's owner,
reading the token from `credentials.yaml` at call time. No GitHub token sits in the
container environment.

## Context

User request (2026-10-15, after the brain entry
`projects/archie/github-multi-account-credentials.md`):

- the real gh binary becomes `gh-base` (or similar); the wrapper lives in `/usr/local/bin/`;
- the wrapper is transparent and passes every argument through unchanged;
- credentials `github.personal_token` and `github.work_token` replace `github.token`;
- remove the `GH_TOKEN` env projection; the wrapper reads credentials and sets the env var
  only for the one gh call, using a targeted `yq` read each call;
- provider config gains `work_orgs: list[str]`; repositories whose remote matches a work
  org use `work_token`, everything else uses `personal_token`;
- the Dockerfile installs or moves gh accordingly.

OAuth was ruled out: the user is an enterprise owner and an OAuth or classic token would
carry that authority. Git push uses an SSH key, so only `gh` needs routing.

Codebase facts:
- `shared/src/archie_shared/credentials/providers.py` — `github` is a `StaticProvider`
  with `fields=["token"]`, `env={"token": "GH_TOKEN"}`, `set_env=True`.
- `shared/src/archie_shared/credentials/models.py` — `GithubCredential(token)`,
  `forbid_unknown_fields=True`.
- `shared/src/archie_shared/credentials/runtime.py` — `runtime_environment()` exports
  `set_env` providers at container start (`orchestrator/.../lifecycle.py:127`).
- `shared/src/archie_shared/credentials/api.py` — `AuthProviderOverride` (config
  `auth.providers.<name>`) is `forbid_unknown_fields=True`, so `work_orgs` must be added
  there or config loading rejects it.
- `cli/src/archie_cli/auth.py` — a static provider with an `env` mapping is read from env
  vars; without one it takes JSON stdin or prompts per field (secret fields hidden).
- `orchestrator/.../auth.py:489` — every `provider.fields` entry is required on PUT.
- `Dockerfile:109-114` installs gh from the GitHub apt repo to `/usr/bin/gh`; `yq` is
  installed at `/usr/local/bin/yq` earlier (line 90).
- Container paths: credentials at `$ARCHIE_HOME_DIR/credentials.yaml`, config at
  `$ARCHIE_HOME_DIR/config.yaml`; `ARCHIE_HOME_DIR=/home/archie/.nexus` is set by the
  orchestrator.

## Baseline

- Check commands: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check <files>`
- Setup: `uv sync`
- Setup leftovers: none
- Failing before this work: none (1262 passed; ruff clean)
- Not included: `persona/skills/aws-agent-access/`, `plans/060-spec-container-aws-profiles.md` (untracked in the user's checkout; unrelated)

## Decisions

| #  | Decision | Chosen | Rejected | Why | Source |
|----|----------|--------|----------|-----|--------|
| D1 | Token selection model | Two tokens, owner-based wrapper | OAuth login; GitHub App | Enterprise-owner scope must not reach the agent; fine-grained tokens are per owner | user |
| D2 | Credential fields | `github.personal_token`, `github.work_token` | keep `token` | User specified | user |
| D3 | Env projection | Remove (`env={}`, `set_env=False`); wrapper sets `GH_TOKEN` only for the exec'd gh | keep projecting | User specified; keeps tokens out of the container env | user |
| D4 | Unmatched owner | Use `personal_token` | fail closed (brain entry) | User specified "everything else" | user |
| D5 | Where `work_orgs` lives | `auth.providers.github.work_orgs` in `config.yaml`, typed on `AuthProviderOverride` | separate top-level `github:` config | User said "provider config"; `AuthProviderOverride` is the existing per-provider config struct | user/self |
| D6 | Wrapper language | bash (arrays and `[[ ]]` allowed) + `yq` | Python script | User suggested targeted yq; bash starts in ms and needs no venv; venv is on PATH but not guaranteed for every shell | self |
| D7 | How the real gh is renamed | `dpkg-divert --rename` `/usr/bin/gh` → `/usr/bin/gh-base` | `mv` after install | `mv` is undone by any later `apt-get upgrade`, which would reinstate `/usr/bin/gh`; a diversion survives upgrades | self |
| D8 | How the wrapper finds the real gh | `gh-base` on `PATH` | hard-coded `/usr/bin/gh-base`; test-only env var | PATH lookup lets tests put a fake `gh-base` first without a test-only knob | self |
| D9 | Owner source order (revised, C1) | `gh api` endpoint `repos\|orgs\|users/<owner>` or `gh repo <sub> <owner/repo>` positional → `-R/--repo` arg → `GH_REPO` → git remote | remote only; `-R`/`GH_REPO` first | Matches what gh itself targets: a literal endpoint or repo argument is what gh calls, and explicit repo flags override the cwd repo | self |
| D10 | Which git remote (revised, C2) | Among GitHub-host remotes only: any `gh-resolved` remote, then `upstream`, `github`, `origin`, then first | `origin` only; name order over all remotes | Mirrors gh's own base-repo choice so the token matches the repo gh talks to (a fork's PRs go to `upstream`); gh ignores non-GitHub remotes | self |
| D11 | Owner match | Case-insensitive, exact owner | substring / glob | GitHub owner names are case-insensitive; substring would let `tillo-fork` match `tillo` | self |
| D12 | Caller-set `GH_TOKEN`/`GITHUB_TOKEN` | If either is non-empty, the wrapper sets nothing and execs `gh-base` with the env as-is | always overwrite | With projection removed, a set token is deliberate (e.g. a one-off `GH_TOKEN=… gh …`); setting `GH_TOKEN` beside a caller's `GITHUB_TOKEN` would silently override it because gh prefers `GH_TOKEN` | self |
| D13 | Selected token missing | Unset `GH_TOKEN`, warn once on stderr naming the field, still run gh | exit non-zero | `gh --version`, `gh help` and public reads work without auth; gh prints its own auth error otherwise | self |
| D14 | Required fields | Both `personal_token` and `work_token` required on login | optional fields | `StaticProvider.fields` doubles as the required list (`auth.py:489`); optional fields would need a new provider concept | self |
| D15 | `work_orgs` on non-github providers | Reject at config load | silently ignore | `AuthProviderOverride` is shared by all providers; a misplaced key would otherwise do nothing silently | self |
| D16 | Old `github.token` entries | Not migrated; status shows "Invalid stored credential" until `archie auth login github` is rerun | auto-migrate token into one field | Unknown which account the old token belongs to; a wrong guess routes requests to the wrong owner | self |
| D17 | Wrapper source path | `container/bin/gh` (new) | `scripts/`, repo root | Container-only file, copied by the Dockerfile; keeps it apart from Python packages | self |
| D18 | Unreadable YAML (missing or malformed file) | Treat as empty, print `gh: could not read <path>` to stderr for malformed files, continue | exit non-zero | Same reasoning as D13: gh still runs and reports its own auth error; a broken config shouldn't block `gh --version` | self |
| D19 | Argument scanning strategy (revised, C2) | Pattern scan with no flag table; a `repo` positional directly after a bare flag is skipped as a possible flag value; only `repo` subcommands that take a repository use the positional; `repo list` takes a bare owner; `--owner` counts: `-R`/`--repo` anywhere before `--`; for `api`, the first later arg matching `^/?(repos\|orgs\|users)/<owner>`; for `repo <sub>`, the first later non-flag arg shaped `owner/repo` | a table of value-taking flags per command | A flag table duplicates gh's CLI surface and goes stale with gh releases; the pattern scan is wrong only when a flag value is literally `-R`/`--repo` or an `owner/repo`-shaped string before the real one, which is recorded under Risks | self |
| D20 | Wrapper tests on hosts without bash/git/yq | `pytest.mark.skipif` when any is missing | hard fail | `uv run pytest` also runs on the host, where yq isn't a project dependency | self |
| D21 | Dockerfile COPY placement and mode | `COPY container/bin/gh` next to the `entrypoint.sh` COPY, `chmod +x` in that RUN | `COPY --chmod` right after the gh install | Late placement keeps wrapper edits from invalidating the tool layers; plain `chmod` matches the entrypoint pattern and doesn't need BuildKit | self |

## Requirements

- MUST select `work_token` when the target owner is in `work_orgs` and `personal_token` otherwise.
  - AC: with origin `git@github.com:tillo/x.git` and `work_orgs: [tillo]`, fake `gh-base` sees `GH_TOKEN=<work>`; with origin `git@github.com:simon-downes/x.git` it sees `<personal>`.
- MUST pass all arguments, stdin, and the exit code through unchanged.
  - AC: `gh pr create --title "a b" --body ''` reaches `gh-base` as the same argv (including the empty arg); `gh-base` exit 3 → wrapper exit 3; stdin is readable by `gh-base`.
- MUST read tokens and `work_orgs` from `$ARCHIE_HOME_DIR/credentials.yaml` and `config.yaml` on each call, defaulting `ARCHIE_HOME_DIR` to `~/.nexus`.
  - AC: changing `credentials.yaml` between two calls changes the token the second call sees; with `ARCHIE_HOME_DIR` unset and `HOME=<tmp>`, tokens are read from `<tmp>/.nexus/credentials.yaml`.
- MUST resolve the owner from a `gh api` endpoint starting `repos/`, `orgs/` or `users/<owner>` (with or without leading `/`) or the `gh repo <sub> <owner/repo>` positional, then `-R`/`--repo` (`-R o/r`, `-Ro/r`, `--repo o/r`, `--repo=o/r`, `HOST/o/r`, URL), then `GH_REPO`, before the git remote.
  - AC: run in a personal-origin repo, `gh issue list -R tillo/x` gets the work token; each listed form is tested.
- MUST pick the git remote in the order: `gh-resolved=base`, `upstream`, `github`, `origin`, first other; and parse SSH (`git@host:o/r`), `ssh://`, and `https://` URLs.
  - AC: repo with `origin`=personal and `upstream`=tillo → work token.
- MUST match owners case-insensitively and exactly.
  - AC: `Tillo` matches `tillo`; `tillo-labs` does not match `tillo`.
- MUST leave a caller-set `GH_TOKEN` or `GITHUB_TOKEN` untouched and set no token of its own when either is non-empty.
  - AC: `GH_TOKEN=x gh …` → `gh-base` sees `GH_TOKEN=x`; `GITHUB_TOKEN=y gh …` → `gh-base` sees `GITHUB_TOKEN=y` and no `GH_TOKEN`.
- MUST treat a missing or malformed `credentials.yaml`/`config.yaml` as empty and still run `gh-base`.
  - AC: malformed `credentials.yaml` → stderr contains `gh: could not read`, `gh-base` runs without `GH_TOKEN`.
- MUST, when the selected token is empty, run `gh-base` with `GH_TOKEN` unset and print `gh: github.<field> is not set in <path>` to stderr.
  - AC: test asserts the stderr line and that `gh-base` ran without `GH_TOKEN`.
- MUST replace `GithubCredential.token` with `personal_token` and `work_token`, both required by `PUT /auth/credential/github`.
  - AC: PUT with both → 200 configured; PUT `{"token": …}` → 400.
- MUST NOT export any GitHub token at container start.
  - AC: `runtime_environment()` with a stored github credential returns no `GH_TOKEN`.
- MUST accept `auth.providers.github.work_orgs: [str]` in config and reject `work_orgs` under any other provider.
  - AC: `NexusConfig` decode with github `work_orgs` succeeds; with google `work_orgs` raises.
- MUST make `archie auth login github` prompt for (or read JSON stdin with) the two fields, instead of reading `GH_TOKEN`.
  - AC: CLI test with JSON stdin `{"personal_token","work_token"}` PUTs both.
- MUST install gh so `/usr/local/bin/gh` is the wrapper and the real binary is `gh-base`.
  - AC: Dockerfile diverts `/usr/bin/gh` to `/usr/bin/gh-base` and copies `container/bin/gh` to `/usr/local/bin/gh` mode 0755 (inspection; image build not run here).

Out of scope: git credential routing (SSH handles git), GitHub App tokens, migrating
stored `github.token` values, `gh auth` subcommand special-casing.

## Design

### Overview

A bash wrapper resolves the target owner from the command line, `GH_REPO`, or the git
remote; reads `work_orgs` and the matching token with `yq`; and `exec`s `gh-base "$@"`
with `GH_TOKEN` set in that process only. The Python side changes the credential shape,
drops env projection, and types `work_orgs`.

### Code structure

- `container/bin/gh` (new) — the wrapper.
- `tests/test_gh_wrapper.py` (new) — runs the wrapper via `subprocess` with a fake
  `gh-base` on `PATH` that prints `GH_TOKEN` and its argv as JSON.
- `shared/.../credentials/models.py`, `providers.py`, `api.py` — credential and config shape.
- `cli/src/archie_cli/auth.py` — no change expected: removing `env` routes github to the
  existing stdin/prompt path.
- `Dockerfile`, `docs/auth-credentials.md`, `docs/config.md` — install and docs.

### Config / infra

- Config: `auth.providers.github.work_orgs` (list of org/user logins).
- Credentials: `github.personal_token`, `github.work_token`.
- Env vars: `GH_TOKEN` no longer projected. Wrapper reads `ARCHIE_HOME_DIR`, `GH_REPO`.
- Image: rebuild needed (`archie build`). Migrations: none; users rerun `archie auth login github`.

### Non-functional

- Tokens never appear in argv or logs: the wrapper reads them into a shell variable and
  sets them only in the environment of the `exec`'d `gh-base`.

## Milestones

### [x] M1 — Wrapper picks the token from the origin remote (tracer bullet)

- **Approach:** `container/bin/gh`, bash, `set -u`. Resolve home `${ARCHIE_HOME_DIR:-$HOME/.nexus}`.
  Owner from `git remote get-url origin` (M2 widens this). Orgs via
  `yq '.auth.providers.github.work_orgs[]'`; token via `yq '.github.<field> // ""'`.
  Missing files → empty; malformed → empty + warning (D18). `exec gh-base "$@"`.
- **Test seam:** the wrapper executable, run with `subprocess` in a temp git repo; fake
  `gh-base` script records env and argv.
- **Edge cases:** no git repo → personal; missing config file → no work orgs; missing
  token → warning + unset (D13); caller `GH_TOKEN` → untouched (D12).
- **Tasks:** wrapper; tests (skipped without bash/git/yq, D20) for work/personal selection,
  argv/stdin/exit passthrough, per-call reads, default home, missing token, malformed YAML,
  caller `GH_TOKEN`/`GITHUB_TOKEN`.
- **Deliverable:** wrapper selects the right token from `origin`.
- **Verify:** `uv run pytest tests/test_gh_wrapper.py` passes.
- **Outcome:** `container/bin/gh` built; 13 tests in `tests/test_gh_wrapper.py` pass (work/personal from origin, https URL, no repo, argv/stdin/exit passthrough, per-call reads, default home, missing config, missing token, malformed YAML, caller GH_TOKEN/GITHUB_TOKEN, token not in argv); `shellcheck` clean.

### [x] M2 — Explicit repo targets and gh's remote order

- **Approach:** Scan args per D19 without consuming them (`-R<value>` counts only when the value has no whitespace): `-R`/`--repo` forms, `GH_REPO`,
  `api` endpoint, `repo <sub> <o/r>`. Normalise `HOST/o/r` and URLs to the owner. Remote
  order per D10 using `git config --get-regexp '^remote\..*\.gh-resolved$'` and `git remote`.
- **Test seam:** same as M1.
- **Edge cases:** `--` ends flag parsing; `-R` as last arg → ignore; `{owner}` placeholder
  in api path → fall through to remote.
- **Tasks:** owner parsing; remote ordering; URL forms; case-insensitive exact match; tests.
- **Deliverable:** owner resolution matches the requirement list.
- **Verify:** `uv run pytest tests/test_gh_wrapper.py` passes.
- **Outcome:** owner parsing (`owner_from_args`, `owner_from_spec`) and gh's remote order (`base_remote`) added; 37 wrapper tests pass, including 14 explicit-target forms, `GH_REPO`, `--`, trailing `-R`, `{owner}` placeholder, exact match, and the four remote-order cases; `shellcheck` clean. Precedence deviates (→ C1).

### [x] M3 — Credential and config shape

- **Approach:** `GithubCredential(personal_token, work_token)`; `github` provider
  `fields=["personal_token","work_token"]`, no `env`, `set_env=False`.
  `AuthProviderOverride.work_orgs: list[str] | None`; `AuthConfig.__post_init__` rejects
  it for non-github keys. Update existing tests in `test_credentials_runtime.py`,
  `test_cli_auth_login.py`, `test_orchestrator_credentials.py`, `test_credentials_models.py`.
- **Test seam:** `runtime_environment()`, `PUT /auth/credential/github` via the existing
  orchestrator test client, `NexusConfig` decoding, `_set_static_from_login`.
- **Tasks:** model/provider/api changes; tests; docs `auth-credentials.md` + `config.md`.
- **Deliverable:** new credential fields stored, no env projection, `work_orgs` typed.
- **Verify:** `uv run pytest` passes; `uv run ruff check .` clean.
- **Outcome:** `GithubCredential(personal_token, work_token)`; `github` provider has both fields, no env, `set_env=False`; `AuthProviderOverride.work_orgs` added and `AuthConfig.__post_init__` rejects it for other providers. New/updated tests: runtime (no GH_TOKEN), CLI login (JSON stdin, hidden prompts), orchestrator (both required, legacy `token` 400, provider descriptor), config (github accepted, google rejected). Full suite 1304 passed; ruff check clean. Docs: `auth-credentials.md`, `config.md`.

### [x] M4 — Image installs the wrapper

- **Approach:** In the GitHub CLI RUN step, after `apt-get install gh`, run
  `dpkg-divert --local --rename --divert /usr/bin/gh-base --add /usr/bin/gh`. Next to
  the `entrypoint.sh` COPY, `COPY container/bin/gh /usr/local/bin/gh` and add
  `chmod +x /usr/local/bin/gh` to that RUN (D21).
- **Test seam:** none (infrastructure).
- **Tasks:** Dockerfile; docs note in `auth-credentials.md`.
- **Deliverable:** image layout per requirement.
- **Verify:** Dockerfile inspection; `bash -n container/bin/gh` and `shellcheck container/bin/gh` exit 0; `d=$(mktemp -d); ln -s /usr/bin/gh $d/gh-base; PATH=$d:$PATH container/bin/gh --version` prints `gh version …` and exits 0.
- **Outcome:** Dockerfile diverts `/usr/bin/gh` → `/usr/bin/gh-base` (and runs `gh-base --version` in the build step), copies `container/bin/gh` to `/usr/local/bin/gh` beside the entrypoint, `chmod +x` in the same RUN. `bash -n` and `shellcheck` exit 0; with `gh-base` symlinked to `/usr/bin/gh`, `container/bin/gh --version` printed `gh version 2.102.0` and exited 0; with an empty token, it warned and gh reported "not logged into any GitHub hosts". Git mode set to 100755 (the M1 commit had 100644 because the workspace filesystem dropped the exec bit). Image build not run (no Docker here).

## Change log

- C2 (review): owner resolution widened and narrowed after code review — `repo` positionals after a bare flag are skipped (`-b feature/x` had picked owner `feature`), only repository-taking `repo` subcommands use the positional, `repo list <owner>`, `--owner` and full-URL `api` endpoints count, non-GitHub remotes are ignored, any `gh-resolved` value counts, non-string YAML values are treated as missing, and an unset `HOME` no longer aborts — evidence: reviewer reproductions with a fake `gh-base` — no scope change.
- C1 (M2): owner precedence changed so an `api` endpoint or `repo` positional beats `-R`/`GH_REPO`, and `users/<owner>` endpoints were added — a literal endpoint is the repo gh calls, while `GH_REPO` only fills `{owner}` placeholders — no scope change.

## Risks

- The image build is not run in this environment (no Docker); the Dockerfile change is verified by inspection only.
- Owner detection is heuristic for commands that name a repo in other ways (e.g. `gh pr view https://github.com/o/r/pull/1`); those fall back to the cwd remote and may pick the wrong token.
- Not verified: whether `uploads.github.com/user-attachments` accepts a fine-grained token.
- Existing `github.token` entries become invalid until login is rerun (D16).
- A user `config.yaml` that sets `auth.providers.github.env` (e.g. `{token: GH_TOKEN}`) makes `runtime_environment()` raise "maps unknown credential field" and container start fail; the docs migration note tells users to remove it.
- D19 pattern scan misreads a flag value that is literally `-R`/`--repo`/`--owner`. A `gh repo` positional after a boolean flag (`gh repo view --web o/r`) is skipped and the remote's token is used. Documented in `auth-credentials.md` with `-R` as the workaround.
- With no token selected, gh falls back to any login stored in the container's gh config; the warning says so.

## Review

- Plan review: 1 pass. 3 ⚠️ (GITHUB_TOKEN-only case → D12/AC; malformed YAML → D18; arg scanning strategy → D19) and 6 ℹ️ (root `-R` AC → `gh issue list -R`; default-home AC; D6 wording; test skips → D20; COPY placement/BuildKit → D21; M4 Verify made exact; existing `github.env` override → Risks). All accepted; no ❌, so no second pass.
- Code review: 1 round, roles correctness, conventions, risk (risk ran: secrets, shell, public config key). No ❌, so no second round. Accepted and fixed:
  - correctness ⚠️ `repo` flag values (`-b feature/x`, `--template o/t`, deploy-key path) taken as target → skip positional after a bare flag; positional only for repository-taking subcommands (C2).
  - correctness ⚠️ non-GitHub `upstream` chosen as base remote → GitHub-host remotes only (C2).
  - risk ⚠️ `repo list <owner>` / `--owner` ignored → both now select the owner (C2).
  - ℹ️ unset `HOME` aborted under `set -u` → `${HOME:-}`; full-URL `api` endpoints; malformed `config.yaml` test; non-string YAML token → treated as missing; missing-token warning names gh's stored-login fallback.
  - conventions ℹ️ README layout row for `container/`; `test_credentials_models.py` github fields; `config.md` defaults block restored to `providers: {}` and re-wrapped; D17 row order; architecture.md pointer (answers the ❓: yes, a one-line pointer was added).
  - ❓ `gh-resolved` values other than `base`: any `gh-resolved` remote now counts.
  - Rejected: none.
- Merge risk: Medium: a wrong owner guess sends the other account's token, so gh returns 404/permission errors or public-only results for that call; it is limited to `gh` (git uses SSH), each token is fine-grained to one owner, and `-R` overrides the guess.

## Outcome

PR: pending
CI:

## Follow-ups

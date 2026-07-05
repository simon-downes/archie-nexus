# 001 — Skeleton Codebase

## Objective

Create the skeleton codebase for archie-nexus as a uv workspace with two packages:
`archie-cli` (CLI client) and `archie-agent` (web server running inside containers).
Implement enough to build an image, start background container sessions, list them,
and shell into them.

## Context

Step 2 of VISION.md evolution path ("Package split"), with an architectural shift:
containers run in the background as headless agent servers, and clients (CLI, TUI,
web) connect to them. This skeleton establishes that pattern without implementing
the full agent loop — just the container lifecycle and a health endpoint.

The client/server split enables future multi-session management, web UI, remote
hosts, and detach/reattach semantics.

## Requirements

- MUST be structured as a uv workspace with two member packages
  - AC: workspace root pyproject.toml with `[tool.uv.workspace]`; each member has own pyproject.toml
- MUST use Python 3.13, uv, ruff (line-length=100, same rules as nextgen), pytest
  - AC: `.python-version` is `3.13`; `uv sync` creates a single venv with both members
- MUST have two packages: `archie-cli` (in `cli/`) and `archie-agent` (in `agent/`)
  - AC: both importable from the workspace venv
- MUST register entry points: `archie` → CLI, `archie-agent` → agent server
  - AC: `uv run archie --help` and `uv run archie-agent --help` both work
- MUST implement `archie build` → builds Docker image tagged `archie:latest`
  - AC: `uv run archie build` invokes docker build successfully
- MUST provide Dockerfile (based on nextgen, stripped of capability code, installs only agent package)
  - AC: image contains `archie-agent` command but no archie_cli code
- MUST implement `archie start` → starts a background container running the agent server
  - AC: container runs detached with project dir mounted at /workspace
  - AC: prints container name, session ID, and mapped host port
- MUST map a random host port to the agent's container port (8080)
  - AC: `-p 127.0.0.1:0:8080` used; port retrievable via `docker port`
- MUST wait for the agent to be ready before `start` reports success
  - AC: `start` polls until `/status` returns 200 (or container dies / timeout)
  - AC: reported port is live — an immediate `curl /status` succeeds with no wait
  - AC: on crash/timeout, error tells the user to re-run without `--rm` to debug
    (logs are unavailable after `--rm` reaps a crashed container)
- MUST implement `archie ls` → lists running archie sessions with ports
  - AC: shows session ID, status, and mapped host port for each session
- MUST implement `archie shell [session_id]` → interactive bash in a running container
  - AC: `docker exec -it` into the target session with /workspace as workdir
  - AC: prefix matching on session ID
  - AC: no argument or ambiguous match → display list of sessions to choose from
- MUST implement agent server with `GET /status` endpoint returning JSON
  - AC: `curl http://127.0.0.1:{port}/status` returns `{"status": "ok"}`
- MUST generate session IDs: `YYYY-MM-DD-{project}-{hash}` (5-char hex)
  - AC: container named `archie-{session_id}`
- SHOULD use `--rm` so containers are removed when stopped
- SHOULD mount CWD at `/workspace` as working directory

## Design

### Directory structure

```
archie-nexus/
├── pyproject.toml              # workspace root (dev deps, tooling config)
├── .python-version
├── .gitignore
├── Dockerfile                  # at repo root (build context = repo root)
├── entrypoint.sh              # container entrypoint script
├── cli/
│   ├── pyproject.toml          # archie-cli package (click dep)
│   └── src/archie_cli/
│       ├── __init__.py
│       └── cli.py
├── agent/
│   ├── pyproject.toml          # archie-agent package (starlette, uvicorn deps)
│   └── src/archie_agent/
│       ├── __init__.py
│       ├── cli.py              # entry point: starts uvicorn
│       └── app.py              # starlette application
├── tests/
│   └── __init__.py
└── plans/
```

### Workspace root pyproject.toml

Not a package — holds workspace config, dev deps, and tooling settings only.

### Member packages

- `archie-cli`: click for CLI. Entry point `archie`.
- `archie-agent`: starlette + uvicorn for the web server. Entry point `archie-agent`.

Both declare `click>=8,<9` (cli for CLI, agent for its entry point that starts
uvicorn). Agent additionally depends on `starlette` and `uvicorn`.

Build backend: hatchling (not uv_build). Hatchling is the standard for src/ layout
projects, well-supported in uv workspaces, and handles the
`[tool.hatch.build.targets.wheel] packages` config cleanly.

### Agent server

Starlette + uvicorn. Chosen for:
- Minimal, async-native
- WebSocket support built-in (needed for future streaming)
- No heavy framework overhead (no pydantic requirement, no auto-docs)

The `archie-agent` entry point starts uvicorn on 0.0.0.0:8080 inside the container.

### Port mapping

Each container maps a random host port to container port 8080:
```
-p 127.0.0.1:0:8080
```

- `127.0.0.1` — localhost only (no remote access yet)
- `0` — OS picks an available ephemeral port (no conflicts)
- `8080` — fixed container-side port

The assigned host port is queried via:
```bash
docker port archie-{session_id} 8080
# → 127.0.0.1:32771
```

This works identically on Linux and macOS (Colima). No container IP routing needed.

### Container paths

All paths inside the container use `/opt/archie/` (no `-nexus` suffix — the container
doesn't know about the other project variants):

- `/opt/archie/agent/` — agent source (mounted read-only from host)
- `/opt/archie/venv/` — Python venv (deps pre-cached at build, project linked at start)
- `/opt/archie/entrypoint.sh` — startup script
- `/workspace` — project directory (mounted read-write from host)

### Container lifecycle

```bash
# Start: background container with agent server, random host port, agent source mounted
docker run -d --rm \
  --name archie-{session_id} \
  --user {uid} \
  -p 127.0.0.1:0:8080 \
  -v {repo}/agent:/opt/archie/agent:ro \
  -v {cwd}:/workspace:rw \
  -w /workspace \
  archie:latest

# Query assigned port
docker port archie-{session_id} 8080

# List: filter by container name prefix
docker ps --filter name=archie- --format json

# Shell: exec into running container
docker exec -it -w /workspace archie-{session_id} bash
```

### Session ID format

`{YYYY-MM-DD}-{cwd_name}-{secrets.token_hex(3)[:5]}`

Random 5-character hex suffix ensures uniqueness across concurrent sessions.

Container name: `archie-{session_id}`

### Shell session selection

`archie shell [session_id]`:
1. No argument → list all running archie sessions, prompt user to choose
2. Argument matches exactly one container (prefix match) → exec into it
3. Argument matches multiple → show matches, prompt user to choose
4. No matches → error message

### Deviations from VISION.md

- **Package naming:** VISION.md calls the container package "archie-sandbox". Renamed to
  "archie-agent" to reflect the headless server model (no TUI in container). Host package
  is "archie-cli" (not "archie-host") to align with the CLI/web/TUI client split.
- **Background daemon:** VISION.md describes `docker run -it` with interactive TTY. We use
  `docker run -d` with HTTP/WS communication — enables multi-session, detach/reattach, and
  web UI futures.
- **Deferred mounts:** VISION.md lists mounts for brain, sessions, skills, credentials,
  .ssh, .gitconfig. Deferred to later plans. The skeleton only mounts agent source (dev
  iteration) and workspace (project directory).

---

## Milestones

### 1. Workspace scaffolding

**Approach:**
- Workspace root pyproject.toml: no [build-system], just workspace + tooling config
- Each member uses hatchling with src/ layout
- `[tool.hatch.build.targets.wheel] packages = ["src/archie_cli"]` (or archie_agent)
- Tooling config (ruff, pytest) at workspace root only
- `.python-version` at workspace root

**Tasks:**
- Create `pyproject.toml` (workspace root: members list, dev deps, ruff/pytest config)
- Create `.python-version` → `3.13`
- Create `.gitignore` (Python caches, .venv, build artifacts)
- Create `cli/pyproject.toml` (archie-cli, click dep, `archie` entry point, hatchling)
- Create `agent/pyproject.toml` (archie-agent, click + starlette + uvicorn deps, `archie-agent` entry point, hatchling)
- Create `cli/src/archie_cli/__init__.py` (empty)
- Create `agent/src/archie_agent/__init__.py` (empty)
- Create stub `cli/src/archie_cli/cli.py` (click group, no commands)
- Create stub `agent/src/archie_agent/cli.py` (click command that prints "starting server")
- Create `tests/__init__.py`

**Deliverable:** `uv sync` resolves workspace; both entry points respond to --help.

**Verify:** `cd archie-nexus && uv sync && uv run archie --help && uv run archie-agent --help`

---

### 2. Agent server with /status endpoint

**Approach:**
- `agent/src/archie_agent/app.py`: Starlette application with a single route
- `GET /status` → `{"status": "ok"}` (application/json)
- `agent/src/archie_agent/cli.py`: starts uvicorn serving the app on 0.0.0.0:8080
- Use `uvicorn.run()` directly (no need for CLI complexity at this stage)

**Tasks:**
- Create `agent/src/archie_agent/app.py` with Starlette app and /status route
- Update `agent/src/archie_agent/cli.py` to run uvicorn on host 0.0.0.0 port 8080
- Add test: `tests/test_agent_app.py` using Starlette's TestClient for /status

**Deliverable:** `archie-agent` starts a web server; /status returns JSON health response.

**Verify:** `uv run archie-agent &` then `curl http://localhost:8080/status` returns
`{"status": "ok"}`. Kill the background process.

---

### 3. Dockerfile + entrypoint

**Approach:**
- Base: nextgen's sandbox/Dockerfile (system tools: git, ripgrep, fd, curl, jq, yq, aws-cli, tofu, gh, etc.)
- Remove all capability/exec code (runner.py, requirements.txt, /opt/archie/venv, capabilities dir)
- Do NOT bake agent source into the image — it's mounted at runtime for fast iteration
- At build time: copy only `agent/pyproject.toml` and pre-install dependencies (cached layer)
- At runtime: agent source is mounted at `/opt/archie/agent/`, entrypoint.sh does
  a quick `uv sync` (links project source, deps already cached) then `exec archie-agent`
- `EXPOSE 8080` for documentation
- Agent's venv bin dir on PATH

**entrypoint.sh** (lives at repo root, copied into image):
```bash
#!/bin/sh
set -e
export UV_PROJECT_ENVIRONMENT=/opt/archie/venv
cd /opt/archie/agent
uv sync --no-dev --quiet
exec archie-agent
```

`exec` ensures signals (SIGTERM from docker stop) go directly to uvicorn.
`UV_PROJECT_ENVIRONMENT` places the venv outside the read-only agent mount.

**What triggers a rebuild:**
- Dependency changes (new library added/removed in agent/pyproject.toml)
- System tool changes (Dockerfile modifications)

**What does NOT need a rebuild:**
- Any Python code change in agent/ (mounted, picked up on next container start)

**Edge cases:**
- uv needs `.python-version` to know which interpreter to use
- First run after a dep change: `uv sync` installs the new dep (~seconds)

**Tasks:**
- Create `Dockerfile` at repo root, based on nextgen sandbox/Dockerfile
- Strip capability code (runner.py, requirements.txt, venv, capabilities mkdir)
- Add dependency pre-install (cached layer):
  ```dockerfile
  COPY .python-version /opt/archie/
  COPY agent/pyproject.toml /opt/archie/agent/
  ENV UV_PROJECT_ENVIRONMENT=/opt/archie/venv
  RUN cd /opt/archie/agent && uv sync --no-dev --no-install-project
  ENV PATH="/opt/archie/venv/bin:$PATH"
  ```
- Create `entrypoint.sh` at repo root
- Add to Dockerfile:
  ```dockerfile
  COPY entrypoint.sh /opt/archie/entrypoint.sh
  RUN chmod +x /opt/archie/entrypoint.sh
  ```
- Add `EXPOSE 8080`
- Set `CMD ["/opt/archie/entrypoint.sh"]`
- Remove old `CMD ["sleep", "infinity"]`

**Deliverable:** Image builds with deps pre-cached; agent source is mounted at runtime.

**Verify:** `docker build -t archie:latest .` succeeds. Then with a mount:
`docker run --rm -v $(pwd)/agent:/opt/archie/agent:ro archie:latest` starts
the agent server (entrypoint installs from mounted source, uvicorn starts).

---

### 4. CLI — `archie build`

**Approach:**
- Add `build` subcommand to CLI click group
- Dockerfile located via `Path(__file__).resolve().parents[3] / "Dockerfile"`
  - File at `{repo}/cli/src/archie_cli/cli.py` → parents[3] = repo root
- Build args: USERNAME (from $USER), USER_UID (from os.getuid())
- Stream docker output (no stdout/stderr capture)
- Exit with docker's return code

**Tasks:**
- Add `build` command to `cli/src/archie_cli/cli.py`
- Locate Dockerfile, verify exists (ClickException if not)
- Run: `docker build --tag archie:latest --build-arg USERNAME=... --build-arg USER_UID=... -f Dockerfile {repo_root}`
- Print status before build, exit with return code

**Deliverable:** `uv run archie build` builds the image successfully.

**Verify:** `uv run archie build` → image `archie:latest` appears in `docker images`.

---

### 5. CLI — `archie start`

**Approach:**
- Generate session ID: `{YYYY-MM-DD}-{cwd_name}-{secrets.token_hex(3)[:5]}`
- Pre-flight: check docker available, check image exists
- Run container in background with port mapping and agent source mount:
  ```
  docker run -d --rm --name archie-{session_id} \
    --user {uid} \
    -p 127.0.0.1:0:8080 \
    -v {repo}/agent:/opt/archie/agent:ro \
    -v {cwd}:/workspace:rw -w /workspace \
    archie:latest
  ```
- Agent source mounted read-only — container installs from it via entrypoint.sh
- UID from `os.getuid()` — only UID needs to match host for file permissions.
  The Dockerfile creates a group matching the username (no host GID needed).
- **Wait for readiness before reporting success.** `docker run -d` returns when the
  container is *created*, not when the agent is *listening*. The startup sequence
  (`uv sync` + uvicorn bind) takes seconds, and the port mapping is not published
  instantly. A naive `docker port` immediately after `run` often returns empty, and
  even once the port is known, `/status` refuses connections until uvicorn binds.
  Poll until ready (see readiness loop below), then report.
- Print: session ID, container name, agent URL (http://127.0.0.1:{port})

**Readiness check (`wait_for_ready`):**

Single loop with a timeout budget (default 30s, generous for a cold `uv sync`),
re-checking liveness on every iteration so a startup crash is surfaced promptly:

1. **Liveness guard** — `docker inspect -f '{{.State.Running}}' {name}`. If the
   container is no longer running (crashed during startup), it has already been
   removed by `--rm`, so `docker logs` will be empty. Do NOT rely on logs. Instead,
   fail with a message instructing the user to re-run the *same* command with `--rm`
   removed to inspect the crashed container (see debug guidance below).
2. **Port published** — `docker port {name} 8080`. Empty → not ready yet. Once
   non-empty, parse the host port (`127.0.0.1:32771` → `32771`, first line) and
   cache it (it won't change).
3. **Agent listening** — `GET http://127.0.0.1:{port}/status` (use `urllib`, no new
   dependency; 1s per-request timeout). 200 → ready, return the port.
4. Sleep ~0.5s between iterations. On timeout, fail with a clear message.

```python
def wait_for_ready(name: str, timeout: float = 30.0) -> str:
    deadline = time.monotonic() + timeout
    port = None
    while time.monotonic() < deadline:
        if not _container_running(name):           # docker inspect .State.Running
            raise click.ClickException(
                f"Container {name} exited during startup and was removed (--rm).\n"
                f"To debug, re-run the same docker command without --rm:\n"
                f"  {debug_run_cmd}\n"
                f"then inspect with: docker logs {name}"
            )
        if port is None:
            port = _query_port(name)                # docker port {name} 8080
        if port and _status_ok(port):              # GET /status == 200
            return port
        time.sleep(0.5)
    raise click.ClickException(
        f"Container {name} did not become ready within {timeout:.0f}s. "
        f"Re-run without --rm to inspect: docker logs {name}"
    )
```

**Debug guidance (on crash/timeout):** because `--rm` removes crashed containers,
the error message includes the exact `docker run` command with `--rm` stripped so the
user can reproduce the crash and read `docker logs`. Keeping `--rm` is deliberate —
debugging is an explicit, opt-in step, not the default path.

**Edge cases:**
- Docker not available → ClickException with install hints
- Image not built → ClickException suggesting `archie build`
- docker run fails → show stderr, exit non-zero
- Agent source path: located same way as Dockerfile — `Path(__file__).resolve().parents[3] / "agent"`
- Container crashes before/during readiness → liveness guard catches it; error tells
  the user how to re-run without `--rm` for diagnosis (logs unavailable post-reap)
- Container starts but agent never binds within timeout → timeout branch, same
  debug guidance

**Tasks:**
- Add `start` command to `cli/src/archie_cli/cli.py`
- Implement `generate_session_id()` helper
- Implement `check_docker()` and `check_image(tag)` pre-flight helpers
- Locate agent source directory (repo root / agent)
- Construct docker run command with port mapping + both mounts (agent source + workspace)
- Implement `_container_running(name)` (docker inspect `.State.Running`)
- Implement `_query_port(name)` (docker port, parse host port from first line)
- Implement `_status_ok(port)` (urllib GET /status, 1s timeout, catch URLError/OSError)
- Implement `wait_for_ready(name, timeout=30)` combining the three, with liveness
  guard and debug-guidance error messages
- Call `wait_for_ready` after `docker run`; print session ID, container name, agent URL

**Deliverable:** `uv run archie start` launches a background container and reports its port.

**Verify:**
```bash
uv run archie start
# → Session: 2026-07-04-nexus-a3f2b
#   Container: archie-2026-07-04-nexus-a3f2b
#   Agent: http://127.0.0.1:32771

curl http://127.0.0.1:32771/status
# → {"status": "ok"}
```

Because `start` now blocks on `wait_for_ready`, the reported port is guaranteed live —
the follow-up `curl` should succeed immediately with no manual wait.

Crash path: temporarily break `entrypoint.sh` (e.g. bad command), `uv run archie start`
→ fails with the timeout/liveness error and prints the `--rm`-stripped command to re-run.

---

### 6. CLI — `archie ls`

**Approach:**
- List containers matching name prefix `archie-`
- Use `docker ps --filter name=archie- --format '{{json .}}'` for structured output
- For each container, query its port via `docker port {name} 8080`
- Display table: SESSION ID | STATUS | PORT

**Tasks:**
- Add `ls` command to `cli/src/archie_cli/cli.py`
- Run docker ps with filter and JSON format
- For each container, query port assignment
- Format as aligned table
- Handle empty case: "No running sessions."

**Deliverable:** `uv run archie ls` shows all running archie sessions with their ports.

**Verify:** Start two sessions, run `uv run archie ls` → shows both with unique ports.

---

### 7. CLI — `archie shell`

**Approach:**
- Optional argument: session_id (or prefix)
- Resolution logic:
  1. Get list of running archie containers (reuse `list_sessions()` from ls)
  2. No argument: if exactly one session → use it; otherwise show picker
  3. Argument given: filter by prefix match on session ID
     - Exactly one match → use it
     - Multiple matches → show matches as picker
     - No matches → error with suggestion to run `archie ls`
- Picker: numbered list, user enters a number (simple input(), no TUI dependency)
- Once resolved: `docker exec -it -w /workspace {container_name} bash`
- Use `subprocess.run` to inherit terminal (for -it to work)
- Exit with docker's return code

**Edge cases:**
- No running sessions → "No running sessions. Start one with: archie start"
- Session stopped between listing and exec → docker exec fails, show error
- Prefix matches against session ID (container name minus `archie-` prefix)

**Tasks:**
- Add `shell` command to `cli/src/archie_cli/cli.py` with optional session_id argument
- Extract `list_sessions()` helper (shared with `ls` command)
- Implement prefix matching + interactive picker
- Run docker exec -it, exit with return code

**Deliverable:** `uv run archie shell` provides interactive bash in a running session,
with prefix matching and interactive selection when ambiguous.

**Verify:**
- Start two sessions
- `archie shell` → shows picker, select one → drops into bash
- `archie shell 2026-07-04-nexus` → if ambiguous shows matches; if unique goes in
- Inside container: `pwd` → /workspace, project files visible
- `exit` → returns to host

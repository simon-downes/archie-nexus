# 025 — SPEC: Remote client + credential delivery

## Objective

Make the orchestrator reachable from remote clients. The same TUI, pointed at a
non-local orchestrator via profiles, works unchanged. Credentials are delivered from
client to remote orchestrator via an explicit push command. Sessions stay loopback-only
(D8).

## Context

This is M6 of project plan 019 (Orchestrator Control Plane). After M2b, the
orchestrator is the single ingress proxy. Today it binds to `127.0.0.1:7600`. This
slice makes it routable, adds profile-based addressing, credential delivery, and
protocol version awareness.

Locked decisions: D1 (control plane + ingress), D3 (sessions co-located), D8 (single
ingress, sessions loopback-only), D9 (auto-reattach), D10 (client↔orchestrator auth
deferred — trusted network, additive later).

Depends on: 022-spec-attach-exec-proxy (M2b — proxy established).

**No client↔orchestrator auth (per 019 D10).** This is a personal tool on trusted
networks. Auth adds friction without current need. If needed later, it's an additive
change (token header checked by middleware).

## Requirements

- MUST refactor `OrchestratorConfig` to profiles-only (remove top-level `host`/`port`)
  - AC: `OrchestratorConfig` has only `profiles: dict[str, OrchestratorProfile]`
  - AC: `OrchestratorProfile` contains `host: str = "127.0.0.1"` and `port: int = 7600`
  - AC: If no `default` profile in config, hardcoded defaults are used
  - AC: `archie serve` reads from the `default` profile (or hardcoded defaults)
  - AC: M1–M5 code that read `config.orchestrator.host` / `.port` updated

- MUST support profile-based orchestrator addressing via positional args
  - AC: `archie start gpu-box/myproject` → profile `gpu-box`, workspace `myproject`
  - AC: `archie start myproject` → default profile, workspace `myproject`
  - AC: `archie stop gpu-box/session-id` → profile `gpu-box`
  - AC: `archie stop session-id` → default profile
  - AC: `archie attach gpu-box/session-id` → profile `gpu-box`
  - AC: `archie attach session-id` → default profile
  - AC: `archie ls` → list all sessions across all configured profiles
  - AC: `archie ls gpu-box` → list sessions for that profile only
  - AC: `archie shell session-id` → always local exec, default profile used for
    session discovery only (no remote shell)
  - AC: Profile separator is `/` — workspaces and session IDs cannot contain `/`

- MUST support `ARCHIE_HOST` env var as override for orchestrator address
  - AC: `ARCHIE_HOST=192.168.1.50:7600 archie ls` connects to that address
  - AC: Overrides profile resolution for all commands
  - AC: Format: `host:port` or `host` (default port 7600)

- MUST allow the orchestrator to bind to a routable interface
  - AC: Setting `host: "0.0.0.0"` in the profile used by `archie serve` binds to all
    interfaces
  - AC: Remote clients can connect to the orchestrator's routable IP + port
  - AC: Session ports stay loopback-only (`127.0.0.1:0:8080`) — never exposed

- MUST expose `POST /credentials` on the orchestrator for credential delivery
  - AC: Request body is the YAML content of `credentials.yaml`
  - AC: Orchestrator writes to its own `~/.nexus/credentials.yaml` (atomic, 0600)
  - AC: Returns 200 on success
  - AC: Credential file contents are never logged (only "credentials received")

- MUST implement `archie auth push <profile>` to push local credentials to a remote
  orchestrator
  - AC: Reads local `~/.nexus/credentials.yaml`
  - AC: POSTs content to the profile's orchestrator at `POST /credentials`
  - AC: Reports success or failure
  - AC: Fails clearly if profile not found in config
  - AC: Fails clearly if orchestrator unreachable
  - AC: Fails clearly if no local credentials.yaml exists

- MUST make the TUI read `protocol_version` from `SessionInfo` and warn on mismatch
  - AC: If session protocol version > client's known version, display a warning
  - AC: Warning suggests updating the CLI
  - AC: Connection proceeds regardless (no hard fail)
  - AC: Matching or lower version → no warning

- MUST NOT introduce client↔orchestrator authentication
  - AC: No token, mTLS, or auth header required for API calls
  - Why: Personal tool, trusted network. Additive later if needed.

## Technical Design

### Config Schema Refactor

```python
class OrchestratorProfile(msgspec.Struct, forbid_unknown_fields=True):
    """One orchestrator target."""
    host: str = "127.0.0.1"
    port: int = 7600

class OrchestratorConfig(msgspec.Struct, forbid_unknown_fields=True):
    """Orchestrator configuration — profiles only."""
    profiles: dict[str, OrchestratorProfile] = msgspec.field(default_factory=dict)
```

Resolution:
```python
def get_profile(config: OrchestratorConfig, name: str = "default") -> OrchestratorProfile:
    """Get a profile by name, falling back to hardcoded defaults."""
    return config.profiles.get(name, OrchestratorProfile())
```

`archie serve` reads from the `default` profile to determine bind address.

### Profile Resolution in CLI

```python
def resolve_target(arg: str, config: NexusConfig) -> tuple[OrchestratorProfile, str]:
    """Parse 'profile/value' or 'value' from a positional arg.
    
    Returns (profile, remainder) where remainder is workspace or session_id.
    """
    # ARCHIE_HOST env override
    env_host = os.environ.get("ARCHIE_HOST")
    if env_host:
        host, _, port = env_host.partition(":")
        profile = OrchestratorProfile(host=host, port=int(port) if port else 7600)
        return profile, arg  # entire arg is the value
    
    if "/" in arg:
        profile_name, value = arg.split("/", 1)
        profile = config.orchestrator.profiles.get(profile_name)
        if profile is None:
            raise click.ClickException(f"Unknown profile: '{profile_name}'")
        return profile, value
    
    return get_profile(config.orchestrator), arg
```

### CLI Command Changes

**`archie start`:**
```python
@main.command()
@click.argument("workspace")
@click.option("-d", "--detach", is_flag=True)
def start(workspace: str, detach: bool):
    config = load_nexus_config()
    profile, ws_name = resolve_target(workspace, config)
    url = f"http://{profile.host}:{profile.port}"
    response = httpx.post(f"{url}/sessions", json={"workspace": ws_name}, timeout=60)
    ...
```

**`archie ls`:**
```python
@main.command(name="ls")
@click.argument("profile", required=False)
def ls_cmd(profile: str | None):
    config = load_nexus_config()
    if profile:
        # Single profile
        prof = config.orchestrator.profiles.get(profile)
        if prof is None:
            raise click.ClickException(f"Unknown profile: '{profile}'")
        _list_sessions_for(prof, profile)
    else:
        # All profiles (including implicit default)
        profiles = {"default": get_profile(config.orchestrator), **config.orchestrator.profiles}
        for name, prof in profiles.items():
            _list_sessions_for(prof, name)
```

**`archie stop` / `archie attach`:**
```python
@main.command()
@click.argument("session", required=False)
def stop(session: str | None):
    config = load_nexus_config()
    if session and "/" in session:
        profile, session_id = resolve_target(session, config)
    else:
        profile = get_profile(config.orchestrator)
        session_id = session
    url = f"http://{profile.host}:{profile.port}"
    # ... resolve + stop/attach via orchestrator
```

**`archie shell`:**
```python
@main.command()
@click.argument("session_id", required=False)
def shell(session_id: str | None):
    # Always local — uses default profile's orchestrator for session resolution
    # then docker exec directly
    ...
```

### Credential Push Endpoint

```python
Route("/credentials", endpoint=push_credentials, methods=["POST"]),

async def push_credentials(request: Request) -> Response:
    body = await request.body()
    cred_path = home_dir() / "credentials.yaml"
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write with 0600 permissions (reuse save_store pattern)
    import tempfile
    fd = tempfile.NamedTemporaryFile(
        mode="wb", dir=cred_path.parent,
        prefix=".credentials-", suffix=".tmp", delete=False,
    )
    try:
        fd.write(body)
        fd.close()
        os.chmod(fd.name, 0o600)
        os.replace(fd.name, cred_path)
    except Exception:
        os.unlink(fd.name)
        raise
    log.info("Credentials received")
    return JSONResponse({"status": "ok"}, status_code=200)
```

### `archie auth push <profile>`

```python
@auth.command("push")
@click.argument("profile")
def auth_push(profile: str):
    """Push local credentials to a remote orchestrator."""
    config = load_nexus_config()
    prof = config.orchestrator.profiles.get(profile)
    if prof is None:
        raise click.ClickException(f"Unknown profile: '{profile}'")

    cred_path = home_dir() / "credentials.yaml"
    if not cred_path.exists():
        raise click.ClickException("No local credentials.yaml found.")

    url = f"http://{prof.host}:{prof.port}"
    try:
        resp = httpx.post(f"{url}/credentials", content=cred_path.read_bytes(), timeout=10)
        if resp.status_code == 200:
            click.echo(f"✓ Credentials pushed to {profile} ({prof.host}:{prof.port})")
        else:
            raise click.ClickException(f"Push failed: HTTP {resp.status_code}")
    except httpx.ConnectError:
        raise click.ClickException(f"Orchestrator unreachable at {url}")
```

### Protocol Version Warning

```python
from archie_shared.events import PROTOCOL_VERSION  # already defined as 1

# In TUI SessionInfo handler:
if info.protocol_version > PROTOCOL_VERSION:
    self._show_warning(
        f"Session uses protocol v{info.protocol_version}, "
        f"CLI supports v{PROTOCOL_VERSION}. "
        "Some features may not work — consider updating."
    )
```

### Routable Binding

No code change needed — `archie serve` already reads `host` from config (M1). Setting
`host: "0.0.0.0"` in the `default` profile (or whichever profile `serve` reads) makes
it routable. The proxy backend connections always use `127.0.0.1:{port}` for sessions
(hardcoded in proxy.py) — this is correct per D8.

Verify: no response bodies or redirect URLs contain the bind address (they shouldn't —
the API returns session data, not orchestrator addresses).

## Milestones

### 1. Refactor OrchestratorConfig to profiles-only

**Approach:**
- Remove `host` and `port` from `OrchestratorConfig`.
- Add `profiles: dict[str, OrchestratorProfile]` with `OrchestratorProfile` containing
  `host` and `port` with defaults.
- Add `get_profile(config, name="default")` helper.
- Update `archie serve` to read from the default profile.
- Update all M1–M5 code that referenced `config.orchestrator.host` / `.port`:
  - `_orchestrator_url()` in cli.py
  - `archie serve` uvicorn.run call
- ⚠️ Breaking config change — existing `orchestrator.host` / `orchestrator.port` in
  config.yaml will fail validation. Acceptable (personal project).

**Tasks:**
- Replace `OrchestratorConfig` fields with `profiles` dict
- Add `OrchestratorProfile` struct
- Add `get_profile()` helper
- Update `archie serve`: read bind address from default profile
- Update `_orchestrator_url()`: read from default profile
- Update `tests/test_nexus_config.py`
- Test: empty profiles → hardcoded defaults
- Test: custom default profile → used by serve
- Test: named profile loads correctly

**Deliverable:** Config uses profiles; orchestrator starts and CLI connects via
profile resolution.

**Verify:** `pytest tests/test_nexus_config.py -v && pytest tests/ -v` — all pass.

---

### 2. Implement profile-based CLI addressing

**Approach:**
- Add `resolve_target(arg, config)` helper that parses `profile/value`.
- Update `archie start`: positional `workspace` arg, parse profile prefix.
- Update `archie stop` / `archie attach`: parse `profile/session-id`.
- Update `archie ls`: optional `profile` arg; no arg → enumerate all profiles.
- `archie shell`: unchanged (always local, default profile for resolution).
- Support `ARCHIE_HOST` env var (overrides profile for all commands).
- `ls` across all profiles: iterate configured profiles + implicit default, call each
  orchestrator's `GET /sessions`, display grouped by profile.
- ⚠️ Handle unreachable profiles gracefully in `ls` all-profiles mode (show error
  per profile, continue with others).

**Edge cases:**
- Unknown profile → clear error
- `ARCHIE_HOST` set → overrides everything
- `ls` with unreachable profile → "Profile 'X' unreachable: connection refused"
- Workspace or session-id containing `/` → invalid (rejected)

**Tasks:**
- Implement `resolve_target()` helper
- Refactor `archie start`: positional arg, profile parsing
- Refactor `archie stop`: profile/session-id parsing
- Refactor `archie attach`: profile/session-id parsing
- Refactor `archie ls`: optional profile arg, all-profiles enumeration
- Add `ARCHIE_HOST` env var support
- Create `tests/test_cli_profiles.py`
- Test: `gpu-box/myproject` → correct profile + workspace
- Test: `myproject` → default profile
- Test: `ls` no arg → queries all profiles
- Test: `ls gpu-box` → queries one profile
- Test: `ARCHIE_HOST` overrides
- Test: unknown profile → error
- Test: unreachable profile in ls → error shown, others continue

**Deliverable:** All CLI commands support profile-based addressing for remote
orchestrators.

**Verify:** `pytest tests/test_cli_profiles.py -v` — all pass.

---

### 3. Implement credential push endpoint and CLI command

**Approach:**
- Add `POST /credentials` route to orchestrator `app.py`.
- Receives raw body (YAML content), writes atomically to `~/.nexus/credentials.yaml`
  with 0600 permissions (same pattern as `save_store()` in the shared credentials
  module).
- Add `archie auth push <profile>` to the `auth` CLI subgroup.
- Reads local `~/.nexus/credentials.yaml`, POSTs content to the profile's orchestrator.
- Reports success/failure.
- ⚠️ Never log credential file contents — only log "credentials received".
- ⚠️ Atomic write ensures sessions reading the file mid-update see either old or new
  content, never partial.

**Edge cases:**
- No local credentials.yaml → clear error
- Profile not found → clear error
- Orchestrator unreachable → error with address
- Malformed YAML in body → orchestrator writes it anyway (validation is the sender's
  responsibility — the orchestrator is just a conduit)

**Tasks:**
- Add `POST /credentials` route to app.py
- Implement atomic file write (temp + rename + chmod 0600)
- Add `archie auth push <profile>` command
- Create `tests/test_orchestrator_credentials.py`
- Test: POST credentials → file written at `~/.nexus/credentials.yaml`
- Test: atomic write (file exists before, overwritten)
- Test: permissions are 0600
- Create `tests/test_cli_auth_push.py`
- Test: push creds → correct POST body sent
- Test: unknown profile → error
- Test: no local creds → error
- Test: orchestrator unreachable → error

**Deliverable:** Credentials can be pushed from a local machine to a remote
orchestrator's credential store in the existing `credentials.yaml` format.

**Verify:** `pytest tests/test_orchestrator_credentials.py tests/test_cli_auth_push.py -v` — all pass.

---

### 4. Add protocol version warning to TUI

**Approach:**
- Reuse `PROTOCOL_VERSION` from `archie_shared.events` (already defined as 1).
- In TUI's `SessionInfo` event handler, compare session's version against it.
- If session > client: display non-blocking warning via existing TUI notification
  mechanism.
- No hard fail.

**Tasks:**
- Import `PROTOCOL_VERSION` from `archie_shared.events` in TUI
- Add version check in TUI SessionInfo handler
- Display warning via existing TUI status/notification pattern
- Create `tests/test_protocol_version.py`
- Test: matching versions → no warning
- Test: session version > client → warning displayed
- Test: session version ≤ client → no warning

**Deliverable:** TUI warns users about protocol version mismatch without failing.

**Verify:** `pytest tests/test_protocol_version.py -v` — all pass.

---

### 5. Verify routable binding works end-to-end

**Approach:**
- No new code expected — binding to `0.0.0.0` should just work via config.
- Verify that:
  - Proxy backend connections use `127.0.0.1` (not bind address) for sessions
  - No response bodies contain the bind address or localhost assumptions
  - All endpoints respond correctly when accessed from non-localhost
- Write integration tests simulating remote access (TestClient with explicit base_url).
- Document setup: "To expose remotely, add a profile with `host: 0.0.0.0`."

**Tasks:**
- Audit proxy.py: verify backend URL construction always uses `127.0.0.1`
- Audit all endpoint responses: no hardcoded localhost in bodies
- Create `tests/test_orchestrator_remote.py`
- Test: orchestrator bound to 0.0.0.0, client connects (simulated non-localhost)
- Test: proxy still forwards to 127.0.0.1 session port
- Test: `archie ls` from "remote" client shows sessions
- Document remote setup in README or plan

**Deliverable:** Orchestrator works correctly when accessed remotely; proxy always
routes to loopback sessions.

**Verify:**
1. `pytest tests/test_orchestrator_remote.py -v` — all pass
2. Manual e2e: configure `0.0.0.0` on one machine, connect from another (deferred)

---

## Not yet specified

- Client↔orchestrator authentication (token/mTLS) — deferred until multi-user or
  untrusted network
- TLS/HTTPS for orchestrator (plaintext acceptable on trusted LAN)
- Auto-push credentials on start (convenience optimization)
- Web client auth (cookie/query-param variant for browser WS)
- Credential refresh daemon for remote orchestrators
- Multiple `archie serve` instances (one per profile on different ports)

# 026 — SPEC: Orchestrator web console (read-only session list)

## Objective

Add a read-only HTML web console to the orchestrator that displays running sessions.
Lay the foundations for future web portal expansion: Jinja2 templating with base/page
template inheritance, CSS design tokens, and a proper static/template file structure.

## Context

This is M7 of project plan 019 (Orchestrator Control Plane). The orchestrator (from M1)
already serves `GET /sessions` as JSON. This slice adds an HTML route that renders the
same data as a human-readable page. It is a debug/fallback console for when the TUI is
broken, and the starting point for future orchestrator-focused views.

Locked decisions: D1 (control plane only — this is a pure consumer of control-plane
state, never touches sessions or LLM path).

Depends on: 020-spec-serve-and-ls (M1 — orchestrator ASGI app + `GET /sessions`).

**Constraints (load-bearing — keep this slice independent):**
- **Read-only.** No start/stop/attach. No writes. The moment it wants to control
  sessions, it inherits M2a (lifecycle write path).
- **Localhost-only.** Not exposed remotely. The moment it wants remote access, it
  inherits M6 (auth boundary).
- **Not the web client.** The web client (roadmap #4) is a full session-driving
  protocol client equivalent to the TUI. M7 is a host-local console rendering
  orchestrator state.

## Requirements

- MUST serve an HTML page at `GET /` showing running sessions
  - AC: Page displays session ID, workspace (parsed from session ID), status, and port
    for each running session
  - AC: Empty session list shows a "No running sessions" message
  - AC: Page renders correctly in any modern browser

- MUST use Jinja2 templating with base template inheritance
  - AC: `base.html` defines the page skeleton (head, body structure, block definitions)
  - AC: `sessions.html` extends base and fills content blocks
  - AC: Adding a new page requires only a new page template extending base

- MUST include a CSS stylesheet with design token variables
  - AC: CSS defines variables for: primary, secondary, muted colors; text color;
    background; vertical spacing; horizontal spacing; border radius; font family
  - AC: All component styles reference variables (not hardcoded values)
  - AC: Stylesheet is served via Starlette `StaticFiles`

- MUST store templates and static files inside the orchestrator package
  - AC: Templates at `orchestrator/src/archie_orchestrator/templates/`
  - AC: Static files at `orchestrator/src/archie_orchestrator/static/`
  - AC: Files are included in the built package (pyproject.toml package data)

- MUST auto-refresh the page periodically
  - AC: Page includes `<meta http-equiv="refresh" content="5">` (5-second refresh)
  - AC: No JavaScript required

- MUST add `jinja2` as an orchestrator dependency
  - AC: `jinja2` appears in `orchestrator/pyproject.toml` dependencies

- SHOULD display the orchestrator's uptime and version on the page
  - AC: Page header shows how long the orchestrator has been running
  - AC: Page header shows the archie-orchestrator version (from package metadata)

## Technical Design

### File Structure

```
orchestrator/src/archie_orchestrator/
├── app.py              # existing — add routes + static/template mount
├── templates/
│   ├── base.html       # page skeleton, block definitions
│   └── sessions.html   # session list page
├── static/
│   └── style.css       # design token CSS
├── web.py              # route handlers for HTML pages
└── ...
```

### Template: `base.html`

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="5">
    <title>{% block title %}Archie{% endblock %}</title>
    <link rel="stylesheet" href="/static/style.css">
    {% block head %}{% endblock %}
</head>
<body>
    <header>
        <h1>Archie Orchestrator</h1>
        <p class="meta">Up {{ uptime }} · v{{ version }}</p>
    </header>
    <main>
        {% block content %}{% endblock %}
    </main>
</body>
</html>
```

### Template: `sessions.html`

```html
{% extends "base.html" %}
{% block title %}Sessions — Archie{% endblock %}
{% block content %}
{% if sessions %}
<table>
    <thead>
        <tr>
            <th>Session ID</th>
            <th>Workspace</th>
            <th>Status</th>
            <th>Port</th>
        </tr>
    </thead>
    <tbody>
        {% for s in sessions %}
        <tr>
            <td>{{ s.session_id }}</td>
            <td>{{ s.workspace }}</td>
            <td>{{ s.raw_docker_status }}</td>
            <td>{{ s.port or "—" }}</td>
        </tr>
        {% endfor %}
    </tbody>
</table>
{% else %}
<p class="empty">No running sessions.</p>
{% endif %}
{% endblock %}
```

### CSS: `style.css`

```css
:root {
    /* Colors */
    --color-primary: #2563eb;
    --color-secondary: #7c3aed;
    --color-muted: #6b7280;
    --color-text: #1f2937;
    --color-bg: #ffffff;
    --color-surface: #f9fafb;
    --color-border: #e5e7eb;

    /* Spacing */
    --space-xs: 0.25rem;
    --space-sm: 0.5rem;
    --space-md: 1rem;
    --space-lg: 1.5rem;
    --space-xl: 2rem;

    /* Typography */
    --font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    --font-mono: "SF Mono", "Fira Code", monospace;
    --font-size-sm: 0.875rem;
    --font-size-base: 1rem;

    /* Shape */
    --radius: 0.375rem;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
    font-family: var(--font-family);
    font-size: var(--font-size-base);
    color: var(--color-text);
    background: var(--color-bg);
    padding: var(--space-xl);
    max-width: 960px;
    margin: 0 auto;
}

header {
    margin-bottom: var(--space-xl);
}

header h1 {
    font-size: 1.5rem;
    margin-bottom: var(--space-xs);
}

header .meta {
    color: var(--color-muted);
    font-size: var(--font-size-sm);
}

table {
    width: 100%;
    border-collapse: collapse;
}

th, td {
    text-align: left;
    padding: var(--space-sm) var(--space-md);
    border-bottom: 1px solid var(--color-border);
}

th {
    font-size: var(--font-size-sm);
    color: var(--color-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
}

td {
    font-family: var(--font-mono);
    font-size: var(--font-size-sm);
}

.empty {
    color: var(--color-muted);
    padding: var(--space-lg) 0;
}
```

### Route Handler: `web.py`

```python
from datetime import UTC, datetime
from importlib.metadata import version as pkg_version
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.templating import Jinja2Templates

from archie_orchestrator.docker import list_sessions
from archie_shared.session import split_id

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_START_TIME = datetime.now(UTC)


def _uptime() -> str:
    delta = datetime.now(UTC) - _START_TIME
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


async def sessions_page(request: Request) -> HTMLResponse:
    sessions = list_sessions()
    # Map to template-friendly dicts (SessionDescriptor is a msgspec.Struct,
    # can't assign dynamic attributes)
    session_data = []
    for s in sessions:
        workspace, _ = split_id(s.session_id)
        session_data.append({
            "session_id": s.session_id,
            "workspace": workspace,
            "raw_docker_status": s.raw_docker_status,
            "port": s.port,
        })

    return templates.TemplateResponse("sessions.html", {
        "request": request,
        "sessions": session_data,
        "uptime": _uptime(),
        "version": pkg_version("archie-orchestrator"),
    })
```

### App Integration

```python
from starlette.staticfiles import StaticFiles
from starlette.routing import Mount, Route

from archie_orchestrator.web import sessions_page

_STATIC_DIR = Path(__file__).parent / "static"

# Add to app routes:
Route("/", endpoint=sessions_page, methods=["GET"]),
Mount("/static", app=StaticFiles(directory=str(_STATIC_DIR)), name="static"),
```

### Dependencies

Add to `orchestrator/pyproject.toml`:
```toml
dependencies = [
    ...
    "jinja2>=3.1",
]
```

### Package Data

Hatchling's default behaviour for src-layout packages includes all non-Python files
in the package directory. No explicit `force-include` configuration is needed —
`templates/` and `static/` will be included automatically since they're inside
`src/archie_orchestrator/`.

## Milestones

### 1. Set up Jinja2 templates, static files, and CSS foundation

**Approach:**
- Create `templates/` and `static/` directories inside the orchestrator package.
- Create `base.html` with page skeleton, block definitions, meta refresh, stylesheet
  link.
- Create `style.css` with CSS custom properties (design tokens) and base element styles.
- Add `jinja2>=3.1` to orchestrator dependencies.
- Mount `StaticFiles` in the orchestrator app.
- Verify template rendering with a minimal test page.
- ⚠️ Ensure `templates/` and `static/` are included in the package wheel (check
  hatchling config).

**Tasks:**
- Create `orchestrator/src/archie_orchestrator/templates/base.html`
- Create `orchestrator/src/archie_orchestrator/static/style.css` with design tokens
- Add `jinja2>=3.1` to `orchestrator/pyproject.toml`
- Mount `StaticFiles` in `app.py`
- Configure `Jinja2Templates` directory
- Verify package includes non-Python files (hatchling config if needed)
- Create `tests/test_web_console.py`
- Test: `GET /static/style.css` returns 200 with CSS content
- Test: Jinja2Templates renders base.html without error

**Deliverable:** Template and static infrastructure in place; CSS served correctly.

**Verify:** `pytest tests/test_web_console.py -v` — all pass.

---

### 2. Implement the sessions page

**Approach:**
- Create `orchestrator/src/archie_orchestrator/web.py` with `sessions_page` handler.
- Call `list_sessions()` to get current sessions.
- Parse workspace from session_id via `split_id()`.
- Render `sessions.html` template with session data, uptime, version.
- Create `sessions.html` extending `base.html`.
- Add `GET /` route to orchestrator app.
- Handle empty sessions (show "No running sessions" message).
- Uptime: compute from module-level start time.
- Version: read from package metadata (`importlib.metadata.version`).

**Edge cases:**
- No sessions running → "No running sessions" message
- Session with no port (still starting) → display "—"
- `split_id` on malformed session_id → handle gracefully (show raw ID)

**Tasks:**
- Create `orchestrator/src/archie_orchestrator/web.py`
- Implement `sessions_page` handler
- Implement `_uptime()` helper
- Create `orchestrator/src/archie_orchestrator/templates/sessions.html`
- Add `GET /` route to app.py
- Add to `tests/test_web_console.py`
- Test: `GET /` with sessions → HTML contains session IDs and workspace names
- Test: `GET /` with no sessions → HTML contains "No running sessions"
- Test: HTML contains auto-refresh meta tag
- Test: uptime displays correctly
- Test: version displays correctly

**Deliverable:** Browser at `http://localhost:7600/` shows current sessions.

**Verify:** `pytest tests/test_web_console.py -v` — all pass.

---

### Not yet specified

- Additional console pages (metrics view, session detail, logs)
- Dark mode (CSS variable swap)
- WebSocket-based live updates (replace meta refresh)
- Remote access for the console (inherits M6 if exposed)
- Session control actions (stop/restart — inherits M2a, changes "read-only" constraint)

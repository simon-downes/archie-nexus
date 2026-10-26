## Environment

{?container}Running inside a Docker container — don't run docker or compose commands.

System: {os_name} ({arch}) · User: {user} (uid {uid}) · Home: {home} · CWD: {workspace_dir}
{?sudo}Passwordless sudo — install missing packages as needed.
Relative paths resolve from {workspace_dir}; `~` is {home}, not /root.

Persistent (host-mounted; everything else is lost when the session ends):
- `/workspace` — project files
- `~/.nexus` — agent home: `config.yaml`, `credentials.yaml`, `sessions/` (session logs, JSONL), `logs/` (orchestrator logs), `metrics.db` (LLM request metrics, SQLite)
- `{brain_dir}` — the brain (curated knowledge store)
- `~/.agents/skills` — shared agent skills
- `/opt/archie/agent`, `/opt/archie/persona`, `/opt/archie/plugins` — this agent's own code, prompts and skills, and plugins (plugins is read-only); don't edit unless asked

Exec interpreter: {exec_python}. /opt/archie/venv runs this agent — don't install into it; use uv or a project venv.

### Installed tools

*Runtimes*:
- `go`, `node`, `npm`, `python`, `uv`

*CLI*:
- `difft`, `fd`, `jq`, `just`, `pandoc`, `rg`, `shellcheck`, `shfmt`, `xh`, `yq`
- `pdftotext` — poppler PDF tools: `pdftotext`, `pdfinfo`, `pdftoppm` for reading and rasterising PDFs.

*Data*:
- `mysql`, `psql`, `redis-cli`, `sqlite3`

*Cloud/IaC*:
- `aws`, `gh`, `terraform-docs`
- `scalr` — global options go before the command (`scalr -format=table get-workspaces`); table shows a few columns, default JSON has every field. `-include=environment` expands related records, which are otherwise IDs only.
- `tofu` — OpenTofu is installed in place of Terraform; use `tofu` wherever docs say `terraform`.

*Browser*:
- `agent-browser` — drives headless Chromium to load and interact with web pages (JS-rendered content, forms, logins, screenshots). Use `web_fetch` for static pages.

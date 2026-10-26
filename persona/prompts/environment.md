---
# Preinstalled tools shown in the Environment section.
#
# Each key under `tools` is a category heading, shown in this order. Its list holds
# binary names; a tool is listed only when found on PATH. Write `name: note` to add a
# note: only what an agent would otherwise get wrong or miss. Tools are shown sorted
# by name, those without a note sharing one line.
#
# To add a tool: install it in the Dockerfile, then add it here.
#
# Body syntax: {name} is replaced by a value; a line starting with {?name} is kept
# (without the guard) only when name is non-empty. Unknown names are an error.
# Values: os_name, arch, user, uid, home, workspace_dir, exec_python, tools, paths,
# container, sudo.
tools:
  Runtimes: [go, node, npm, python, uv]
  CLI:
    - difft
    - fd
    - jq
    - just
    - pandoc
    - pdftotext: "poppler PDF tools: `pdftotext`, `pdfinfo`, `pdftoppm` for reading and rasterising PDFs."
    - rg
    - shellcheck
    - shfmt
    - xh
    - yq
  Data: [mysql, psql, redis-cli, sqlite3]
  Cloud/IaC:
    - aws
    - gh
    - scalr: >-
        global options go before the command (`scalr -format=table get-workspaces`); table
        shows a few columns, default JSON has every field. `-include=environment` expands
        related records, which are otherwise IDs only.
    - terraform-docs
    - tofu: "OpenTofu is installed in place of Terraform; use `tofu` wherever docs say `terraform`."
  Browser:
    - agent-browser: >-
        drives headless Chromium to load and interact with web pages
        (JS-rendered content, forms, logins, screenshots). Use `web_fetch` for static pages.
---
## Environment

{?container}Running inside a Docker container — don't run docker or compose commands.

System: {os_name} ({arch}) · User: {user} (uid {uid}) · Home: {home} · CWD: {workspace_dir}
{?sudo}Passwordless sudo — install missing packages as needed.
Relative paths resolve from {workspace_dir}; `~` is {home}, not /root.

{?paths}Host-mounted (persistent; everything else is lost when the session ends):
{paths}

Exec interpreter: {exec_python}. /opt/archie/venv runs this agent — don't install into it; use uv or a project venv.

{?tools}### Installed tools
{?tools}
{tools}

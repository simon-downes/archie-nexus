---
# Preinstalled tools shown in the Environment section.
#
# Each key is a binary name; it is listed only when found on PATH. Fields:
#   category  (required) heading the tool is grouped under, in first-appearance order
#             (tools are sorted by name; tools without a note share one line)
#   note      (optional) only what an agent would otherwise get wrong or miss
#
# To add a tool: install it in the Dockerfile, then add an entry here.
#
# Body syntax: {name} is replaced by a value; a line starting with {?name} is kept
# (without the guard) only when name is non-empty. Unknown names are an error.
# Values: os_name, arch, user, uid, home, workspace_dir, exec_python, tools, paths,
# container, sudo.
tools:
  python: {category: Runtimes}
  uv: {category: Runtimes}
  node: {category: Runtimes}
  npm: {category: Runtimes}
  go: {category: Runtimes}
  rg: {category: CLI}
  fd: {category: CLI}
  jq: {category: CLI}
  yq: {category: CLI}
  xh: {category: CLI}
  difft: {category: CLI}
  just: {category: CLI}
  shellcheck: {category: CLI}
  shfmt: {category: CLI}
  pandoc: {category: CLI}
  pdftotext:
    category: CLI
    note: "poppler PDF tools: `pdftotext`, `pdfinfo`, `pdftoppm` for reading and rasterising PDFs."
  sqlite3: {category: Data}
  psql: {category: Data}
  mysql: {category: Data}
  redis-cli: {category: Data}
  aws: {category: Cloud/IaC}
  gh: {category: Cloud/IaC}
  tofu:
    category: Cloud/IaC
    note: "OpenTofu is installed in place of Terraform; use `tofu` wherever docs say `terraform`."
  terraform-docs: {category: Cloud/IaC}
  scalr:
    category: Cloud/IaC
    note: >-
      global options go before the command (`scalr -format=table get-workspaces`); table
      shows a few columns, default JSON has every field. `-include=environment` expands
      related records, which are otherwise IDs only.
  agent-browser:
    category: Browser
    note: >-
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

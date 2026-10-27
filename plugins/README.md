# Plugins

Each `plugins/<id>/` is one integration: `plugin.yaml`, the Python package `archie_plugins.<id>`,
skills in `skills/<name>/SKILL.md`, tests in `tests/`, and a `README.md`.

Each plugin's `README.md` is its maintainer doc: what it does and how it talks to the service,
its namespaces and functions (read vs write), its credential and login, a config snippet with
every setting and policy default, how the code works (modules, safety rules, caching, hooks)
and how to run its tests.

| Plugin | Kind |
| --- | --- |
| [aws](aws/README.md) | Startup hook writing AWS CLI profiles; no tools |
| [cloudflare](cloudflare/README.md) | Read-only API token and account id for the `cf` CLI, plus a skill; no code |
| [datadog](datadog/README.md) | REST tools, `datadog` |
| [github](github/README.md) | Credential for the `gh` wrapper; no code |
| [google](google/README.md) | REST tools, `mail`, `calendar`, `drive`, `meet`, `google` |
| [incident_io](incident_io/README.md) | Read-only REST tools, `incident_io` |
| [jira](jira/README.md) | REST tools, `jira` |
| [notion](notion/README.md) | MCP tools, `notion` |
| [openai](openai/README.md) | API key for the OpenAI model backend; no code |
| [scalr](scalr/README.md) | Credential and settings exported as env vars; no code |
| [slack](slack/README.md) | REST tools, `slack` |

See [docs/plugins.md](../docs/plugins.md) for the manifest reference, the SDK, testing and
the checklist for adding a plugin.

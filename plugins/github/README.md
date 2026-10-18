# GitHub plugin

Gives agent containers authenticated `gh` (GitHub CLI) access with two fine-grained tokens,
one for work organisations and one for everything else. The plugin has no Python code: it
declares the credential and the `work_orgs` setting, and the `gh` wrapper in
`container/bin/gh` picks the token per call.

## Namespaces and functions

No namespaces, no tools and no skill. The agent runs `gh` as a normal shell command.

## Credential

Static, fields `personal_token` and `work_token`. `archie auth login github` reads a JSON
object from stdin (`{"personal_token": "...", "work_token": "..."}`) or, without piped input,
prompts for both with hidden input.

Not projected into the container environment (no `env`, no `set_env`). The wrapper reads the
one token it needs from `credentials.yaml` in the mounted Nexus home on each call.

## Configuration

```yaml
plugins:
  github:
    enabled: true
    settings:
      work_orgs: [tillo]   # owners whose repos use work_token; default none
```

| Key | Type | Default | Read | Meaning |
| --- | --- | --- | --- | --- |
| `settings.work_orgs` | list[str] | none | every `gh` call (by the wrapper) | Owners that use `github.work_token` |

No tool policy (no tools). `work_orgs` is accepted only under `plugins.github.settings`.
`enabled: false` hides login and status for the provider. The wrapper does not check
`enabled`; it reads `config.yaml` and `credentials.yaml` directly.

## How it works

The image diverts the real binary to `/usr/bin/gh-base`. The orchestrator bind-mounts
`container/bin/gh` read-only at `/usr/local/bin/gh` (the Dockerfile also copies it there as a
fallback), so wrapper changes ship without an image rebuild. On each call the wrapper:

1. Leaves everything alone and runs `gh-base` if `GH_TOKEN` or `GITHUB_TOKEN` is already set,
   so a caller-supplied token always wins.
2. Resolves the target owner, first match wins: a `gh api` endpoint (path or full URL)
   starting `repos/`, `orgs/` or `users/<owner>`; the repository argument of
   `gh repo <subcommand>` or the owner of `gh repo list <owner>`; `--owner`; `-R`/`--repo`;
   `GH_REPO`; then the GitHub remote gh treats as the base repository (the one marked by
   `gh repo set-default`, then `upstream`, `github`, `origin`, then the first). Remotes on
   other hosts are ignored.
3. Reads `plugins.github.settings.work_orgs` from `config.yaml` with `yq`. An exact,
   case-insensitive match selects `github.work_token`; anything else, including no owner,
   selects `github.personal_token`.
4. Reads that token from `credentials.yaml` and `exec`s `gh-base` with all arguments, stdin
   and exit code unchanged and `GH_TOKEN` set only in that process.

The Nexus home is `$ARCHIE_HOME_DIR`, default `~/.nexus`. A missing or non-string token, or an
unreadable file, prints a warning to stderr and runs `gh-base` without a token, so gh falls
back to its own stored login. Owner detection works on argument patterns: a `gh repo`
argument right after a bare flag is skipped, and URL-only targets such as
`gh pr view https://github.com/o/r/pull/1` use the current remote. Pass `-R owner/repo` in
those cases.

## Tests

`tests/test_gh_wrapper.py` runs the wrapper as a subprocess with a fake `gh-base` first on
`PATH` that records `GH_TOKEN` and its argv; `tests/test_orchestrator_lifecycle.py` checks the
bind mount. Run `uv run pytest -q tests/test_gh_wrapper.py` (there is no `plugins/github/tests`).

See [docs/plugins.md](../../docs/plugins.md) for the manifest format, the plugin SDK and testing conventions.

# Python CLI tools

Conventions for command-line tools built with Click and Rich. Follow an existing
tool's structure when the project already has one.

## Project layout

Use the `src/` layout with the hatchling build backend:

```
project/
├── src/<package>/
│   ├── __init__.py      # version string
│   ├── cli.py           # Click entry point
│   ├── output.py        # Rich output primitives and the shared Console instances
│   └── ...
├── tests/
├── pyproject.toml
└── README.md
```

- Declare the entry point under `[project.scripts]` in `pyproject.toml`.
- Set `[tool.hatch.build.targets.wheel] packages = ["src/<package>"]`.
- Install the tool globally with `uv tool install`.
- Create the two `Console` instances, stdout and stderr, once at module level in
  `output.py`. Never create a `Console` inside a function.

## Commands

- Use Click for parsing and Rich for formatted output.
- Use `@click.group()` when the tool has more than one command, and register
  subcommand groups with `main.add_command(group)`.
- Put cross-cutting checks, such as install guards or pre-flight checks, in a custom
  `click.Group` subclass instead of repeating them in each command.
- For a command that passes its arguments through to another tool, set
  `context_settings={"ignore_unknown_options": True, "allow_extra_args": True}`.

## Global options

- Support `--plain` on the top-level group. It sets `console.no_color = True` on both
  shared consoles.
- Add `--json` to commands whose output is useful to scripts or agents.

## Configuration

- Store configuration as YAML.
- When the YAML is invalid, print a short message that names the file, not a traceback.
- Create a default config when that saves the user a setup step.
- Fall back to the default for a missing or incomplete entry.

## Terminal output

### Width

A fixed console width, such as 80, prevents layout problems on wide terminals. Choose
it per tool; it doesn't suit every output.

### Colour palette

Use a fixed palette in which each colour means one thing:

| Role | Purpose | Example |
| --- | --- | --- |
| Heading | Section titles | bright_magenta |
| Keyword | Resource names, identifiers | bright_blue |
| Command | Runnable CLI commands | bright_cyan |
| Value | Paths, filenames, data values | #5f87d7 |
| Plain | Default body text | white |
| Muted | Secondary info, types, timestamps | bright_black |
| Chrome | Rules, bullets, separators | dim |
| Status OK | Success icon only | #50c878 |
| Status Warn | Warning icon only | #f0c050 |
| Status Error | Error icon only | bright_red |

- Use status colours only for the icons (✓, !, ✗), never for text or decoration.
- Keep brand colours for banners, not operational output.
- Let message functions accept Rich markup, so callers choose what to highlight.

### Output primitives

Print through a small set of output functions instead of inline Rich markup across the
codebase. `assets/output.py` is a starter implementation to copy and adapt:

| Function | Purpose |
| --- | --- |
| `section(title)` | Rule with a heading-coloured title |
| `status_table(*rows)` | Icon, label and detail in aligned columns |
| `kv_table(*rows)` | Key-value pairs |
| `data_table(*rows, styles)` | Generic aligned table |
| `print_success/error/warning/info(msg)` | Icon and message |
| `bullet_list(items)` | Indented bullets |
| `empty_state(text)` | Muted placeholder for empty results |
| `cmd(text)` | Command-coloured text |
| `human_time(iso)` | Relative time ("in 59m", "2h ago") |

### Timestamps

Show relative times in output for people. Use ISO timestamps when precision matters
and in `--json` output.

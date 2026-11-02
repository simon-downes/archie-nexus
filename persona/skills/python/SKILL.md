---
name: python
description: >
  Python standards: uv and pyproject.toml, ruff, pytest, type hints, Google-style
  docstrings, naming, imports and idioms, plus Click and Rich conventions for CLI tools.
  Use when writing, changing or reviewing .py files or Python projects, scaffolding a
  Python package or CLI tool, or passing coding standards to a Python code reviewer.
  Not for general design, error-handling or security rules (they are in the system
  prompt's coding standards) or for Terraform.
---

# Purpose

The Python defaults Simon wants when a project hasn't already settled them. The
project's own config and conventions win: if `pyproject.toml`, `CONTRIBUTING.md` or
the surrounding code says otherwise, follow the project.

# When to Use

- Writing or changing Python code
- Starting a Python project, package or CLI tool
- Reviewing Python changes, or briefing a reviewer subagent on the standards

# When Not to Use

- Language-neutral rules (design, docstrings, errors, logging, security): the system
  prompt's coding standards already cover them.
- Terraform or OpenTofu code: use the `terraform` skill.

# Toolchain

- Manage dependencies and environments with `uv`. Declare dependencies in
  `pyproject.toml`, and add development-only ones with `uv add --dev`.
- Lint with `ruff check` and format with `ruff format` when the project has no other
  linter or formatter. Configure both in `pyproject.toml`, and run them on the files you
  touched after each change.
- Test with pytest in a `tests/` directory when the project has no other framework.
  Use fixtures for setup and name tests after the behaviour they check.

# Style

- Follow PEP 8 as the formatter enforces it. Use the project's line length; with no
  config, use ruff's default of 88.
- Write Google-style docstrings. Leave types out of them; the type hints carry types.
- Annotate every parameter, return value and variable whose type isn't obvious from
  the assignment. Use modern syntax: `list[str]`, `dict[str, Any]`, `X | None`.

## Naming

| Kind | Style | Examples |
| --- | --- | --- |
| Module | short, lowercase | `users.py`, `db.py` |
| Package | short, lowercase, no underscores | `mypackage`, `core` |
| Class | PascalCase noun | `UserProfile`, `APIClient` |
| Function | snake_case verb | `get_user()`, `connect_to_db()` |
| Variable | snake_case noun | `user_list`, `max_retries` |
| Constant | UPPER_SNAKE_CASE | `MAX_CONNECTIONS`, `DEFAULT_TIMEOUT` |
| Internal member | single leading underscore | `_cache`, `_load()` |

## Imports

- Group imports as standard library, third-party, then local, with a blank line
  between groups. `ruff`'s `I` rules sort them when the project enables them.
- Use absolute imports. Never use wildcard imports.
- Import the module when you use many of its names (`import datetime`); import the
  names when you use a few (`from datetime import datetime`).

## Idioms

- Use comprehensions for simple transforms and filters, and a loop once a
  comprehension needs more than one condition or a nested loop to read.
- Use `enumerate()` instead of manual indexes, `with` for every resource, unpacking
  (`first, *rest = items`), and `dict.get(key, default)` instead of a membership check.
- Catch specific exception types. Never write a bare `except:`.
- Put a bare `*` before optional arguments to make them keyword-only. Use `**kwargs`
  only when the callee really accepts open-ended options, and document what it takes.
- Prefer dataclasses, Pydantic models or NamedTuples for data. Give a hand-written
  class a `__repr__` when it appears in logs or test failures.

# CLI tools

When building a command-line tool, read `references/CLI-TOOLS.md`. It covers the `src/`
layout, Click command groups, Rich output, `--plain` and `--json`, YAML config and a
semantic colour palette. `assets/output.py` is a starter set of Rich output primitives
to copy into the project and adapt.

# Example

**User:** "Add a `--since` filter to the `stats` command."

1. Read `pyproject.toml` and `CONTRIBUTING.md`: ruff at line length 100, pytest, Click.
2. Add the option with a type hint and a docstring update, and parse it with
   `datetime.fromisoformat` inside a `try` that catches `ValueError` only.
3. Add a pytest case for a valid date, an invalid date and the default.
4. Run `uv run ruff check` and `uv run ruff format` on the touched files, then the new
   tests, and report which passed.

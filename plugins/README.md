# Plugins

Each `plugins/<id>/` is one integration, declared by `plugin.yaml` (name, namespaces, entry, credential, policy, settings, context).
Skills live in `skills/<name>/SKILL.md` and are discovered alongside persona skills.
Python code is the package `archie_plugins.<id>` (`plugins/<id>/__init__.py`); tools use `archie_agent.exec.plugin.tool(namespace=...)`.
Tests live in `tests/` (no `__init__.py`; a local `conftest.py` provides fixtures).

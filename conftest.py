"""Repo-root pytest setup: install ``archie_plugins`` and isolate the Nexus home."""

from pathlib import Path

import pytest
from archie_agent.exec.plugin_loader import install_package

REPO_PLUGINS_DIR = Path(__file__).resolve().parent / "plugins"

# Make ``archie_plugins.<id>`` importable for plugin tests (D2, D16).
install_package(REPO_PLUGINS_DIR)


def pytest_collect_directory(path, parent):
    """Collect ``plugins/<id>/`` as a plain directory, not a ``Package``.

    A plugin's ``__init__.py`` makes pytest treat its dir as a package and import
    ``__init__.py`` as top-level ``<id>`` (plugins/ itself has no ``__init__.py``),
    bypassing ``archie_plugins.<id>`` and breaking the SDK's module-name check.
    """
    if path.parent == REPO_PLUGINS_DIR and (path / "__init__.py").is_file():
        return pytest.Dir.from_parent(parent, path=path)
    return None


@pytest.fixture(autouse=True)
def _isolated_nexus_home(tmp_path_factory, monkeypatch):
    """Point ARCHIE_HOME_DIR at a fresh temp dir so tests never read ~/.nexus (D20)."""
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path_factory.mktemp("nexus-home")))

"""Repo-root pytest setup: install ``archie_plugins`` and isolate the Nexus home."""

import os
from pathlib import Path

import pytest
from archie_agent.exec.plugin_loader import install_package

REPO_ROOT = Path(__file__).resolve().parent
REPO_PLUGINS_DIR = REPO_ROOT / "plugins"

# Pin plugin and persona lookups to this checkout. An Archie session container exports
# both variables pointing at the live agent's mounts; without this, runner and server
# subprocesses load plugins from there and clash with the package installed below.
# Set in os.environ (not monkeypatch) so subprocesses spawned by tests inherit it.
os.environ["ARCHIE_PLUGINS_DIR"] = str(REPO_PLUGINS_DIR)
os.environ["ARCHIE_PERSONA_DIR"] = str(REPO_ROOT / "persona")

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

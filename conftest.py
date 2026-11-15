"""Repo-root pytest setup: install ``archie_plugins`` and isolate the Nexus home."""

import os
from pathlib import Path

import pytest
from archie_agent import host_config
from archie_agent.exec.plugin_loader import install_package
from archie_shared.agent_sync import TOKEN_ENV
from archie_shared.config import AGENT_CONTAINER_ENV

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


class FileSource:
    """A ``host_config`` source answered from the test's ``ARCHIE_HOME_DIR`` files.

    It runs the orchestrator's own :func:`~archie_orchestrator.agent_api.answer_sync`, so
    agent tests that write ``config.yaml`` or ``credentials.yaml`` into the temp home keep
    working while versions and replies follow the real rules (plan 090 D10). Requests are
    recorded in :attr:`requests`.
    """

    def __init__(self) -> None:
        self.requests: list = []

    def sync(self, request):
        from archie_orchestrator.agent_api import answer_sync
        from archie_shared.credentials.store import load_store
        from archie_shared.schemas import load_nexus_config

        self.requests.append(request)
        return answer_sync(request, load_config=load_nexus_config, load_store=load_store)


_REAL_SOURCE_FROM_ENV = host_config.source_from_env


@pytest.fixture(autouse=True)
def _host_config_from_files(monkeypatch):
    """Give agent code a file-backed host config and a clean container environment.

    Clears the container flag and the agent token (a session container exports the flag;
    tests that need it set it themselves), patches ``source_from_env`` with
    :class:`FileSource`, and forgets the installed ``HostConfig`` before and after.
    """
    monkeypatch.delenv(AGENT_CONTAINER_ENV, raising=False)
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.setattr(host_config, "source_from_env", FileSource)
    host_config.reset()
    yield
    host_config.reset()


@pytest.fixture
def real_source_from_env():
    """The unpatched ``host_config.source_from_env``."""
    return _REAL_SOURCE_FROM_ENV

"""A missed call site fails loudly instead of reading the host files (plan 090).

Agent containers get config and credentials from the orchestrator. The host files may
still be mounted, so shared readers raise under ``ARCHIE_AGENT_CONTAINER=1``, and a
static scan keeps agent and plugin code off them.
"""

import ast
from pathlib import Path

import pytest
from archie_shared.config import HostFileAccessError
from archie_shared.credentials.runtime import runtime_environment
from archie_shared.credentials.store import get_credential, load_store, set_credential, store_path
from archie_shared.schemas import NexusConfig, load_nexus_config

REPO = Path(__file__).resolve().parents[1]
FORBIDDEN = {"load_nexus_config", "get_credential", "load_store", "store_path"}
HOST_FILES = ("config.yaml", "credentials.yaml")


# ---------------------------------------------------------------------------
# Runtime guard
# ---------------------------------------------------------------------------


@pytest.fixture
def in_container(monkeypatch):
    monkeypatch.setenv("ARCHIE_AGENT_CONTAINER", "1")


def _raises_from_here(reader: str):
    return pytest.raises(
        HostFileAccessError,
        match=rf"{reader}\(\) reads the host's files.*test_host_file_guard\.py:\d+.*archie_agent\.host_config",
    )


def test_the_default_config_read_raises(in_container):
    with _raises_from_here("load_nexus_config"):
        load_nexus_config()


@pytest.mark.parametrize(
    "read",
    [
        store_path,
        load_store,
        lambda: get_credential("github"),
        lambda: set_credential("github", {"personal_token": "x"}),
    ],
    ids=["store_path", "load_store", "get_credential", "set_credential"],
)
def test_credential_store_access_raises(in_container, read):
    with _raises_from_here("store_path"):
        read()


def test_runtime_environment_without_a_lookup_raises(in_container):
    """The transitive default: shared code reading the store on the agent's behalf."""
    with _raises_from_here("store_path"):
        runtime_environment(NexusConfig())
    assert isinstance(runtime_environment(NexusConfig(), lookup=lambda name, kind: None), dict)


def test_an_explicit_config_path_still_loads(in_container, tmp_path):
    path = tmp_path / "candidate.yaml"
    path.write_text("global:\n  model: bedrock-claude-sonnet-4-6\n")
    assert load_nexus_config(path).global_.model == "bedrock-claude-sonnet-4-6"


def test_outside_a_container_the_files_are_read():
    assert load_nexus_config() == NexusConfig()
    assert load_store() == {}


# ---------------------------------------------------------------------------
# Static scan
# ---------------------------------------------------------------------------


def forbidden_references(source: str, filename: str = "<source>") -> list[str]:
    """``file:line name`` for each use of a shared host-file reader in ``source``."""
    tree = ast.parse(source, filename)
    hits: list[str] = []
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("archie_shared"):
            hits += [
                f"{filename}:{node.lineno} {a.name}" for a in node.names if a.name in FORBIDDEN
            ]
            aliases |= {a.asname or a.name for a in node.names if a.name not in FORBIDDEN}
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("archie_shared"):
                    aliases.add(alias.asname or alias.name.split(".")[0])
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN:
            base = node.value
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name) and base.id in aliases:
                hits.append(f"{filename}:{node.lineno} {node.attr}")
    return hits


def _agent_and_plugin_sources() -> list[Path]:
    files = sorted((REPO / "agent" / "src").rglob("*.py"))
    files += sorted(p for p in (REPO / "plugins").rglob("*.py") if "tests" not in p.parts)
    return files


def _scripts() -> list[Path]:
    scripts = sorted((REPO / "container" / "bin").iterdir())
    return scripts + sorted((REPO / "plugins").glob("*/bin/*"))


def script_references(text: str) -> list[str]:
    return [name for name in HOST_FILES if name in text]


def test_agent_and_plugin_code_never_uses_the_host_file_readers():
    files = _agent_and_plugin_sources()
    assert len(files) > 50
    hits = [hit for path in files for hit in forbidden_references(path.read_text(), str(path))]
    assert hits == []


def test_container_scripts_never_name_the_host_files():
    scripts = _scripts()
    assert {p.name for p in scripts} >= {"gh", "credential-process"}
    assert {str(p): script_references(p.read_text()) for p in scripts} == {
        str(p): [] for p in scripts
    }


@pytest.mark.parametrize(
    "source",
    [
        "from archie_shared.credentials import get_credential\n",
        "from archie_shared.credentials.store import (\n    load_store,\n)\n",
        "from archie_shared.schemas import load_nexus_config as load\n",
        "import archie_shared.credentials.store\narchie_shared.credentials.store.store_path()\n",
        "from archie_shared import credentials\ncredentials.get_credential('x')\n",
    ],
)
def test_the_scanner_catches_each_form(source):
    assert forbidden_references(source)


def test_the_scanner_allows_the_agents_own_readers():
    source = (
        "from archie_agent import host_config\n"
        "from archie_agent.host_config import get_credential\n"
        "host_config.get_credential('x')\n"
        "from archie_shared.credentials.models import CREDENTIAL_TYPES\n"
    )
    assert forbidden_references(source) == []


def test_the_script_check_catches_a_file_name():
    assert script_references('yq -r .github "$home/credentials.yaml"') == ["credentials.yaml"]

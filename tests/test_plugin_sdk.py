"""Plugin SDK runtime context tests (plan 070, M6): context, gates, credentials, runner."""

import asyncio
import contextvars
import inspect
import json
import os
import types
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest
from archie_agent.exec import plugin as sdk
from archie_agent.exec.envelope import Envelope
from archie_agent.exec.plugin import (
    CredentialSource,
    PluginContext,
    PluginContextError,
    PluginPolicy,
    PolicyBlock,
    build_context,
    context,
    install_context,
    require_read,
    require_write,
    use_context,
)
from archie_agent.exec.plugin_loader import _reset_for_tests, install_package, load_plugins
from archie_agent.exec.plugin_testing import make_plugin_context
from archie_agent.exec.runner import run
from archie_agent.exec.tools.cache import ToolCache
from archie_shared.plugins import load_manifest, load_manifests

from tests.test_plugin_loader import _restore_repo_plugins, _snapshot_repo_plugins

REPO_PLUGINS_DIR = Path(__file__).resolve().parents[1] / "plugins"


class GateError(Exception):
    pass


READ_ON = PolicyBlock(True)


def _ctx(tmp_path, *, read=READ_ON, write=None, plugin="demo", settings=None):
    return PluginContext(
        plugin=plugin,
        settings=settings or {"a": 1},
        policy=PluginPolicy(read=read, write=write),
        credentials=CredentialSource(plugin, loader=lambda: None),
        cache=ToolCache(tmp_path),
        workspace=tmp_path,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.fixture(autouse=True)
def _no_context():
    install_context(None)
    yield
    install_context(None)


def test_context_without_host_raises():
    with pytest.raises(PluginContextError, match="no plugin context"):
        context()
    assert issubclass(PluginContextError, sdk.ToolError)


def test_use_context_sets_and_restores(tmp_path):
    outer, inner = _ctx(tmp_path, plugin="outer"), _ctx(tmp_path, plugin="inner")
    with use_context(outer):
        assert context() is outer
        with use_context(inner):
            assert context() is inner
        assert context() is outer
    with pytest.raises(PluginContextError):
        context()


def test_context_is_frozen_and_settings_read_only(tmp_path):
    source = {"a": 1, "nested": {"b": 2}}
    ctx = _ctx(tmp_path, settings=source)
    assert isinstance(ctx.settings, types.MappingProxyType)
    with pytest.raises(TypeError):
        ctx.settings["a"] = 2
    with pytest.raises(FrozenInstanceError):
        ctx.plugin = "other"
    source["a"] = 99
    assert ctx.settings["a"] == 1


def _write_credentials(home: Path, data: dict) -> None:
    path = home / "credentials.yaml"
    path.write_text(json.dumps(data))
    path.chmod(0o600)


def test_credential_source_reads_store_every_call(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    source = CredentialSource("datadog")
    assert source.get() is None
    _write_credentials(
        tmp_path,
        {"datadog": {"api_key": "a1", "app_key": "b1"}, "incident_io": {"api_key": "other"}},
    )
    assert (source.get().api_key, source.get().app_key) == ("a1", "b1")
    _write_credentials(tmp_path, {"datadog": {"api_key": "a2", "app_key": "b2"}})
    assert source.get().api_key == "a2"


def test_credential_source_only_reads_its_own_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    _write_credentials(tmp_path, {"incident_io": {"api_key": "secret"}})
    assert CredentialSource("datadog").get() is None
    # No way to ask for another provider: get() takes no arguments.
    assert list(inspect.signature(CredentialSource.get).parameters) == ["self"]
    assert "secret" not in repr(CredentialSource("incident_io"))


def test_credential_source_loader_substitutes_store():
    calls = []
    source = CredentialSource("demo", loader=lambda: calls.append(1) or "cred")
    assert source.get() == "cred" and source.get() == "cred"
    assert calls == [1, 1]


def test_require_read_and_write_messages(tmp_path):
    with use_context(_ctx(tmp_path, read=PolicyBlock(False), write=PolicyBlock(False))):
        with pytest.raises(GateError, match="^reads off$"):
            require_read(GateError, "reads off")
        with pytest.raises(GateError, match="^writes off$"):
            require_write(GateError, "writes off")
    with use_context(_ctx(tmp_path, write=PolicyBlock(True, None))):
        assert require_read(GateError, "x") == PolicyBlock(True)
        assert require_write(GateError, "x") == PolicyBlock(True, None)


def test_require_write_without_write_block_is_disabled(tmp_path):
    with use_context(_ctx(tmp_path, write=None)):
        with pytest.raises(GateError, match="no writes"):
            require_write(GateError, "no writes")


def test_require_gate_without_context_fails_closed():
    with pytest.raises(PluginContextError):
        require_read(GateError, "x")


@pytest.mark.parametrize("scope", [["team:x"], {"pages": ["p"]}, "x"])
def test_unsupported_scope_is_rejected(tmp_path, scope):
    ctx = _ctx(tmp_path, read=PolicyBlock(True, scope), write=PolicyBlock(True, scope))
    with use_context(ctx):
        with pytest.raises(GateError, match="^demo read scope is not supported; remove it$"):
            require_read(GateError, "x")
        with pytest.raises(GateError, match="^custom$"):
            require_write(GateError, "x", scope_message="custom")
        assert require_read(GateError, "x", scope_supported=True).scope == scope


@pytest.mark.parametrize("scope", [None, [], {}])
def test_empty_scope_is_allowed(tmp_path, scope):
    with use_context(_ctx(tmp_path, read=PolicyBlock(True, scope))):
        require_read(GateError, "x")


def _manifest(tmp_path: Path, name: str, body: str):
    plugin = tmp_path / name
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(f"name: {name}\n{body}")
    return load_manifest(plugin)


def test_build_context_uses_manifest_defaults(tmp_path):
    loaded = _manifest(
        tmp_path,
        "demo",
        "entry: archie_plugins.demo\npolicy:\n  read: {enabled: true}\n"
        "  write: {enabled: false}\ncache: {max_bytes: 1048576}\n",
    )
    ctx = build_context(
        "demo",
        manifest=loaded,
        policy_snapshot={},
        settings={"x": 1},
        home=tmp_path / "home",
        workspace=None,
    )
    assert ctx.policy == PluginPolicy(read=PolicyBlock(True), write=PolicyBlock(False))
    assert dict(ctx.settings) == {"x": 1}
    assert ctx.cache.root == tmp_path / "home" / "cache" / "demo"
    assert ctx.cache.max_bytes == 1048576
    assert ctx.credentials.plugin == "demo"
    assert ctx.workspace is None
    assert ctx.clock().tzinfo is not None


def test_build_context_reads_snapshot_and_no_write_block(tmp_path):
    loaded = _manifest(tmp_path, "ro", "entry: archie_plugins.ro\n")
    snapshot = {"ro": {"read": {"enabled": False, "scope": None}, "write": {"enabled": True}}}
    ctx = build_context(
        "ro", manifest=loaded, policy_snapshot=snapshot, settings={}, home=tmp_path, workspace=None
    )
    assert ctx.policy == PluginPolicy(read=PolicyBlock(False), write=None)
    assert ctx.cache.max_bytes == 256 * 1024


def test_build_context_default_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    loaded = _manifest(tmp_path / "p", "demo", "entry: archie_plugins.demo\n")
    ctx = build_context("demo", manifest=loaded, policy_snapshot={}, settings={}, workspace=None)
    assert ctx.cache.root == tmp_path / "cache" / "demo"


def test_tool_cache_max_bytes(tmp_path):
    small = ToolCache(tmp_path, max_bytes=100)
    with pytest.raises(ValueError, match="too large"):
        small.set("demo.item", {"v": "x" * 100}, 60)
    ToolCache(tmp_path, max_bytes=1024).set("demo.item", {"v": "x" * 100}, 60)


def test_make_plugin_context_for_repo_plugin(tmp_path):
    ctx = make_plugin_context(
        "datadog",
        tmp_path=tmp_path,
        policy={"datadog": {"write": {"enabled": True}}},
        credential="cred",
    )
    assert ctx.settings["site"] == "app.datadoghq.eu"
    assert ctx.policy == PluginPolicy(read=PolicyBlock(True), write=PolicyBlock(True))
    assert ctx.credentials.get() == "cred"
    assert ctx.cache.root == tmp_path / "cache" / "datadog"


# --- Runner: per-plugin context per call (D12) -------------------------------

CONTEXT_CODE = """\
import asyncio

from archie_agent.exec.plugin import context, require_write, ToolError, tool


@tool(namespace="{name}")
async def whoami():
    await asyncio.sleep(0.01)
    ctx = context()
    try:
        require_write(ToolError, "no writes")
        writes = True
    except ToolError:
        writes = False
    return {{
        "plugin": ctx.plugin,
        "settings": dict(ctx.settings),
        "writes": writes,
        "cache": str(ctx.cache.root),
        "credential": ctx.credentials.plugin,
        "workspace": str(ctx.workspace),
    }}
"""


@pytest.fixture
def plugins_root(tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    root.mkdir()
    monkeypatch.setenv("ARCHIE_PLUGINS_DIR", str(root))
    snapshot = _snapshot_repo_plugins()
    _reset_for_tests()
    install_package(root)
    yield root
    _restore_repo_plugins(snapshot)


def _write_context_plugin(root: Path, name: str, extra: str = "") -> None:
    plugin = root / name
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text(
        f"name: {name}\nnamespaces: [{name}]\nentry: archie_plugins.{name}\n{extra}"
    )
    (plugin / "__init__.py").write_text(CONTEXT_CODE.format(name=name))


def test_runner_gives_each_plugin_its_own_context(plugins_root, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(home))
    _write_context_plugin(
        plugins_root, "alpha", "settings:\n  label: {type: str, default: a-default}\n"
    )
    _write_context_plugin(
        plugins_root,
        "beta",
        "policy:\n  read: {enabled: true}\n  write: {enabled: false}\n"
        "settings:\n  label: {type: str, default: b-default}\n",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "main.py").write_text(
        "async def main():\n"
        "    a, b = await asyncio.gather(alpha.whoami(), beta.whoami())\n"
        "    return [a, b, await alpha.whoami()]\n"
    )
    (run_dir / "context.json").write_text(
        json.dumps(
            {
                "policy": {"beta": {"read": {"enabled": True}, "write": {"enabled": True}}},
                "settings": {"beta": {"label": "b-configured"}},
                "enabled": ["alpha", "beta"],
            }
        )
    )
    cwd = os.getcwd()
    try:
        contextvars.copy_context().run(run, run_dir)
    finally:
        os.chdir(cwd)
    envelope = Envelope.read(run_dir)
    assert envelope.ok is True, envelope.error
    alpha, beta, alpha_again = envelope.return_value
    assert alpha == alpha_again
    assert alpha["plugin"] == "alpha" and alpha["credential"] == "alpha"
    assert alpha["settings"] == {"label": "a-default"}  # missing -> manifest defaults
    assert alpha["writes"] is False  # no write block
    assert alpha["cache"] == str(home / "cache" / "alpha")
    assert alpha["workspace"] == "/workspace"
    assert beta["plugin"] == "beta" and beta["settings"] == {"label": "b-configured"}
    assert beta["writes"] is True
    assert beta["cache"] == str(home / "cache" / "beta")
    assert [call.fn for call in envelope.calls] == ["alpha.whoami", "beta.whoami", "alpha.whoami"]


def test_plugin_tool_called_outside_host_fails_closed(plugins_root):
    _write_context_plugin(plugins_root, "alpha")
    func = load_plugins(load_manifests(plugins_root), root=plugins_root)["alpha"]["alpha.whoami"]
    with pytest.raises(PluginContextError):
        contextvars.copy_context().run(asyncio.run, func())

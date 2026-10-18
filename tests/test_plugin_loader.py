"""Plugin SDK + loader tests with a fixture plugin under tmp_path."""

import contextvars
import json
import sys
from pathlib import Path

import pytest
from archie_agent.exec import plugin as plugin_sdk
from archie_agent.exec import plugin_loader
from archie_agent.exec.envelope import Envelope
from archie_agent.exec.plugin import plugin_tools, tool
from archie_agent.exec.plugin_loader import (
    PluginLoadError,
    _reset_for_tests,
    install_package,
    load_plugins,
    run_startup_hooks,
)
from archie_agent.exec.runner import _build_namespace, run
from archie_shared.plugins import load_manifests

REPO_PLUGINS_DIR = Path(__file__).resolve().parents[1] / "plugins"

DEMO_CODE = """\
from archie_agent.exec.plugin import tool


@tool(namespace="demo")
async def ping(value="pong"):
    return value
"""


def _snapshot_repo_plugins():
    """Capture the repo's ``archie_plugins`` modules and plugin tools.

    Restoring the same module objects keeps repo plugin tests (which imported them at
    collection time) and monkeypatches via ``sys.modules`` pointing at one copy.
    """
    modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "archie_plugins" or name.startswith("archie_plugins.")
    }
    return modules, dict(plugin_sdk._PLUGIN_TOOLS)


def _restore_repo_plugins(snapshot) -> None:
    modules, tools = snapshot
    _reset_for_tests()
    sys.modules.update(modules)
    plugin_sdk._PLUGIN_TOOLS.update(tools)
    install_package(REPO_PLUGINS_DIR)


@pytest.fixture
def plugins_root(tmp_path, monkeypatch):
    """Swap ``archie_plugins`` to a tmp root; restore the repo root afterwards."""
    root = tmp_path / "plugins"
    root.mkdir()
    monkeypatch.setenv("ARCHIE_PLUGINS_DIR", str(root))
    snapshot = _snapshot_repo_plugins()
    _reset_for_tests()
    install_package(root)
    yield root
    _restore_repo_plugins(snapshot)


def _write_plugin(
    root: Path, name: str = "demo", *, namespaces=("demo",), code: str = DEMO_CODE
) -> Path:
    plugin = root / name
    plugin.mkdir()
    ns = ", ".join(namespaces)
    (plugin / "plugin.yaml").write_text(
        f"name: {name}\nnamespaces: [{ns}]\nentry: archie_plugins.{name}\n"
    )
    (plugin / "__init__.py").write_text(code)
    return plugin


def test_load_returns_demo_ping(plugins_root):
    _write_plugin(plugins_root)
    loaded = load_plugins(load_manifests(plugins_root), root=plugins_root)
    assert list(loaded) == ["demo"]
    assert list(loaded["demo"]) == ["demo.ping"]
    func = loaded["demo"]["demo.ping"]
    assert func._plugin == "demo" and func._namespace == "demo"
    assert sys.modules["archie_plugins.demo"].__name__ == "archie_plugins.demo"


def test_enabled_filter_skips_import(plugins_root):
    _write_plugin(plugins_root)
    loaded = load_plugins(load_manifests(plugins_root), root=plugins_root, enabled=[])
    assert loaded == {}
    assert "archie_plugins.demo" not in sys.modules


def test_install_package_idempotent_and_rejects_other_root(plugins_root, tmp_path):
    install_package(plugins_root)
    with pytest.raises(PluginLoadError) as exc:
        install_package(tmp_path / "elsewhere")
    assert str(plugins_root) in str(exc.value)
    assert "elsewhere" in str(exc.value)


def test_runner_exposes_and_audits_plugin_tool(plugins_root, tmp_path):
    _write_plugin(plugins_root)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "main.py").write_text("async def main():\n    return await demo.ping('hi')\n")
    contextvars.copy_context().run(run, run_dir)
    envelope = Envelope.read(run_dir)
    assert envelope.ok is True, envelope.error
    assert envelope.return_value == "hi"
    assert [call.fn for call in envelope.calls] == ["demo.ping"]
    assert envelope.calls[0].ok is True


def test_build_namespace_has_demo_namespace(plugins_root):
    _write_plugin(plugins_root)
    namespace = contextvars.copy_context().run(_build_namespace, [])
    assert callable(namespace["demo"].ping)
    assert "read" in namespace


def test_undeclared_namespace_rejected(plugins_root):
    code = DEMO_CODE.replace('namespace="demo"', 'namespace="other"')
    _write_plugin(plugins_root, code=code)
    with pytest.raises(PluginLoadError) as exc:
        load_plugins(load_manifests(plugins_root), root=plugins_root)
    assert "'demo'" in str(exc.value) and "'other'" in str(exc.value)


def test_tool_outside_plugin_package_rejected():
    async def stray():
        return None

    with pytest.raises(ValueError, match=__name__):
        tool(namespace="demo")(stray)
    assert "demo.stray" not in plugin_tools()


def test_duplicate_plugin_tool_rejected(plugins_root):
    _write_plugin(plugins_root)
    load_plugins(load_manifests(plugins_root), root=plugins_root)
    func = plugin_tools()["demo.ping"]
    with pytest.raises(ValueError, match="Duplicate"):
        tool(namespace="demo")(func)


def test_entry_import_error_is_envelope_error(plugins_root, tmp_path):
    _write_plugin(plugins_root, code="raise RuntimeError('boom')\n")
    with pytest.raises(PluginLoadError, match="Failed to load plugin 'demo': boom"):
        load_plugins(load_manifests(plugins_root), root=plugins_root)
    _reset_for_tests()
    install_package(plugins_root)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "main.py").write_text("async def main():\n    return 1\n")
    contextvars.copy_context().run(run, run_dir)
    envelope = Envelope.read(run_dir)
    assert envelope.ok is False
    assert envelope.error.type == "RuntimeError"
    assert "Failed to load plugin 'demo'" in envelope.error.message


def test_namespace_colliding_with_core_tool_rejected(plugins_root):
    _write_plugin(plugins_root, namespaces=("read",), code="")
    with pytest.raises(PluginLoadError, match="'read'"):
        load_plugins(load_manifests(plugins_root), root=plugins_root)


def test_namespace_colliding_with_runner_builtin_rejected(plugins_root):
    _write_plugin(plugins_root, namespaces=("json",), code="")
    with pytest.raises(PluginLoadError, match="'json'"):
        load_plugins(load_manifests(plugins_root), root=plugins_root)


def test_runner_rejects_plugin_namespace_shadowing_core_namespace(plugins_root):
    # ``google`` is still a core exec namespace until M12 moves it into a plugin.
    code = DEMO_CODE.replace('namespace="demo"', 'namespace="google"')
    _write_plugin(plugins_root, namespaces=("google",), code=code)
    with pytest.raises(RuntimeError, match="'google'"):
        contextvars.copy_context().run(_build_namespace, [])


def test_tool_from_unknown_plugin_rejected(plugins_root):
    _write_plugin(plugins_root)
    load_plugins(load_manifests(plugins_root), root=plugins_root)
    with pytest.raises(PluginLoadError, match="unknown plugin 'demo'"):
        load_plugins({}, root=plugins_root)


def test_plugins_do_not_leak_into_native_specs_or_exec_docs(plugins_root):
    from archie_agent.exec.native import make_native_specs
    from archie_agent.exec.tool import _build_exec_description

    before = [spec.name for spec in make_native_specs()]
    _write_plugin(plugins_root)
    load_plugins(load_manifests(plugins_root), root=plugins_root)
    assert "demo.ping" in plugin_tools()
    assert [spec.name for spec in make_native_specs()] == before
    assert "demo." not in _build_exec_description()


def test_runner_without_plugins_dir_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHIE_PLUGINS_DIR", str(tmp_path / "missing"))
    snapshot = _snapshot_repo_plugins()
    _reset_for_tests()
    try:
        install_package(tmp_path / "missing")
        (tmp_path / "main.py").write_text("async def main():\n    return 'ok'\n")
        (tmp_path / "context.json").write_text(json.dumps({}))
        contextvars.copy_context().run(run, tmp_path)
        assert Envelope.read(tmp_path).return_value == "ok"
    finally:
        _restore_repo_plugins(snapshot)
    assert plugin_loader.RUNNER_BUILTINS == {"asyncio", "os", "json", "re", "Path"}


HOOK_CODE = """\
from archie_agent.exec.plugin import context

CALLS = []


def start():
    ctx = context()
    CALLS.append((ctx.plugin, dict(ctx.settings), ctx.workspace))


def fail():
    raise RuntimeError("bad profiles")


async def start_async():
    return None
"""


def _write_hook_plugin(root: Path, name: str = "hooky", *, func: str = "start") -> Path:
    plugin = root / name
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text(
        f"name: {name}\n"
        f"hooks: {{startup: 'archie_plugins.{name}.hook:{func}'}}\n"
        "settings:\n  region: {type: str, default: eu-west-1}\n"
    )
    (plugin / "__init__.py").write_text("")
    (plugin / "hook.py").write_text(HOOK_CODE)
    return plugin


def test_startup_hook_runs_with_its_context(plugins_root, tmp_path):
    _write_hook_plugin(plugins_root)
    _write_plugin(plugins_root)  # no hook: not run
    manifests = load_manifests(plugins_root)
    ran = run_startup_hooks(
        manifests,
        enabled=["demo", "hooky"],
        settings={"hooky": {"region": "us-east-1"}},
        root=plugins_root,
        home=tmp_path,
    )
    assert ran == ["hooky"]
    calls = sys.modules["archie_plugins.hooky.hook"].CALLS
    assert calls == [("hooky", {"region": "us-east-1"}, None)]
    with pytest.raises(plugin_sdk.PluginContextError):
        plugin_sdk.context()


def test_startup_hook_defaults_settings_from_manifest(plugins_root, tmp_path):
    _write_hook_plugin(plugins_root)
    run_startup_hooks(
        load_manifests(plugins_root), enabled=["hooky"], settings={}, root=plugins_root
    )
    calls = sys.modules["archie_plugins.hooky.hook"].CALLS
    assert calls == [("hooky", {"region": "eu-west-1"}, None)]


def test_startup_hook_skipped_when_disabled(plugins_root):
    _write_hook_plugin(plugins_root)
    ran = run_startup_hooks(
        load_manifests(plugins_root), enabled=[], settings={}, root=plugins_root
    )
    assert ran == []
    assert "archie_plugins.hooky.hook" not in sys.modules


def test_hook_plugin_without_entry_loads_no_tools(plugins_root):
    _write_hook_plugin(plugins_root)
    assert load_plugins(load_manifests(plugins_root), root=plugins_root) == {"hooky": {}}
    assert "archie_plugins.hooky.hook" not in sys.modules


def test_startup_hook_failure_names_plugin(plugins_root):
    _write_hook_plugin(plugins_root, func="fail")
    with pytest.raises(PluginLoadError) as exc:
        run_startup_hooks(
            load_manifests(plugins_root), enabled=["hooky"], settings={}, root=plugins_root
        )
    assert str(exc.value) == "plugin 'hooky' startup hook failed: bad profiles"
    assert isinstance(exc.value.__cause__, RuntimeError)


def test_startup_hook_missing_function_names_plugin(plugins_root):
    _write_hook_plugin(plugins_root, func="nope")
    with pytest.raises(PluginLoadError, match="plugin 'hooky' startup hook failed"):
        run_startup_hooks(
            load_manifests(plugins_root), enabled=["hooky"], settings={}, root=plugins_root
        )


def test_async_startup_hook_rejected(plugins_root):
    _write_hook_plugin(plugins_root, func="start_async")
    with pytest.raises(PluginLoadError) as exc:
        run_startup_hooks(
            load_manifests(plugins_root), enabled=["hooky"], settings={}, root=plugins_root
        )
    assert "plugin 'hooky' startup hook failed" in str(exc.value)
    assert "synchronous" in str(exc.value)


def test_app_lifespan_exits_when_startup_hook_fails(plugins_root, tmp_path, monkeypatch, caplog):
    """A failing hook stops agent startup before the harness is built, naming the plugin."""
    import asyncio
    import logging
    from unittest.mock import MagicMock, patch

    _write_hook_plugin(plugins_root, func="fail")
    home = tmp_path / "nexus_home"
    home.mkdir()
    (home / "config.yaml").write_text('global:\n  model: "bedrock-claude-sonnet-4-6"\n')
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(home))
    monkeypatch.setenv("ARCHIE_SESSION_ID", "test-session")

    from archie_agent import app as app_module

    async def start():
        async with app_module.lifespan(app_module.app):
            pass

    with (
        patch.object(app_module, "create_llm_client", return_value=MagicMock()),
        patch.object(app_module, "AgentHarness") as harness,
        caplog.at_level(logging.ERROR, logger="archie_agent.app"),
        pytest.raises(SystemExit) as exc,
    ):
        asyncio.run(start())
    assert exc.value.code == 1
    assert "plugin 'hooky' startup hook failed: bad profiles" in caplog.text
    harness.assert_not_called()

"""Tests for plugin manifests (archie_shared.plugins) and plugins_dir()."""

from pathlib import Path

import msgspec
import pytest
from archie_shared.config import plugins_dir
from archie_shared.plugins import (
    FieldSpec,
    PluginManifestError,
    compile_struct,
    load_manifest,
    load_manifests,
    to_builtins,
    validate_value,
)


def _plugin(root: Path, name: str, body: str | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "plugin.yaml").write_text(f"name: {name}\n" if body is None else body)
    return d


def _rejects(d: Path, match: str) -> None:
    with pytest.raises(PluginManifestError, match=match) as exc:
        load_manifest(d)
    assert str(d) in str(exc.value)


# --- plugins_dir ---


def test_plugins_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ARCHIE_PLUGINS_DIR", str(tmp_path / "p"))
    assert plugins_dir() == tmp_path / "p"


def test_plugins_dir_expands_user(monkeypatch):
    monkeypatch.setenv("ARCHIE_PLUGINS_DIR", "~/plug")
    assert plugins_dir() == Path("~/plug").expanduser()


def test_plugins_dir_default(monkeypatch):
    monkeypatch.delenv("ARCHIE_PLUGINS_DIR", raising=False)
    repo = Path(__file__).resolve().parents[1]
    assert plugins_dir() == repo / "plugins"


# --- valid manifests ---


def test_minimal_manifest(tmp_path):
    loaded = load_manifest(_plugin(tmp_path, "demo"))
    m = loaded.manifest
    assert m.name == "demo" and loaded.name == "demo"
    assert m.namespaces == [] and m.entry is None and m.credential is None
    assert m.policy.read.enabled is True and m.policy.write is None
    assert m.cache.max_bytes == 262144
    assert loaded.root == tmp_path / "demo"
    assert loaded.skills_dir == tmp_path / "demo" / "skills"
    assert loaded.scope_type is None
    assert to_builtins(validate_value(loaded.settings_type, {})) == {}


FULL_STATIC = """\
name: datadog
namespaces: [datadog, dd]
entry: archie_plugins.datadog
hooks: {startup: "archie_plugins.datadog.boot:run"}
credential:
  kind: static
  fields: [api_key, app_key]
  env: {api_key: DD_API_KEY}
  set_env: true
policy:
  read: {enabled: true}
  write: {enabled: false}
  scope: {type: "list[str]"}
settings:
  site: {type: str, default: app.datadoghq.eu, env: DD_SITE}
cache: {max_bytes: 1024}
context: [tools/datadog.md]
"""


def test_full_manifest_static(tmp_path):
    loaded = load_manifest(_plugin(tmp_path, "datadog", FULL_STATIC))
    m = loaded.manifest
    assert m.namespaces == ["datadog", "dd"]
    assert m.hooks.startup == "archie_plugins.datadog.boot:run"
    assert m.credential.kind == "static"
    assert m.credential.env == {"api_key": "DD_API_KEY"}
    assert m.credential.set_env is True
    assert m.policy.write.enabled is False
    assert m.settings["site"].env == "DD_SITE"
    assert m.cache.max_bytes == 1024
    assert m.context == ["tools/datadog.md"]
    assert validate_value(loaded.scope_type, ["a"]) == ["a"]
    with pytest.raises(msgspec.ValidationError):
        validate_value(loaded.scope_type, "a")
    assert validate_value(loaded.settings_type, {}).site == "app.datadoghq.eu"


def test_static_aws_login(tmp_path):
    body = "name: aws\ncredential: {kind: static, fields: [profile], login: aws_chain}\n"
    assert load_manifest(_plugin(tmp_path, "aws", body)).manifest.credential.login == "aws_chain"


def test_full_manifest_oauth(tmp_path):
    body = """\
name: notion
namespaces: [notion]
entry: archie_plugins.notion.tools
credential:
  kind: oauth
  server_url: https://mcp.notion.com
  scopes: [read]
  token_path: data.token
  extra_params: {owner: user}
"""
    cred = load_manifest(_plugin(tmp_path, "notion", body)).manifest.credential
    assert cred.kind == "oauth" and cred.server_url == "https://mcp.notion.com"
    assert cred.token_path == "data.token" and cred.refresh_token_path == "refresh_token"
    assert cred.extra_params == {"owner": "user"} and cred.fields == []


# --- rejections ---


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("name: demo\n  bad: [\n", "cannot read"),
        ("- a\n- b\n", "must be a mapping"),
        ("", "must be a mapping"),
        ("name: demo\nbogus: 1\n", "invalid plugin.yaml"),
        ("name: other\n", "must equal directory name"),
        ("name: demo\nnamespaces: [Bad]\n", "invalid namespace"),
        ("name: demo\nnamespaces: [a, a]\n", "duplicate namespaces"),
        ("name: demo\nentry: archie_plugins.demox\n", "entry"),
        ("name: demo\nentry: demo\n", "entry"),
        ("name: demo\nhooks: {startup: archie_plugins.demo.x}\n", "hooks.startup"),
        ("name: demo\nhooks: {startup: 'archie_plugins.other:f'}\n", "hooks.startup"),
        ("name: demo\ncontext: [../x.md]\n", "context entry"),
        ("name: demo\ncontext: [/abs.md]\n", "context entry"),
        ("name: demo\ncache: {max_bytes: 0}\n", "max_bytes"),
        ("name: demo\ncache: {max_bytes: 9000000}\n", "max_bytes"),
        ("name: demo\ncredential: {kind: static}\n", "at least one field"),
        ("name: demo\ncredential: {kind: static, fields: [a], env: {b: B}}\n", "env keys"),
        (
            "name: demo\ncredential: {kind: static, fields: [a], token_endpoint: x}\n",
            "cannot set token_endpoint",
        ),
        ("name: demo\ncredential: {kind: oauth, fields: [a]}\n", "cannot set fields"),
        ("name: demo\ncredential: {kind: static, fields: [a], login: sso}\n", "invalid"),
        ("name: demo\ncredential: {kind: magic}\n", "invalid"),
        ("name: demo\nsettings: {x: {type: complex}}\n", "unknown type"),
        ("name: demo\nsettings: {Bad: {type: str}}\n", "invalid field name"),
        ("name: demo\nsettings: {x: {type: str, env: lower}}\n", "invalid env var"),
        ("name: demo\nsettings: {x: {type: object}}\n", "requires 'fields'"),
        ("name: demo\nsettings: {x: {type: int, default: abc}}\n", "invalid default"),
        ("name: demo\npolicy: {scope: {type: str, env: X}}\n", "only allowed in settings"),
    ],
)
def test_rejections(tmp_path, body, match):
    _rejects(_plugin(tmp_path, "demo", body), match)


def test_invalid_name(tmp_path):
    _rejects(_plugin(tmp_path, "Demo"), "invalid plugin name")


def test_missing_manifest_file(tmp_path):
    d = tmp_path / "demo"
    d.mkdir()
    _rejects(d, "cannot read")


# --- discovery ---


def test_load_manifests_missing_root(tmp_path):
    assert load_manifests(tmp_path / "nope") == {}


def test_load_manifests_discovery(tmp_path):
    _plugin(tmp_path, "zeta")
    _plugin(tmp_path, "alpha", "name: alpha\nnamespaces: [a]\n")
    _plugin(tmp_path, "_skip", "garbage: [")
    _plugin(tmp_path, ".hidden", "garbage: [")
    (tmp_path / "nomanifest").mkdir()
    (tmp_path / "file.txt").write_text("x")
    assert list(load_manifests(tmp_path)) == ["alpha", "zeta"]


def test_load_manifests_default_root(monkeypatch, tmp_path):
    _plugin(tmp_path, "demo")
    monkeypatch.setenv("ARCHIE_PLUGINS_DIR", str(tmp_path))
    assert list(load_manifests()) == ["demo"]


def test_load_manifests_namespace_conflict(tmp_path):
    a = _plugin(tmp_path, "aaa", "name: aaa\nnamespaces: [shared]\n")
    b = _plugin(tmp_path, "bbb", "name: bbb\nnamespaces: [shared]\n")
    with pytest.raises(PluginManifestError, match="shared") as exc:
        load_manifests(tmp_path)
    assert str(a) in str(exc.value) and str(b) in str(exc.value)


def test_load_manifests_propagates_bad_plugin(tmp_path):
    d = _plugin(tmp_path, "bad", "name: wrong\n")
    with pytest.raises(PluginManifestError) as exc:
        load_manifests(tmp_path)
    assert str(d) in str(exc.value)


# --- settings schema ---


def _settings_type():
    fields = msgspec.convert(
        {
            "site": {"type": "str", "default": "eu"},
            "limit": {"type": "int"},
            "ratio": {"type": "float", "default": 0.5},
            "on": {"type": "bool", "default": False},
            "orgs": {"type": "list[str]", "default": ["x"]},
            "tags": {"type": "dict[str,str]"},
            "nested": {
                "type": "object",
                "fields": {"depth": {"type": "int", "default": 2}, "label": {"type": "str"}},
            },
        },
        dict[str, FieldSpec],
    )
    return compile_struct("DemoSettings", fields)


def test_settings_defaults():
    v = validate_value(_settings_type(), {})
    assert to_builtins(v) == {
        "site": "eu",
        "limit": None,
        "ratio": 0.5,
        "on": False,
        "orgs": ["x"],
        "tags": None,
        "nested": None,
    }


def test_settings_values_and_nested():
    t = _settings_type()
    v = validate_value(t, {"limit": 3, "tags": {"a": "b"}, "nested": {"label": "l"}})
    assert v.limit == 3 and v.tags == {"a": "b"}
    assert v.nested.depth == 2 and v.nested.label == "l"
    with pytest.raises(AttributeError):
        v.limit = 4  # frozen


def test_settings_mutable_default_not_shared():
    t = _settings_type()
    a, b = validate_value(t, {}), validate_value(t, {})
    assert a.orgs == b.orgs and a.orgs is not b.orgs


@pytest.mark.parametrize(
    "value",
    [
        {"limit": "three"},
        {"orgs": "x"},
        {"tags": {"a": 1}},
        {"unknown": 1},
        {"nested": {"depth": 1, "extra": 2}},
        {"nested": {"depth": "deep"}},
    ],
)
def test_settings_rejects(value):
    with pytest.raises(msgspec.ValidationError):
        validate_value(_settings_type(), value)


def test_field_spec_rejects_unknown_key():
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"type": "str", "bogus": 1}, FieldSpec)


def test_fields_only_for_object():
    spec = msgspec.convert({"x": {"type": "str", "fields": {}}}, dict[str, FieldSpec])
    with pytest.raises(PluginManifestError, match="only allowed for type 'object'"):
        compile_struct("S", spec)

"""Declarative ``plugins/<id>/plugin.yaml`` manifests: structs, validation, discovery."""

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import msgspec
import yaml

from archie_shared.config import plugins_dir
from archie_shared.plugins.errors import PluginManifestError
from archie_shared.plugins.schema import FieldSpec, compile_struct, compile_type

PLUGIN_PACKAGE = "archie_plugins"
MANIFEST_FILE = "plugin.yaml"
MAX_CACHE_BYTES = 8 * 1024 * 1024

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_HOOK_RE = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")
_OAUTH_FIELDS = (
    "server_url",
    "authorization_endpoint",
    "token_endpoint",
    "registration_endpoint",
    "scopes",
    "extra_params",
)
_OAUTH_PATH_DEFAULTS = {
    "token_path": "access_token",
    "refresh_token_path": "refresh_token",
    "expires_in_path": "expires_in",
}


class CredentialSpec(msgspec.Struct, forbid_unknown_fields=True, frozen=True):
    """Credential provider declaration (provider id == plugin name)."""

    kind: Literal["static", "oauth"]
    # static
    fields: list[str] = []
    env: dict[str, str] = {}
    set_env: bool = False
    login: Literal["aws_chain"] | None = None
    # oauth
    server_url: str | None = None
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    registration_endpoint: str | None = None
    scopes: list[str] | None = None
    token_path: str = "access_token"
    refresh_token_path: str = "refresh_token"
    expires_in_path: str = "expires_in"
    extra_params: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.kind == "static":
            set_oauth = [f for f in _OAUTH_FIELDS if getattr(self, f) is not None]
            set_oauth += [f for f, d in _OAUTH_PATH_DEFAULTS.items() if getattr(self, f) != d]
            if set_oauth:
                raise ValueError(f"static credential cannot set {', '.join(set_oauth)}")
            if not self.fields:
                raise ValueError("static credential needs at least one field")
            unknown = sorted(set(self.env) - set(self.fields))
            if unknown:
                raise ValueError(f"credential env keys not in fields: {', '.join(unknown)}")
        else:
            set_static = [
                name
                for name, bad in (
                    ("fields", bool(self.fields)),
                    ("env", bool(self.env)),
                    ("set_env", self.set_env),
                    ("login", self.login is not None),
                )
                if bad
            ]
            if set_static:
                raise ValueError(f"oauth credential cannot set {', '.join(set_static)}")


class PolicyBlockSpec(msgspec.Struct, forbid_unknown_fields=True, frozen=True):
    enabled: bool


class PolicySpec(msgspec.Struct, forbid_unknown_fields=True, frozen=True):
    """Policy defaults; ``write=None`` means no writes, ``scope=None`` no scopes."""

    read: PolicyBlockSpec = PolicyBlockSpec(enabled=True)
    write: PolicyBlockSpec | None = None
    scope: FieldSpec | None = None


class HooksSpec(msgspec.Struct, forbid_unknown_fields=True, frozen=True):
    startup: str | None = None  # "module:function"


class CacheSpec(msgspec.Struct, forbid_unknown_fields=True, frozen=True):
    max_bytes: int = 262144

    def __post_init__(self) -> None:
        if not 0 < self.max_bytes <= MAX_CACHE_BYTES:
            raise ValueError(f"cache.max_bytes must be in 1..{MAX_CACHE_BYTES}")


class PluginManifest(msgspec.Struct, forbid_unknown_fields=True, frozen=True):
    name: str
    namespaces: list[str] = []
    entry: str | None = None
    hooks: HooksSpec = HooksSpec()
    credential: CredentialSpec | None = None
    # None = no ``policy:`` key; see ``LoadedPlugin.policy`` for the effective value.
    policy: PolicySpec | None = None
    settings: dict[str, FieldSpec] = {}
    cache: CacheSpec = CacheSpec()
    context: list[str] = []


@dataclass(frozen=True)
class LoadedPlugin:
    """A validated manifest plus its directory and compiled config types."""

    manifest: PluginManifest
    root: Path
    settings_type: type
    scope_type: Any | None  # bare scope type; None = scopes unsupported

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def policy(self) -> PolicySpec | None:
        """Effective tool policy defaults; None = the plugin has no tool policy.

        A manifest without a ``policy:`` key gets the read-only default when it
        has an ``entry`` (it registers tools), and no tool policy otherwise.
        """
        if self.manifest.policy is not None:
            return self.manifest.policy
        return PolicySpec() if self.manifest.entry is not None else None

    @property
    def skills_dir(self) -> Path:
        return self.root / "skills"


def _check(manifest: PluginManifest, plugin_dir: Path) -> None:
    def fail(msg: str) -> None:
        raise PluginManifestError(f"{plugin_dir}: {msg}")

    name = manifest.name
    if not _NAME_RE.match(name):
        fail(f"invalid plugin name {name!r}")
    if name != plugin_dir.name:
        fail(f"name {name!r} must equal directory name {plugin_dir.name!r}")
    for ns in manifest.namespaces:
        if not _NAME_RE.match(ns):
            fail(f"invalid namespace {ns!r}")
    if len(set(manifest.namespaces)) != len(manifest.namespaces):
        fail("duplicate namespaces")
    prefix = f"{PLUGIN_PACKAGE}.{name}"

    def in_package(module: str) -> bool:
        return module == prefix or module.startswith(prefix + ".")

    if manifest.entry is not None and not in_package(manifest.entry):
        fail(f"entry {manifest.entry!r} must be {prefix!r} or start with '{prefix}.'")
    hook = manifest.hooks.startup
    if hook is not None and not (_HOOK_RE.match(hook) and in_package(hook.split(":")[0])):
        fail(f"hooks.startup {hook!r} must be 'module:function' with module under {prefix!r}")
    for entry in manifest.context:
        path = PurePosixPath(entry)
        if not entry or path.is_absolute() or ".." in path.parts or "\\" in entry:
            fail(f"context entry {entry!r} must be a relative path without '..'")


def load_manifest(plugin_dir: Path) -> LoadedPlugin:
    """Load and validate ``<plugin_dir>/plugin.yaml``."""
    plugin_dir = Path(plugin_dir)
    path = plugin_dir / MANIFEST_FILE
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise PluginManifestError(f"{plugin_dir}: cannot read {MANIFEST_FILE}: {e}") from None
    if not isinstance(data, dict):
        raise PluginManifestError(f"{plugin_dir}: {MANIFEST_FILE} must be a mapping")
    try:
        manifest = msgspec.convert(data, PluginManifest)
    except msgspec.ValidationError as e:
        raise PluginManifestError(f"{plugin_dir}: invalid {MANIFEST_FILE}: {e}") from None
    _check(manifest, plugin_dir)
    base = "".join(p.capitalize() for p in manifest.name.split("_"))
    try:
        settings_type = compile_struct(f"{base}Settings", manifest.settings)
        scope = None if manifest.policy is None else manifest.policy.scope
        scope_type = None if scope is None else compile_type(scope, f"{base}Scope", allow_env=False)
    except PluginManifestError as e:
        raise PluginManifestError(f"{plugin_dir}: {e}") from None
    return LoadedPlugin(manifest, plugin_dir, settings_type, scope_type)


def load_manifests(root: Path | None = None) -> dict[str, LoadedPlugin]:
    """Discover and load every plugin under ``root`` (default ``plugins_dir()``)."""
    root = plugins_dir() if root is None else Path(root)
    if not root.is_dir():
        return {}
    plugins: dict[str, LoadedPlugin] = {}
    owners: dict[str, Path] = {}
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if child.name.startswith((".", "_")) or not (child / MANIFEST_FILE).is_file():
            continue
        loaded = load_manifest(child)
        for ns in loaded.manifest.namespaces:
            if ns in owners:
                raise PluginManifestError(
                    f"namespace {ns!r} declared by both {owners[ns]} and {child}"
                )
            owners[ns] = child
        plugins[loaded.name] = loaded
    return plugins

"""In-repo plugin manifests (``plugins/<id>/plugin.yaml``), parsed without agent code."""

from archie_shared.plugins.config import (
    PluginAuthConfig,
    PluginConfig,
    PluginPolicyConfig,
    PolicyBlockConfig,
    enabled_plugins,
    is_enabled,
    policy_snapshot,
    resolved_settings,
    validate_plugins,
)
from archie_shared.plugins.errors import PluginManifestError
from archie_shared.plugins.manifest import (
    MANIFEST_FILE,
    PLUGIN_PACKAGE,
    CacheSpec,
    CredentialSpec,
    HooksSpec,
    LoadedPlugin,
    PluginManifest,
    PolicyBlockSpec,
    PolicySpec,
    load_manifest,
    load_manifests,
)
from archie_shared.plugins.schema import (
    FieldSpec,
    compile_struct,
    compile_type,
    to_builtins,
    validate_value,
)

__all__ = [
    "MANIFEST_FILE",
    "PLUGIN_PACKAGE",
    "CacheSpec",
    "CredentialSpec",
    "FieldSpec",
    "HooksSpec",
    "LoadedPlugin",
    "PluginAuthConfig",
    "PluginConfig",
    "PluginManifest",
    "PluginManifestError",
    "PluginPolicyConfig",
    "PolicyBlockConfig",
    "PolicyBlockSpec",
    "PolicySpec",
    "compile_struct",
    "compile_type",
    "enabled_plugins",
    "is_enabled",
    "load_manifest",
    "load_manifests",
    "policy_snapshot",
    "resolved_settings",
    "to_builtins",
    "validate_plugins",
    "validate_value",
]

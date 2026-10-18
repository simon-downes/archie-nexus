"""In-repo plugin manifests (``plugins/<id>/plugin.yaml``), parsed without agent code."""

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
    "PluginManifest",
    "PluginManifestError",
    "PolicyBlockSpec",
    "PolicySpec",
    "compile_struct",
    "compile_type",
    "load_manifest",
    "load_manifests",
    "to_builtins",
    "validate_value",
]

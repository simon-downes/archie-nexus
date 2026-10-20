"""Field-map schema compiler for plugin settings and scopes.

A field map (from ``plugin.yaml``) is compiled to a frozen msgspec Struct so
hosts can validate plugin config without importing plugin code.
"""

import copy
import keyword
import re
from typing import Any

import msgspec

from archie_shared.plugins.errors import PluginManifestError

FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

_SCALAR_TYPES: dict[str, Any] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list[str]": list[str],
    "dict[str,str]": dict[str, str],
}


_ENV_TYPES = frozenset({"str", "int", "float", "bool"})


class FieldSpec(msgspec.Struct, forbid_unknown_fields=True, frozen=True):
    """One field: ``{type, default?, fields? (object only), env? (settings only)}``.

    ``env`` is allowed only on a top-level settings field of a scalar type
    (``str``, ``int``, ``float``, ``bool``), so every projected value is a scalar.
    """

    type: str
    default: Any = msgspec.UNSET
    fields: dict[str, "FieldSpec"] | None = None
    env: str | None = None


def compile_type(
    spec: FieldSpec, name: str = "Field", *, allow_env: bool = True, _in_object: bool = False
) -> Any:
    """Return the Python/msgspec type for one field spec (without ``| None``)."""
    if spec.env is not None:
        if not allow_env:
            raise PluginManifestError(f"{name}: 'env' is only allowed in settings")
        if _in_object:
            raise PluginManifestError(f"{name}: 'env' is not allowed on a field inside an object")
        if spec.type not in _ENV_TYPES:
            raise PluginManifestError(
                f"{name}: 'env' requires a scalar type (str, int, float, bool), not {spec.type!r}"
            )
        if not ENV_NAME_RE.match(spec.env):
            raise PluginManifestError(f"{name}: invalid env var name {spec.env!r}")
    if spec.type == "object":
        if not spec.fields:
            raise PluginManifestError(f"{name}: type 'object' requires 'fields'")
        return compile_struct(name, spec.fields, allow_env=allow_env, _in_object=True)
    if spec.fields is not None:
        raise PluginManifestError(f"{name}: 'fields' is only allowed for type 'object'")
    try:
        return _SCALAR_TYPES[spec.type]
    except KeyError:
        raise PluginManifestError(f"{name}: unknown type {spec.type!r}") from None


def compile_struct(
    name: str, fields: dict[str, FieldSpec], *, allow_env: bool = True, _in_object: bool = False
) -> type[msgspec.Struct]:
    """Compile a field map into a frozen, kw-only msgspec Struct type.

    Every field is optional: absent means the manifest default, else None.
    """
    defs = []
    for fname, spec in fields.items():
        if not FIELD_NAME_RE.match(fname) or keyword.iskeyword(fname):
            raise PluginManifestError(f"{name}: invalid field name {fname!r}")
        sub = f"{name}_{fname}"
        typ = compile_type(spec, sub, allow_env=allow_env, _in_object=_in_object)
        if spec.default is msgspec.UNSET or spec.default is None:
            defs.append((fname, typ | None, None))
            continue
        try:
            value = msgspec.convert(spec.default, typ)
        except msgspec.ValidationError as e:
            raise PluginManifestError(f"{sub}: invalid default: {e}") from None
        if isinstance(value, (list, dict, msgspec.Struct)):
            default = msgspec.field(default_factory=lambda v=value: copy.deepcopy(v))
        else:
            default = value
        defs.append((fname, typ | None, default))
    return msgspec.defstruct(name, defs, forbid_unknown_fields=True, frozen=True, kw_only=True)


def validate_value(struct_type: Any, value: Any) -> Any:
    """Validate ``value`` against a compiled type (raises msgspec.ValidationError)."""
    return msgspec.convert(value, struct_type)


def to_builtins(value: Any) -> Any:
    """Convert a validated value back to plain dicts/lists/scalars."""
    return msgspec.to_builtins(value)

"""Credential store — atomic IO for the typed credential file.

Store path: <ARCHIE_HOME_DIR>/credentials.yaml
Format: YAML dict keyed by service name, each value is a flat field dict.
"""

import fcntl
import os
import stat
import sys
import tempfile
from pathlib import Path

import msgspec
import yaml

from archie_shared.config import home_dir
from archie_shared.credentials.models import CREDENTIAL_TYPES


def store_path() -> Path:
    """Resolve the credential store file path."""
    return home_dir() / "credentials.yaml"


def load_store() -> dict[str, dict]:
    """Load the credential store from disk.

    Returns:
        Dict mapping service names to their field dicts.
        Missing file or empty YAML → empty dict.

    Warns to stderr if file permissions are broader than 0600.
    """
    path = store_path()
    if not path.exists():
        return {}

    # Check permissions
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(
            f"Warning: {path} has permissions {oct(mode)} — expected 0600",
            file=sys.stderr,
        )

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in {path}: {e}") from None

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"Credential store {path} must be a YAML mapping, got {type(raw).__name__}"
        )

    return raw


def save_store(data: dict[str, dict]) -> None:
    """Write the credential store atomically with 0600 permissions.

    Uses temp file + os.replace in the same directory for atomic rename.
    """
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    content = yaml.dump(data, default_flow_style=False, sort_keys=False)

    # Write to temp file in same directory, then atomic rename
    fd = tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        prefix=".credentials-",
        suffix=".tmp",
        delete=False,
    )
    try:
        fd.write(content)
        fd.close()
        os.chmod(fd.name, 0o600)
        os.replace(fd.name, path)
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(fd.name)
        except OSError:
            pass
        raise


def get_credential[T](service: str, credential_type: type[T] | None = None) -> T | None:
    """Load and validate a single service's credential.

    Args:
        service: Service name (e.g. "bedrock", "notion").
        credential_type: Override the struct type. If None, looks up from
            CREDENTIAL_TYPES registry.

    Returns:
        Typed credential struct, or None if service not in store.

    Raises:
        KeyError: If service is not in CREDENTIAL_TYPES and no type override given.
        ValueError: If stored fields fail validation against the struct.
    """
    store = load_store()
    entry = store.get(service)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise ValueError(f"Credential entry for '{service}' must be a mapping")

    if credential_type is None:
        if service not in CREDENTIAL_TYPES:
            raise KeyError(
                f"Unknown service '{service}'. Known: {', '.join(sorted(CREDENTIAL_TYPES.keys()))}"
            )
        credential_type = CREDENTIAL_TYPES[service]

    try:
        return msgspec.convert(entry, credential_type, strict=False)
    except msgspec.ValidationError as e:
        path = store_path()
        raise ValueError(f"Validation error for '{service}' in {path}: {e}") from None


def _update_store(update) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            data = load_store()
            update(data)
            save_store(data)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def replace_credential(service: str, fields: dict[str, str]) -> None:
    """Replace one provider entry while preserving all other entries."""
    def update(data: dict[str, dict]) -> None:
        data[service] = dict(fields)

    _update_store(update)


def delete_credential(service: str) -> None:
    """Delete one provider entry; missing entries are ignored."""
    def update(data: dict[str, dict]) -> None:
        data.pop(service, None)

    _update_store(update)


def set_credential(service: str, fields: dict[str, str | None]) -> None:
    """Set credential fields for a service (read-merge-write).

    Merges the provided fields into the existing entry for the service.
    None values remove the field from the entry.
    Unknown services are tolerated (preserved on write).
    """
    def update(store: dict[str, dict]) -> None:
        if service not in store or not isinstance(store[service], dict):
            store[service] = {}
        for key, value in fields.items():
            if value is None:
                store[service].pop(key, None)
            else:
                store[service][key] = value

    _update_store(update)

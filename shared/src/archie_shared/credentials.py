"""Credential store — read/write ~/.archie/nexus.creds.yaml with 0600 permissions.

Stores service-keyed credentials that are shared between host CLI and container agent.
The file is mounted read-only into containers at /archie/config/nexus.creds.yaml.

Structure:
    bedrock:
        aws_access_key_id: AKIA...
        aws_secret_access_key: ...
        aws_session_token: ...  # optional, present with temporary creds

Security: file is created with 0600 permissions. A warning is emitted if permissions
are too permissive.
"""

import os
import stat
import sys
from pathlib import Path

import yaml

from archie_shared.config import ARCHIE_DIR

CREDENTIALS_PATH = ARCHIE_DIR / "nexus.creds.yaml"

# Inside the container, credentials are mounted here
CONTAINER_CREDENTIALS_PATH = Path("/archie/config/nexus.creds.yaml")

# Service keys — shared constants to prevent typo bugs
SERVICE_BEDROCK = "bedrock"


def get_credentials_path() -> Path:
    """Determine credentials file path (container vs host)."""
    env_path = os.environ.get("ARCHIE_CREDENTIALS")
    if env_path:
        return Path(env_path)
    # Check container path first
    if CONTAINER_CREDENTIALS_PATH.exists():
        return CONTAINER_CREDENTIALS_PATH
    return CREDENTIALS_PATH


def load_credentials() -> dict:
    """Load credentials file. Warn if permissions too permissive."""
    path = get_credentials_path()
    if not path.exists():
        return {}

    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(
            f"Warning: {path} has permissions {oct(mode)} — expected 0600",
            file=sys.stderr,
        )

    try:
        with path.open() as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise yaml.YAMLError(f"invalid YAML in {path}: {e}") from e


def save_credentials(data: dict) -> None:
    """Write credentials file with 0600 permissions."""
    path = CREDENTIALS_PATH  # Always write to host path
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)

    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    os.chmod(str(path), 0o600)


def get_service_credentials(service: str) -> dict | None:
    """Get all credentials for a service. Returns None if not found."""
    creds = load_credentials()
    data = creds.get(service)
    return data if isinstance(data, dict) else None


def set_service_credentials(service: str, fields: dict[str, str]) -> None:
    """Set credentials for a service (merge with existing)."""
    creds = load_credentials()
    if service not in creds:
        creds[service] = {}
    creds[service].update(fields)
    save_credentials(creds)

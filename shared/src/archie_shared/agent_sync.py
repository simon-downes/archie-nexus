"""Wire contract for agent config and credential sync (plan 090).

An agent container doesn't read ``config.yaml`` or ``credentials.yaml``. It sends the
orchestrator the versions it holds and gets back only what changed. A version is a
content hash, so neither side keeps counters, and hand edits on the host are picked up
like any other change. A held version of ``None`` means "not held": the item is always
returned, which is how a cache miss is filled.

The agent authenticates with the per-session token the orchestrator passes in
:data:`TOKEN_ENV`; the orchestrator stores only :func:`token_hash` of it.
"""

from __future__ import annotations

import hashlib
from typing import Any

import msgspec

SYNC_PATH = "/api/agent/sync"
TOKEN_ENV = "ARCHIE_AGENT_TOKEN"
ORCHESTRATOR_URL_ENV = "ARCHIE_ORCHESTRATOR_URL"


class SyncRequest(msgspec.Struct, forbid_unknown_fields=True):
    """The versions an agent holds.

    Attributes:
        config: The held config version, or None when no config is held.
        credentials: Provider name to held version, or None for one not held yet.
    """

    config: str | None = None
    credentials: dict[str, str | None] = {}


class SyncItem(msgspec.Struct):
    """One changed item: its new version and value.

    A config ``value`` is ``msgspec.to_builtins(NexusConfig)``. A credential ``value`` is
    the provider's raw store entry, or None when nothing is stored for it.
    """

    version: str
    value: Any = None


class SyncResponse(msgspec.Struct):
    """What changed since the request's versions.

    Attributes:
        config: The current config when its version differs from the request's.
        config_error: Why the host config couldn't be loaded; ``config`` is then None.
        credentials: The requested credentials whose version differs.
        credential_errors: Requested providers that couldn't be answered, with the reason.
    """

    config: SyncItem | None = None
    config_error: str | None = None
    credentials: dict[str, SyncItem] = {}
    credential_errors: dict[str, str] = {}


def version_of(value: Any) -> str:
    """The version of a JSON-compatible value: 16 hex of SHA-256 over sorted-key JSON."""
    return hashlib.sha256(msgspec.json.encode(value, order="sorted")).hexdigest()[:16]


def token_hash(token: str) -> str:
    """The stored form of an agent token: its SHA-256 hex digest."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

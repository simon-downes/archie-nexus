"""Credential subsystem — typed, multi-service credential store.

Public API (safe for container import — no httpx dependency):
- store: store_path, load_store, save_store, get_credential, set_credential
- models: BedrockCredential, OAuthCredential, CREDENTIAL_TYPES (static plugin
  credential structs are generated from manifests), credential_type
- providers: StaticProvider, OAuthProvider, PROVIDERS registry
- refresh: is_expired, can_refresh_noninteractive, InteractiveReauthRequired

OAuth primitives (credentials.oauth) are NOT re-exported here — they require
httpx which is a CLI-only dependency. Import directly:
    from archie_shared.credentials.oauth import ...
"""

from archie_shared.credentials.api import (
    AuthConfig,
    AuthProviderOverride,
    CredentialStatus,
    OAuthFlowStatus,
    OAuthLoginResponse,
    OAuthProviderResponse,
    StaticProviderResponse,
)
from archie_shared.credentials.models import (
    CREDENTIAL_TYPES,
    BedrockCredential,
    OAuthCredential,
    credential_type,
)
from archie_shared.credentials.providers import (
    PROVIDERS,
    OAuthProvider,
    StaticProvider,
    effective_provider,
)
from archie_shared.credentials.refresh import (
    InteractiveReauthRequired,
    can_refresh_noninteractive,
    extract_nested,
    is_expired,
    refresh_credential,
)
from archie_shared.credentials.runtime import runtime_environment
from archie_shared.credentials.store import (
    delete_credential,
    get_credential,
    load_store,
    replace_credential,
    save_store,
    set_credential,
    store_path,
)

__all__ = [
    "AuthConfig",
    "AuthProviderOverride",
    "CredentialStatus",
    "OAuthFlowStatus",
    "OAuthLoginResponse",
    "OAuthProviderResponse",
    "StaticProviderResponse",
    "BedrockCredential",
    "CREDENTIAL_TYPES",
    "InteractiveReauthRequired",
    "OAuthCredential",
    "OAuthProvider",
    "PROVIDERS",
    "StaticProvider",
    "can_refresh_noninteractive",
    "credential_type",
    "delete_credential",
    "effective_provider",
    "extract_nested",
    "get_credential",
    "is_expired",
    "load_store",
    "refresh_credential",
    "replace_credential",
    "runtime_environment",
    "save_store",
    "set_credential",
    "store_path",
]

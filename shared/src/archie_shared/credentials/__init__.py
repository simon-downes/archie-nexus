"""Credential subsystem — typed, multi-service credential store.

Public API (safe for container import — no httpx dependency):
- store: store_path, load_store, save_store, get_credential, set_credential
- models: per-service credential structs, CREDENTIAL_TYPES
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
    AwsCredential,
    BedrockCredential,
    GithubCredential,
    JiraCredential,
    LinearCredential,
    OAuthCredential,
    ScalrCredential,
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
    "AwsCredential",
    "BedrockCredential",
    "CREDENTIAL_TYPES",
    "GithubCredential",
    "InteractiveReauthRequired",
    "JiraCredential",
    "LinearCredential",
    "OAuthCredential",
    "OAuthProvider",
    "PROVIDERS",
    "ScalrCredential",
    "StaticProvider",
    "can_refresh_noninteractive",
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

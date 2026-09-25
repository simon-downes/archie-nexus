"""Orchestrator-owned provider and credential authentication."""

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import msgspec
from archie_shared.credentials.api import (
    AuthProviderOverride,
    CredentialStatus,
    OAuthFlowStatus,
    OAuthProviderResponse,
    StaticProviderResponse,
)
from archie_shared.credentials.models import CREDENTIAL_TYPES
from archie_shared.credentials.oauth import build_auth_url, generate_pkce
from archie_shared.credentials.providers import (
    PROVIDERS,
    OAuthProvider,
    StaticProvider,
    effective_provider,
)
from archie_shared.credentials.store import (
    delete_credential,
    get_credential,
    replace_credential,
)
from archie_shared.schemas import NexusConfig


class AuthError(Exception):
    status_code = 400


class UnknownProviderError(AuthError):
    status_code = 404


class CredentialValidationError(AuthError):
    status_code = 400


class ProviderUnavailableError(AuthError):
    status_code = 502


def _oauth_error(response: httpx.Response, operation: str) -> str:
    """Build a safe diagnostic from a provider OAuth error response."""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        error = payload.get("error")
        description = payload.get("error_description")
        if isinstance(error, str) and error.strip():
            detail = error.strip()
            if isinstance(description, str) and description.strip():
                detail += f": {description.strip()}"
            return f"OAuth {operation} rejected by provider ({detail})"
    return f"OAuth {operation} rejected by provider (HTTP {response.status_code})"


@dataclass
class AuthFlow:
    flow_id: str
    provider: str
    state: str
    verifier: str
    redirect_uri: str
    client_id: str
    token_endpoint: str
    client_secret: str | None
    created_at: datetime
    expires_at: datetime
    status: str = "pending"
    error: str | None = None
    credential_status: CredentialStatus | None = None


@dataclass
class AuthFlowStore:
    flows: dict[str, AuthFlow] = field(default_factory=dict)
    login_timeout: timedelta = timedelta(seconds=120)
    retention: timedelta = timedelta(minutes=5)

    def purge(self) -> None:
        now = datetime.now(UTC)
        self.flows = {key: value for key, value in self.flows.items() if value.expires_at > now}

    def add(
        self,
        provider: str,
        verifier: str,
        redirect_uri: str,
        client_id: str,
        token_endpoint: str,
        client_secret: str | None,
    ) -> AuthFlow:
        now = datetime.now(UTC)
        flow = AuthFlow(
            secrets.token_urlsafe(18),
            provider,
            secrets.token_urlsafe(32),
            verifier,
            redirect_uri,
            client_id,
            token_endpoint,
            client_secret,
            now,
            now + self.login_timeout,
        )
        self.flows[flow.flow_id] = flow
        return flow


@dataclass
class AuthService:
    config: NexusConfig
    flows: AuthFlowStore = field(default_factory=AuthFlowStore)
    transport: object | None = None

    def redirect_uri(self, request_origin: str, provider: str) -> str:
        public_url = self.config.orchestrator.profiles.get("default")
        origin = public_url.public_url if public_url and public_url.public_url else request_origin
        return origin.rstrip("/") + f"/auth/callback/{provider}"

    async def _request(self, method: str, url: str, **kwargs):
        if self.transport is not None:
            return await self.transport.request(method, url, **kwargs)
        async with httpx.AsyncClient(timeout=30) as client:
            return await client.request(method, url, **kwargs)

    async def _discover(self, server_url: str) -> dict:
        try:
            response = await self._request(
                "GET", f"{server_url}/.well-known/oauth-protected-resource"
            )
            response.raise_for_status()
            servers = response.json().get("authorization_servers", [])
            if not servers:
                raise AuthError("OAuth discovery returned no authorization server")
            response = await self._request(
                "GET", f"{servers[0].rstrip('/')}/.well-known/oauth-authorization-server"
            )
            response.raise_for_status()
            return response.json()
        except AuthError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            raise ProviderUnavailableError("OAuth discovery failed") from exc

    async def _register(self, endpoint: str, redirect_uri: str) -> dict:
        try:
            response = await self._request(
                "POST",
                endpoint,
                json={
                    "client_name": "Archie",
                    "redirect_uris": [redirect_uri],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            raise ProviderUnavailableError("OAuth client registration failed") from exc

    async def complete(self, flow: AuthFlow, code: str) -> CredentialStatus:
        provider = self.provider(flow.provider)
        try:
            response = await self._request(
                "POST",
                flow.token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": flow.client_id,
                    "redirect_uri": flow.redirect_uri,
                    "code_verifier": flow.verifier,
                    **({"client_secret": flow.client_secret} if flow.client_secret else {}),
                },
            )
            if getattr(response, "is_error", False):
                raise AuthError(_oauth_error(response, "token exchange"))
            tokens = response.json()
        except AuthError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise ProviderUnavailableError("OAuth token exchange failed") from exc
        access = _nested(tokens, provider.token_path)
        if not access:
            raise AuthError("OAuth provider returned no access token")
        current = get_credential(flow.provider, CREDENTIAL_TYPES[flow.provider])
        fields = (
            {
                key: getattr(current, key)
                for key in current.__struct_fields__
                if getattr(current, key) is not None
            }
            if current
            else {}
        )
        fields.update({"access_token": access, "client_id": flow.client_id})
        refresh = _nested(tokens, provider.refresh_token_path)
        if refresh:
            fields["refresh_token"] = refresh
        expires = _nested(tokens, provider.expires_in_path)
        if expires is not None:
            fields["expires_at"] = (datetime.now(UTC) + timedelta(seconds=int(expires))).isoformat()
        replace_credential(flow.provider, fields)
        return self.status(flow.provider)

    async def refresh(self, provider_name: str) -> CredentialStatus:
        provider = self.provider(provider_name)
        if not isinstance(provider, OAuthProvider):
            raise AuthError(f"Provider '{provider_name}' cannot be refreshed")
        credential = get_credential(provider_name, CREDENTIAL_TYPES[provider_name])
        refresh = getattr(credential, "refresh_token", None) if credential else None
        if not refresh:
            raise AuthError(f"Provider '{provider_name}' requires reauthentication")
        override = self.config.auth.providers.get(provider_name, AuthProviderOverride())
        client_id = override.client_id or getattr(credential, "client_id", None)
        token_endpoint = (
            override.token_endpoint
            or getattr(credential, "token_endpoint", None)
            or provider.token_endpoint
        )
        if not client_id or not token_endpoint:
            raise AuthError(f"Provider '{provider_name}' requires reauthentication")
        response = await self._request(
            "POST",
            token_endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
                **(
                    {"client_secret": credential.client_secret}
                    if getattr(credential, "client_secret", None)
                    else {}
                ),
            },
        )
        response.raise_for_status()
        tokens = response.json()
        access = _nested(tokens, provider.token_path)
        if not access:
            raise AuthError("OAuth provider returned no access token")
        fields = {"access_token": access}
        rotated = _nested(tokens, provider.refresh_token_path)
        if rotated:
            fields["refresh_token"] = rotated
        expires = _nested(tokens, provider.expires_in_path)
        if expires is not None:
            fields["expires_at"] = (datetime.now(UTC) + timedelta(seconds=int(expires))).isoformat()
        current = {
            key: getattr(credential, key)
            for key in credential.__struct_fields__
            if getattr(credential, key) is not None
        }
        current.update(fields)
        replace_credential(provider_name, current)
        return self.status(provider_name)

    async def start_login(self, provider_name: str, redirect_uri: str) -> tuple[AuthFlow, str]:
        provider = self.provider(provider_name)
        if not isinstance(provider, OAuthProvider):
            raise AuthError(f"Provider '{provider_name}' is not an OAuth provider")
        try:
            credential = get_credential(provider_name, CREDENTIAL_TYPES[provider_name])
        except ValueError:
            credential = None
        metadata = {
            key: getattr(credential, key)
            for key in (
                "authorization_endpoint",
                "token_endpoint",
                "registration_endpoint",
                "client_id",
                "client_secret",
            )
            if credential is not None and getattr(credential, key, None) is not None
        }
        override = self.config.auth.providers.get(provider_name, AuthProviderOverride())
        endpoints = {
            "authorization_endpoint": provider.authorization_endpoint,
            "token_endpoint": provider.token_endpoint,
            "registration_endpoint": provider.registration_endpoint,
        }
        for key in endpoints:
            if getattr(override, key, None) is None and metadata.get(key):
                endpoints[key] = metadata[key]
        if (
            not endpoints["authorization_endpoint"] or not endpoints["token_endpoint"]
        ) and provider.server_url:
            discovery = await self._discover(provider.server_url)
            for key in endpoints:
                if (
                    getattr(override, key, None) is None
                    and not metadata.get(key)
                    and discovery.get(key)
                ):
                    endpoints[key] = discovery[key]
            replace_credential(provider_name, {**metadata, **endpoints})
        if not endpoints["authorization_endpoint"] or not endpoints["token_endpoint"]:
            raise AuthError(
                f"OAuth provider '{provider_name}' has incomplete endpoint configuration"
            )
        client_id = override.client_id or metadata.get("client_id")
        client_secret = metadata.get("client_secret")
        if not client_id and endpoints["registration_endpoint"]:
            registration = await self._register(endpoints["registration_endpoint"], redirect_uri)
            client_id = registration.get("client_id")
            if not client_id:
                raise AuthError(
                    f"OAuth provider '{provider_name}' registration returned no client_id"
                )
            client_secret = registration.get("client_secret") or client_secret
            replace_credential(
                provider_name,
                {
                    **metadata,
                    **endpoints,
                    "client_id": client_id,
                    **({"client_secret": client_secret} if client_secret else {}),
                },
            )
        if not client_id:
            raise AuthError(f"OAuth provider '{provider_name}' requires a configured client_id")
        verifier, challenge = generate_pkce()
        flow = self.flows.add(
            provider_name,
            verifier,
            redirect_uri,
            client_id,
            endpoints["token_endpoint"],
            client_secret,
        )
        return flow, build_auth_url(
            endpoints["authorization_endpoint"],
            client_id,
            redirect_uri,
            flow.state,
            challenge,
            scopes=provider.scopes,
            extra_params=provider.extra_params,
        )

    def flow_status(self, flow_id: str) -> OAuthFlowStatus:
        self.flows.purge()
        flow = self.flows.flows.get(flow_id)
        if flow is None:
            raise AuthError("Unknown or expired OAuth flow")
        return OAuthFlowStatus(
            flow_id,
            flow.provider,
            flow.status,
            credential_status=flow.credential_status,
            error=flow.error,
        )

    def find_flow(self, provider_name: str, state: str) -> AuthFlow:
        self.flows.purge()
        flow = next((item for item in self.flows.flows.values() if item.state == state), None)
        if flow is None or flow.provider != provider_name:
            raise AuthError("Invalid or expired OAuth flow")
        if flow.status != "pending":
            raise AuthError("OAuth flow already used")
        flow.status = "exchanging"
        return flow

    def provider(self, name: str):
        if name not in PROVIDERS:
            raise UnknownProviderError(f"Unknown auth provider '{name}'")
        override = self.config.auth.providers.get(name)
        return effective_provider(name, override)

    def provider_names(self):
        return list(PROVIDERS)

    def providers(self, redirect_uri: str | None = None):
        result = []
        for name in PROVIDERS:
            provider = self.provider(name)
            if isinstance(provider, StaticProvider):
                result.append(
                    StaticProviderResponse(
                        name=name,
                        fields=provider.fields,
                        can_refresh_noninteractive=provider.can_refresh_noninteractive,
                        env=provider.env,
                        set_env=provider.set_env,
                    )
                )
            else:
                override = self.config.auth.providers.get(name, AuthProviderOverride())
                credential = get_credential(name, CREDENTIAL_TYPES[name])
                persisted = (
                    {
                        key: getattr(credential, key, None)
                        for key in (
                            "authorization_endpoint",
                            "token_endpoint",
                            "registration_endpoint",
                            "client_id",
                        )
                    }
                    if credential
                    else {}
                )
                result.append(
                    OAuthProviderResponse(
                        name=name,
                        server_url=provider.server_url,
                        authorization_endpoint=provider.authorization_endpoint
                        or persisted.get("authorization_endpoint"),
                        token_endpoint=provider.token_endpoint or persisted.get("token_endpoint"),
                        registration_endpoint=provider.registration_endpoint
                        or persisted.get("registration_endpoint"),
                        scopes=provider.scopes or [],
                        token_path=provider.token_path,
                        refresh_token_path=provider.refresh_token_path,
                        expires_in_path=provider.expires_in_path,
                        extra_params=provider.extra_params or {},
                        can_refresh_noninteractive=provider.can_refresh_noninteractive,
                        client_id=override.client_id or persisted.get("client_id"),
                        redirect_uri=redirect_uri,
                    )
                )
        return result

    def status(self, name: str) -> CredentialStatus:
        provider = self.provider(name)
        auth_type = "oauth" if isinstance(provider, OAuthProvider) else "static"
        try:
            credential = get_credential(name, CREDENTIAL_TYPES[name])
        except (ValueError, TypeError):
            return CredentialStatus(
                name, auth_type, False, "needs_reauthentication", error="Invalid stored credential"
            )
        if credential is None:
            return CredentialStatus(name, auth_type, False, "missing")
        values = {field: getattr(credential, field) for field in credential.__struct_fields__}
        fields = provider.fields if isinstance(provider, StaticProvider) else ["access_token"]
        configured = all(values.get(field) for field in fields)
        if not configured:
            return CredentialStatus(name, auth_type, False, "missing")
        expires_at = values.get("expires_at")
        state = (
            "expired"
            if expires_at and _expired(expires_at)
            else "valid"
            if auth_type == "oauth"
            else "configured"
        )
        return CredentialStatus(name, auth_type, True, state, expires_at=expires_at)

    def replace_static(self, name: str, body: bytes) -> CredentialStatus:
        provider = self.provider(name)
        if not isinstance(provider, StaticProvider):
            raise CredentialValidationError(f"Provider '{name}' requires OAuth login")
        try:
            credential = msgspec.json.decode(body, type=CREDENTIAL_TYPES[name])
        except (msgspec.DecodeError, msgspec.ValidationError, TypeError) as exc:
            raise CredentialValidationError("Invalid credential fields") from exc
        values = {field: getattr(credential, field) for field in credential.__struct_fields__}
        missing = [field for field in provider.fields if not values.get(field)]
        if missing:
            raise CredentialValidationError(f"Missing required fields: {', '.join(missing)}")
        replace_credential(name, {key: value for key, value in values.items() if value is not None})
        return self.status(name)

    def delete(self, name: str) -> CredentialStatus:
        self.provider(name)
        delete_credential(name)
        return self.status(name)


def _nested(data: dict, path: str):
    current = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _expired(value: str) -> bool:
    from datetime import UTC, datetime, timedelta

    try:
        return datetime.fromisoformat(value).astimezone(UTC) <= datetime.now(UTC) + timedelta(
            seconds=60
        )
    except ValueError:
        return True

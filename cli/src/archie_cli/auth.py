"""Auth CLI commands — manage credentials for archie services.

Commands:
- archie auth bedrock: import AWS credentials from the standard toolchain
- archie auth login <service>: OAuth PKCE login (host-only)
- archie auth refresh <service>: non-interactive OAuth token refresh
- archie auth set <service> <field=value>...: set static credentials
- archie auth status: show credential status for all services
"""

import secrets
from datetime import UTC, datetime

import click
import httpx
from archie_shared.credentials import (
    PROVIDERS,
    InteractiveReauthRequired,
    extract_nested,
    get_credential,
    is_expired,
    set_credential,
)
from archie_shared.credentials.providers import OAuthProvider


@click.group()
def auth():
    """Manage credentials for archie services."""


@auth.command()
def bedrock():
    """Import AWS credentials for Bedrock from the standard toolchain.

    Reads credentials via boto3's credential chain (env vars, ~/.aws/credentials,
    SSO cache, instance profile) and writes them to ~/.nexus/credentials.yaml.

    The containerised agent uses these credentials exclusively for Bedrock API calls,
    independent of any other AWS account configuration.
    """
    try:
        import boto3
        import botocore.exceptions
    except ImportError:
        raise click.ClickException(
            "boto3 is required for credential import.\nInstall it with: pip install boto3"
        ) from None

    click.echo("Reading AWS credentials from standard toolchain...")

    try:
        session = boto3.Session()
        credentials = session.get_credentials()
        if credentials is None:
            raise click.ClickException(
                "No AWS credentials found.\n"
                "Configure credentials via:\n"
                "  - AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env vars\n"
                "  - aws configure\n"
                "  - aws sso login"
            )

        # Resolve credentials (handles STS assume-role, SSO, etc.)
        resolved = credentials.get_frozen_credentials()

        fields: dict[str, str | None] = {
            "aws_access_key_id": resolved.access_key,
            "aws_secret_access_key": resolved.secret_key,
        }
        if resolved.token:
            fields["aws_session_token"] = resolved.token
        else:
            fields["aws_session_token"] = None  # Remove stale session token

        # Verify credentials work by calling STS
        try:
            sts = session.client("sts")
            identity = sts.get_caller_identity()
            account = identity.get("Account", "unknown")
            arn = identity.get("Arn", "unknown")
            click.echo(f"  Account: {account}")
            click.echo(f"  Identity: {arn}")
        except botocore.exceptions.ClientError as e:
            raise click.ClickException(f"Credentials invalid: {e}") from None
        except botocore.exceptions.NoCredentialsError:
            raise click.ClickException("Could not resolve credentials") from None

        # Check if temporary (session token present = will expire)
        if resolved.token:
            click.echo("  Type: temporary (session token present — will expire)")
        else:
            click.echo("  Type: long-lived (no session token)")

        set_credential("bedrock", fields)
        click.echo("\n✓ Bedrock credentials saved.")

    except botocore.exceptions.NoCredentialsError:
        raise click.ClickException(
            "No AWS credentials found in the standard chain.\n"
            "Run 'aws configure' or 'aws sso login' first."
        ) from None
    except botocore.exceptions.ProfileNotFound as e:
        raise click.ClickException(str(e)) from None


@auth.command(name="login")
@click.argument("service")
def login(service: str):
    """Authenticate with an OAuth service via browser."""
    from archie_shared.credentials.oauth import (
        CALLBACK_PORT,
        build_auth_url,
        discover_endpoints,
        exchange_code,
        generate_pkce,
        open_browser,
        register_client,
        wait_for_callback,
    )

    provider = PROVIDERS.get(service)
    if not isinstance(provider, OAuthProvider):
        raise click.ClickException(f"'{service}' is not an OAuth provider.")

    redirect_uri = f"http://localhost:{CALLBACK_PORT}/callback"

    # Load existing credential for stored endpoints/client_id
    cred = get_credential(service)

    # Resolve endpoints
    auth_endpoint = (
        getattr(cred, "authorization_endpoint", None) if cred else None
    ) or provider.authorization_endpoint
    token_endpoint = (
        getattr(cred, "token_endpoint", None) if cred else None
    ) or provider.token_endpoint
    reg_endpoint = getattr(cred, "registration_endpoint", None) if cred else None

    # Discover if needed
    if not auth_endpoint or not token_endpoint:
        if not provider.server_url:
            raise click.ClickException(
                f"No endpoints configured for '{service}' and no server_url for discovery."
            )
        click.echo("Discovering OAuth endpoints...", err=True)
        metadata = discover_endpoints(provider.server_url)
        auth_endpoint = metadata["authorization_endpoint"]
        token_endpoint = metadata["token_endpoint"]
        reg_endpoint = metadata.get("registration_endpoint")

        # Store discovered endpoints in credential
        set_credential(
            service,
            {
                "authorization_endpoint": auth_endpoint,
                "token_endpoint": token_endpoint,
                "registration_endpoint": reg_endpoint,
            },
        )

    # Resolve client_id
    client_id = getattr(cred, "client_id", None) if cred else None
    client_secret = getattr(cred, "client_secret", None) if cred else None

    if not client_id:
        if not reg_endpoint:
            raise click.ClickException(
                f"No client_id or registration_endpoint for '{service}'.\n"
                f"Set client_id with: archie auth set {service} client_id=<value>"
            )
        click.echo("Registering OAuth client...", err=True)
        client_data = register_client(reg_endpoint, redirect_uri)
        client_id = client_data["client_id"]
        store_fields: dict[str, str | None] = {"client_id": client_id}
        if "client_secret" in client_data:
            client_secret = client_data["client_secret"]
            store_fields["client_secret"] = client_secret
        set_credential(service, store_fields)

    # Run OAuth flow
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(16)

    auth_url = build_auth_url(
        auth_endpoint,
        client_id,
        redirect_uri,
        state,
        challenge,
        scopes=provider.scopes,
        extra_params=provider.extra_params,
    )

    if not open_browser(auth_url):
        click.echo(f"\nOpen this URL in your browser:\n{auth_url}\n", err=True)

    click.echo("Waiting for authentication...", err=True)
    code, returned_state, error = wait_for_callback()

    if error:
        raise click.ClickException(f"Authentication failed: {error}")

    if not code:
        raise click.ClickException("Authentication timed out.")

    if returned_state != state:
        raise click.ClickException("State mismatch — possible CSRF attack.")

    tokens = exchange_code(
        token_endpoint,
        client_id,
        code,
        verifier,
        redirect_uri,
        client_secret=client_secret,
    )

    _store_tokens(service, tokens, provider)
    click.echo(f"✓ Authenticated with {service}.")


@auth.command(name="refresh")
@click.argument("service")
def refresh(service: str):
    """Refresh OAuth tokens for a service (non-interactive)."""
    from archie_shared.credentials import refresh_credential

    try:
        refresh_credential(service)
        click.echo(f"✓ Refreshed {service} credentials.")
    except InteractiveReauthRequired as e:
        raise click.ClickException(str(e)) from None
    except ValueError as e:
        raise click.ClickException(str(e)) from None


@auth.command(name="set")
@click.argument("service")
@click.argument("fields", nargs=-1, required=True)
def set_cmd(service: str, fields: tuple[str, ...]):
    """Set credential fields for a static service.

    Usage: archie auth set <service> field1=value1 field2=value2
    """
    parsed: dict[str, str | None] = {}
    for field in fields:
        if "=" not in field:
            raise click.ClickException(f"Invalid format: '{field}'. Use field=value.")
        key, value = field.split("=", 1)
        parsed[key] = value

    set_credential(service, parsed)
    click.echo(f"✓ Credentials saved for {service}.")


@auth.command()
def status():
    """Show credential status for all configured services."""
    from archie_shared.credentials.store import load_store

    store = load_store()

    if not store:
        click.echo("No credentials configured.")
        click.echo("  Run: archie auth bedrock")
        return

    for service, entry in sorted(store.items()):
        if not isinstance(entry, dict):
            continue

        provider = PROVIDERS.get(service)
        provider_type = "oauth" if isinstance(provider, OAuthProvider) else "static"

        # Check expiry
        expires_at = entry.get("expires_at")
        if expires_at:
            expired = is_expired(expires_at)
            expiry_str = " (expired)" if expired else f" (expires: {expires_at})"
        else:
            expiry_str = ""

        # Mask sensitive values
        display_fields = []
        for key, value in entry.items():
            if value is None:
                continue
            if any(
                s in key for s in ("secret", "token", "password", "access_key", "session_token")
            ):
                masked = f"{str(value)[:4]}..." if len(str(value)) > 4 else "****"
                display_fields.append(f"  {key}: {masked}")
            else:
                display_fields.append(f"  {key}: {value}")

        click.echo(f"{service} [{provider_type}]{expiry_str}:")
        if display_fields:
            for f in display_fields:
                click.echo(f)
        else:
            click.echo("  (empty)")


@auth.command(name="push")
@click.argument("profile")
def auth_push(profile: str):
    """Push local credentials to a remote orchestrator.

    Reads ~/.nexus/credentials.yaml and POSTs it to the named profile's
    orchestrator at POST /credentials. The remote orchestrator writes the
    file atomically with 0600 permissions.
    """
    from archie_shared.config import home_dir
    from archie_shared.schemas import load_nexus_config

    config = load_nexus_config()
    prof = config.orchestrator.profiles.get(profile)
    if prof is None:
        raise click.ClickException(
            f"Unknown profile: '{profile}'.\n"
            "Add it to ~/.nexus/config.yaml under orchestrator.profiles."
        )

    cred_path = home_dir() / "credentials.yaml"
    if not cred_path.exists():
        raise click.ClickException(
            "No local credentials.yaml found.\n"
            "Run 'archie auth bedrock' or 'archie auth set' to create credentials first."
        )

    url = f"http://{prof.host}:{prof.port}"
    try:
        resp = httpx.post(
            f"{url}/credentials",
            content=cred_path.read_bytes(),
            timeout=10.0,
        )
    except httpx.ConnectError:
        raise click.ClickException(
            f"Cannot connect to orchestrator at {url}.\n"
            "Ensure 'archie serve' is running on the remote host."
        ) from None

    if resp.status_code == 200:
        click.echo(f"✓ Credentials pushed to {profile} ({prof.host}:{prof.port})")
    else:
        raise click.ClickException(
            f"Push failed: HTTP {resp.status_code}\n"
            f"{resp.text[:200]}"
        )


def _store_tokens(service: str, tokens: dict, provider: OAuthProvider) -> None:
    """Extract and store tokens from an OAuth response."""
    access = extract_nested(tokens, provider.token_path)
    if not access:
        raise click.ClickException(f"No access token in response for '{service}'.")

    fields: dict[str, str | None] = {"access_token": access}

    refresh_tok = extract_nested(tokens, provider.refresh_token_path)
    if refresh_tok:
        fields["refresh_token"] = refresh_tok

    # Calculate expires_at from expires_in
    expires_in = tokens.get("expires_in")
    if not expires_in:
        # Check nested (e.g. Slack's authed_user.expires_in)
        authed = tokens.get("authed_user")
        if isinstance(authed, dict):
            expires_in = authed.get("expires_in")
    if expires_in:
        from datetime import timedelta

        expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))
        fields["expires_at"] = expires_at.isoformat()

    set_credential(service, fields)

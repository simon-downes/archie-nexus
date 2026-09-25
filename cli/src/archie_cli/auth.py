"""Auth CLI commands — manage credentials for archie services.

Commands:
- archie auth login <service>: OAuth or static credential login
- archie auth refresh <service>: non-interactive OAuth token refresh
- archie auth status: show redacted credential status
"""

import json
import os
import sys

import click
import httpx
from archie_shared.credentials import PROVIDERS
from archie_shared.credentials.providers import StaticProvider, effective_provider


@click.group()
def auth():
    """Manage credentials for archie services."""


def _orchestrator_url() -> str:
    from archie_shared.schemas import get_profile, load_nexus_config

    profile = get_profile(load_nexus_config().orchestrator)
    return f"http://{profile.host}:{profile.port}"


def _is_secret_field(field: str) -> bool:
    return any(part in field.lower() for part in ("token", "secret", "password", "key"))


def _response_error(response: httpx.Response) -> str:
    """Return a safe, useful error from an orchestrator JSON error response."""
    try:
        payload = response.json()
    except ValueError:
        return f"orchestrator returned HTTP {response.status_code}"
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        detail = payload["error"].strip()
        if detail:
            return detail
    return f"orchestrator returned HTTP {response.status_code}"


def _set_static_from_login(service: str) -> None:
    provider = PROVIDERS.get(service)
    if provider is None:
        raise click.ClickException(f"Unknown provider: {service}")
    from archie_shared.schemas import load_nexus_config

    provider = effective_provider(service, load_nexus_config().auth.providers.get(service))
    if not isinstance(provider, StaticProvider):
        raise click.ClickException(f"{service} requires OAuth login")
    if service in {"bedrock", "aws"}:
        _set_aws_from_chain(service)
        return
    if provider.env:
        fields = {field: os.environ.get(env_name) for field, env_name in provider.env.items()}
        missing = [env_name for field, env_name in provider.env.items() if not fields.get(field)]
        if missing:
            raise click.ClickException(f"Missing environment variables: {', '.join(missing)}")
    else:
        stdin_text = "" if sys.stdin.isatty() else sys.stdin.read().strip()
        if stdin_text:
            try:
                fields = json.loads(stdin_text)
            except json.JSONDecodeError as exc:
                raise click.ClickException("Credential stdin must be a JSON object") from exc
            if not isinstance(fields, dict):
                raise click.ClickException("Credential stdin must be a JSON object")
        else:
            provider = PROVIDERS.get(service)
            if provider is None or not hasattr(provider, "fields"):
                raise click.ClickException(f"Unknown static provider: {service}")
            fields = {}
            for field in provider.fields:
                fields[field] = click.prompt(
                    f"{service} {field}",
                    hide_input=_is_secret_field(field),
                    err=True,
                )
    try:
        response = httpx.put(
            f"{_orchestrator_url()}/auth/credential/{service}", json=fields, timeout=10
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise click.ClickException("Could not set credentials through orchestrator") from exc
    click.echo(f"✓ Credentials saved for {service}.")


def _set_aws_from_chain(service: str) -> None:
    try:
        import boto3
        import botocore.exceptions
    except ImportError:
        raise click.ClickException("boto3 is required for AWS credential login") from None
    try:
        session = boto3.Session()
        credentials = session.get_credentials()
        if credentials is None:
            raise click.ClickException("No AWS credentials found in the standard chain")
        resolved = credentials.get_frozen_credentials()
        fields = {
            "access_key_id" if service == "aws" else "aws_access_key_id": resolved.access_key,
            "secret_access_key"
            if service == "aws"
            else "aws_secret_access_key": resolved.secret_key,
            "session_token" if service == "aws" else "aws_session_token": resolved.token,
        }
        identity = session.client("sts").get_caller_identity()
        click.echo(f"  Account: {identity.get('Account', 'unknown')}")
    except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as exc:
        raise click.ClickException("AWS credentials could not be validated") from exc
    try:
        response = httpx.put(
            f"{_orchestrator_url()}/auth/credential/{service}", json=fields, timeout=10
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise click.ClickException("Could not set credentials through orchestrator") from exc
    click.echo(f"✓ Credentials saved for {service}.")


@auth.command(name="login")
@click.argument("service")
def login(service: str):
    """Authenticate an OAuth or static provider through the local orchestrator."""
    if service in {"linear", "github", "aws", "scalr", "jira", "bedrock"}:
        _set_static_from_login(service)
        return
    try:
        response = httpx.post(f"{_orchestrator_url()}/auth/login/{service}", timeout=10)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as exc:
        detail = _response_error(exc.response)
        raise click.ClickException(f"Could not start OAuth login for {service}: {detail}") from exc
    except (httpx.RequestError, ValueError, KeyError) as exc:
        raise click.ClickException(
            f"Could not start OAuth login for {service}: orchestrator unavailable or returned invalid data"
        ) from exc
    from archie_shared.credentials.oauth import open_browser

    redirect_uri = data.get("redirect_uri")
    if not isinstance(redirect_uri, str) or not redirect_uri:
        raise click.ClickException(
            f"Could not start OAuth login for {service}: orchestrator omitted the callback URI"
        )
    click.echo(f"OAuth callback: {redirect_uri}", err=True)
    if not open_browser(data["authorization_url"]):
        click.echo(f"Open this URL in your browser:\n{data['authorization_url']}", err=True)
    click.echo("Waiting for authentication...", err=True)
    flow_id = data["flow_id"]
    for _ in range(120):
        import time

        time.sleep(1)
        try:
            status = httpx.get(f"{_orchestrator_url()}/auth/flow/{flow_id}", timeout=10).json()
        except (httpx.HTTPError, ValueError) as exc:
            raise click.ClickException("Could not query orchestrator OAuth flow") from exc
        if status["status"] == "succeeded":
            click.echo(f"✓ Authenticated with {service}.")
            return
        if status["status"] == "failed":
            raise click.ClickException(status.get("error") or "Authentication failed")
    raise click.ClickException("Authentication timed out.")


@auth.command(name="refresh")
@click.argument("service")
def refresh(service: str):
    """Refresh OAuth tokens through the local orchestrator."""
    try:
        response = httpx.post(f"{_orchestrator_url()}/auth/refresh/{service}", timeout=10)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise click.ClickException("Could not refresh credentials through orchestrator") from exc
    click.echo(f"✓ Refreshed {service} credentials.")


@auth.command()
def status():
    """Show redacted credential status from the local orchestrator."""
    try:
        response = httpx.get(f"{_orchestrator_url()}/auth/status", timeout=10)
        response.raise_for_status()
        statuses = response.json()
    except httpx.HTTPError as exc:
        raise click.ClickException("Could not query credentials through orchestrator") from exc
    for item in statuses:
        expiry = f" (expires: {item['expires_at']})" if item.get("expires_at") else ""
        click.echo(f"{item['provider']} [{item['auth_type']}]: {item['state']}{expiry}")

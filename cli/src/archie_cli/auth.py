"""Auth CLI commands — manage credentials for archie services.

archie auth bedrock: reads AWS credentials from the standard toolchain
(env vars, ~/.aws/credentials, SSO session) and writes them to
~/.archie/nexus.creds.yaml for use by the containerised agent.

This decouples the agent's Bedrock credentials from the host's general
AWS configuration, supporting multi-account environments where the
Bedrock account differs from the tooling account.
"""

import click
from archie_shared.credentials import (
    SERVICE_BEDROCK,
    get_service_credentials,
    set_service_credentials,
)


@click.group()
def auth():
    """Manage credentials for archie services."""


@auth.command()
def bedrock():
    """Import AWS credentials for Bedrock from the standard toolchain.

    Reads credentials via boto3's credential chain (env vars, ~/.aws/credentials,
    SSO cache, instance profile) and writes them to ~/.archie/nexus.creds.yaml.

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

        fields = {
            "aws_access_key_id": resolved.access_key,
            "aws_secret_access_key": resolved.secret_key,
        }
        if resolved.token:
            fields["aws_session_token"] = resolved.token

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

        set_service_credentials(SERVICE_BEDROCK, fields)
        click.echo("\n✓ Bedrock credentials saved to ~/.archie/nexus.creds.yaml")

    except botocore.exceptions.NoCredentialsError:
        raise click.ClickException(
            "No AWS credentials found in the standard chain.\n"
            "Run 'aws configure' or 'aws sso login' first."
        ) from None
    except botocore.exceptions.ProfileNotFound as e:
        raise click.ClickException(str(e)) from None


@auth.command()
def status():
    """Show credential status for all services."""
    creds_data = get_service_credentials(SERVICE_BEDROCK)
    if creds_data:
        key_id = creds_data.get("aws_access_key_id", "")
        has_session = "aws_session_token" in creds_data
        # Mask the key — show first 4 and last 4
        masked = f"{key_id[:4]}...{key_id[-4:]}" if len(key_id) > 8 else "****"
        click.echo("bedrock:")
        click.echo(f"  access_key: {masked}")
        click.echo(f"  session_token: {'yes' if has_session else 'no'}")
    else:
        click.echo("bedrock: not configured")
        click.echo("  Run: archie auth bedrock")

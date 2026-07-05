"""Archie agent server entry point."""

import click
import uvicorn


@click.command()
@click.option("--host", default="0.0.0.0", help="Bind address.")
@click.option("--port", default=8080, type=int, help="Bind port.")
def main(host: str, port: int):
    """Start the archie agent server."""
    uvicorn.run("archie_agent.app:app", host=host, port=port)

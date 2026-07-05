#!/bin/sh
# Archie agent container entrypoint.
# Installs the agent package from the mounted source (deps already cached in image),
# then starts the server. exec replaces the shell so signals go directly to uvicorn.
set -e
export UV_PROJECT_ENVIRONMENT=/opt/archie/venv
cd /opt/archie/agent
uv sync --no-dev --quiet
exec archie-agent

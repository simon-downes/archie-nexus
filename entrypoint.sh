#!/bin/sh
# Archie agent container entrypoint.
# Installs the agent package from the mounted source (deps already cached in image),
# then starts the server. exec replaces the shell so signals go directly to uvicorn.
set -e
cd /opt/archie
# Scoped to this command so the agent and its children keep the image default
# (.venv-archie per project) and never sync into the agent's own venv.
UV_PROJECT_ENVIRONMENT=/opt/archie/venv uv sync --package archie-agent --no-dev --quiet
exec archie-agent

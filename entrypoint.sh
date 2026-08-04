#!/bin/sh
# Archie agent container entrypoint.
# Installs the agent package from the mounted source (deps already cached in image),
# then starts the server. exec replaces the shell so signals go directly to uvicorn.
set -e
export UV_PROJECT_ENVIRONMENT=/opt/archie/venv
# The /workspace bind mount can present with an owner (e.g. root on macOS Docker
# Desktop's VirtioFS) that differs from the container user, tripping git's
# "dubious ownership" guard. Mark it safe so agent git operations work regardless
# of how the host filesystem is projected into the container.
git config --global --add safe.directory /workspace
cd /opt/archie
uv sync --package archie-agent --no-dev --quiet
exec archie-agent

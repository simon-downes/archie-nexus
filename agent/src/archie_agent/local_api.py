"""The agent's API for its own subprocesses, on a Unix socket (plan 090).

Subprocesses (the exec runner, the ``gh`` wrapper, AWS ``credential-process``) have no
agent token and don't read the host files. They ask the agent, which answers from its
``host_config`` cache:

  GET /credentials/{provider}        — ``{"value": <raw stored entry or null>}``
  GET /plugins/{plugin_id}/settings  — resolved settings (manifest defaults applied)

The socket is :func:`socket_path` (``ARCHIE_AGENT_SOCKET``, default
:data:`DEFAULT_SOCKET`) in a 0700 directory. These routes are never on port 8080, which
Docker publishes to the host's loopback. Any process running as the container user can
ask; that is deliberate access, not incidental exposure.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import msgspec
import uvicorn
from archie_shared.config import ConfigError
from archie_shared.credentials.models import CREDENTIAL_TYPES
from archie_shared.plugins.config import resolved_settings
from archie_shared.plugins.manifest import load_manifests
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from archie_agent import host_config

log = logging.getLogger(__name__)

SOCKET_ENV = "ARCHIE_AGENT_SOCKET"
DEFAULT_SOCKET = "/tmp/archie-agent/agent.sock"
CLIENT_TIMEOUT = 5.0
"""Seconds a subprocess waits for the agent's answer."""


class LocalApiError(RuntimeError):
    """The agent couldn't be asked over its socket, or couldn't answer."""


def socket_path() -> Path:
    """Where the agent's socket is: ``ARCHIE_AGENT_SOCKET``, else :data:`DEFAULT_SOCKET`."""
    return Path(os.environ.get(SOCKET_ENV) or DEFAULT_SOCKET)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


def _json(value: object, status_code: int = 200) -> Response:
    return Response(
        msgspec.json.encode(value), status_code=status_code, media_type="application/json"
    )


async def credential(request: Request) -> Response:
    """One provider's raw stored entry, fetched through the cache on a miss."""
    name = request.path_params["provider"]
    if name not in CREDENTIAL_TYPES:
        return _json({"error": f"unknown provider '{name}'"}, 404)
    try:
        entry = await asyncio.to_thread(host_config.current().credential_entry, name)
    except (host_config.HostConfigError, ConfigError) as exc:
        return _json({"error": str(exc)}, 503)
    return _json({"value": entry})


async def plugin_settings(request: Request) -> Response:
    """One plugin's settings from the held config, with its manifest defaults (D25)."""
    plugin_id = request.path_params["plugin_id"]
    manifests = await asyncio.to_thread(load_manifests)
    if plugin_id not in manifests:
        return _json({"error": f"unknown plugin '{plugin_id}'"}, 404)
    try:
        config = host_config.get_config()
    except (host_config.HostConfigError, ConfigError) as exc:
        return _json({"error": str(exc)}, 503)
    return _json(resolved_settings(plugin_id, config.plugins, manifests))


app = Starlette(
    routes=[
        Route("/credentials/{provider}", credential, methods=["GET"]),
        Route("/plugins/{plugin_id}/settings", plugin_settings, methods=["GET"]),
    ]
)


class _NestedServer(uvicorn.Server):
    """A uvicorn server that leaves signal handling to the agent's main server (D15).

    uvicorn swaps the process's SIGINT/SIGTERM handlers while a server runs; a nested
    server doing so would take SIGTERM from the main one and stop first.
    """

    def capture_signals(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()


@contextlib.asynccontextmanager
async def serve_local(path: Path | None = None) -> AsyncIterator[Path]:
    """Serve :data:`app` on a Unix socket for the duration of the block.

    Sets ``ARCHIE_AGENT_SOCKET`` in ``os.environ`` so every subprocess finds the socket.

    Raises:
        RuntimeError: If the server stops during startup.
    """
    path = path or socket_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.unlink(missing_ok=True)  # a socket left by an earlier server in this container
    config = uvicorn.Config(app, uds=str(path), log_level="warning", lifespan="off")
    server = _NestedServer(config)
    task = asyncio.create_task(server.serve())
    while not server.started and not task.done():
        await asyncio.sleep(0.01)
    if task.done():
        task.result()
        raise RuntimeError(f"the agent's local API stopped during startup ({path})")
    os.environ[SOCKET_ENV] = str(path)
    log.info("Local API listening on %s", path)
    try:
        yield path
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await task
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Client (subprocesses)
# ---------------------------------------------------------------------------


def fetch(path: str) -> Any:
    """GET ``path`` from the agent's socket and return the decoded JSON body.

    Raises:
        LocalApiError: If the socket can't be reached or the agent answers with an error.
    """
    where = socket_path()
    transport = httpx.HTTPTransport(uds=str(where))
    try:
        with httpx.Client(transport=transport, timeout=CLIENT_TIMEOUT) as client:
            reply = client.get(f"http://agent{path}")
    except httpx.HTTPError as exc:
        raise LocalApiError(f"can't reach the agent at {where}: {exc}") from None
    if reply.status_code != 200:
        try:
            reason = reply.json().get("error", "")
        except ValueError:
            reason = ""
        raise LocalApiError(f"the agent answered {path} with HTTP {reply.status_code} {reason}")
    return reply.json()


def get_credential[T](name: str, credential_type: type[T] | None = None) -> T | None:
    """A provider's credential from the agent, as its typed struct, or None.

    Raises:
        LocalApiError: If the agent can't be asked or can't send it.
        ValueError: If the entry doesn't fit the type.
    """
    if credential_type is None:
        credential_type = CREDENTIAL_TYPES[name]
    entry = fetch(f"/credentials/{name}")["value"]
    if entry is None:
        return None
    try:
        return msgspec.convert(entry, credential_type, strict=False)
    except msgspec.ValidationError as exc:
        raise ValueError(f"Validation error for '{name}' from the agent: {exc}") from None

"""Routes for agent containers, mounted at ``/api/agent`` (plan 090).

Agents don't read ``config.yaml`` or ``credentials.yaml``; they sync them from here:

  POST   /sync    — the agent's held versions in, what changed out

Every request needs ``Authorization: Bearer <ARCHIE_AGENT_TOKEN>``. The token hashes to
one session's ``agent_token_hash``, and that session must still be live. A token
identifies its session only; it carries none of the user's authority.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import msgspec
from archie_shared.agent_sync import SyncItem, SyncRequest, SyncResponse, token_hash, version_of
from archie_shared.config import ConfigError
from archie_shared.credentials.models import CREDENTIAL_TYPES
from archie_shared.credentials.store import load_store
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from archie_orchestrator.config_source import config_source

if TYPE_CHECKING:
    from archie_shared.schemas import NexusConfig

log = logging.getLogger(__name__)

_BEARER = "bearer "


def answer_sync(
    request: SyncRequest,
    *,
    load_config: Callable[[], NexusConfig],
    load_store: Callable[[], dict[str, Any]],
) -> SyncResponse:
    """Build the reply to one sync: only the items whose version differs from the request's.

    Args:
        request: The versions the agent holds.
        load_config: Returns the validated host config; raises ``ConfigError`` when invalid.
        load_store: Returns the raw credential store; called only when credentials are asked.

    Returns:
        The changed config and credentials, and the errors for what couldn't be answered.
        A store that can't be read is reported by exception type only, since its YAML
        errors can quote a line of secrets.
    """
    response = SyncResponse()
    try:
        config = load_config()
    except ConfigError as exc:
        response.config_error = str(exc) or type(exc).__name__
    else:
        value = msgspec.to_builtins(config)
        version = version_of(value)
        if version != request.config:
            response.config = SyncItem(version=version, value=value)
    if not request.credentials:
        return response
    store: dict[str, Any] = {}
    store_error: str | None = None
    try:
        store = load_store()
    except Exception as exc:  # noqa: BLE001 — any unreadable store is reported per name
        store_error = f"the credential store can't be read ({type(exc).__name__})"
    for name, held in sorted(request.credentials.items()):
        if name not in CREDENTIAL_TYPES:
            response.credential_errors[name] = f"unknown provider '{name}'"
            continue
        if store_error is not None:
            response.credential_errors[name] = store_error
            continue
        entry = store.get(name)
        if entry is not None and not isinstance(entry, dict):
            response.credential_errors[name] = f"the stored '{name}' entry isn't a mapping"
            continue
        version = version_of(entry)
        if version != held:
            response.credentials[name] = SyncItem(version=version, value=entry)
    return response


def _json(value: object, status_code: int = 200) -> Response:
    return Response(
        msgspec.json.encode(value), status_code=status_code, media_type="application/json"
    )


async def _authenticated_session(request: Request) -> str | None:
    """The live session whose agent token the request carries, or None."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith(_BEARER):
        return None
    token = header[len(_BEARER) :].strip()
    if not token:
        return None
    tracker = request.app.state.tracker
    session_id = await asyncio.to_thread(tracker.store.session_for_token_hash, token_hash(token))
    if session_id is None or tracker.live(session_id) is None:
        return None
    return session_id


async def sync(request: Request) -> Response:
    """Answer an agent's sync (see :func:`answer_sync`); 401 without a live session's token."""
    session_id = await _authenticated_session(request)
    if session_id is None:
        return _json({"error": "unauthorized"}, 401)
    try:
        body = msgspec.json.decode(await request.body(), type=SyncRequest)
    except (msgspec.DecodeError, msgspec.ValidationError) as exc:
        return _json({"error": f"invalid sync request: {exc}"}, 400)
    response = await asyncio.to_thread(
        answer_sync, body, load_config=config_source.get, load_store=load_store
    )
    sent = sorted(response.credentials)
    if response.config is not None:
        sent.insert(0, "config")
    errors = sorted(response.credential_errors)
    if response.config_error is not None:
        errors.insert(0, "config")
    if sent or errors:
        log.info(
            "Agent sync for %s: sent %s; errors %s",
            session_id,
            ", ".join(sent) or "nothing",
            ", ".join(errors) or "none",
        )
    else:
        log.debug("Agent sync for %s: nothing changed", session_id)
    return _json(response)


routes = [
    Route("/sync", sync, methods=["POST"]),
]

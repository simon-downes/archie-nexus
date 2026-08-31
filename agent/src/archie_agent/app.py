"""Archie agent — Starlette application with WebSocket and HTTP endpoints.

Endpoints:
- GET /status — health check + session metadata
- WS /stream — bidirectional event streaming
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from archie_shared.canonical_events import ModelSwitch
from archie_shared.config import home_dir
from archie_shared.events import (
    PROTOCOL_VERSION,
    InterruptCommand,
    MessageCommand,
    SessionInfo,
    SessionSnapshot,
    SwitchModelCommand,
    TurnError,
    deserialize_command,
    serialize_event,
)
from archie_shared.models import get_model, load_models
from archie_shared.schemas import load_nexus_config
from archie_shared.session.log import (
    MessageEntry,
    read_event_lines,
    session_accounting,
    write_entry,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
from ulid import ULID

from archie_agent.harness import AgentHarness
from archie_agent.llm import create_llm_client
from archie_agent.session import Session

if TYPE_CHECKING:
    from archie_shared.models import ModelEntry
    from archie_shared.schemas import NexusConfig

log = logging.getLogger(__name__)

# Module-level agent reference, set during lifespan
_agent: AgentHarness | None = None
_catalog: dict[str, "ModelEntry"] | None = None
_config: "NexusConfig | None" = None

# Strong references to active tasks (prevents GC of fire-and-forget coroutines)
_active_tasks: set[asyncio.Task] = set()

# Workspace path where .git/HEAD lives
_WORKSPACE = "/workspace"


def _read_git_branch() -> str:
    """Read the current git branch from /workspace/.git/HEAD.

    Returns:
        Branch name, first 8 chars of detached HEAD hash, or "—" if unavailable.
    """
    try:
        head_path = os.path.join(_WORKSPACE, ".git", "HEAD")
        with open(head_path) as f:
            content = f.read().strip()
    except OSError:
        return "—"

    if not content:
        return "—"

    # Normal branch: "ref: refs/heads/<branch>"
    if content.startswith("ref: refs/heads/"):
        return content[len("ref: refs/heads/") :]

    # Detached HEAD: raw commit hash
    return content[:8]


@asynccontextmanager
async def lifespan(app):
    """Initialize the agent loop on startup."""
    global _agent, _catalog, _config

    _config = load_nexus_config()
    _catalog = load_models(home_dir() / "models.yaml")
    model = get_model(_catalog, _config.global_.model)

    llm_client = create_llm_client(model, _config.global_.region)

    session_id = os.environ.get("ARCHIE_SESSION_ID")
    if not session_id:
        log.error(
            "ARCHIE_SESSION_ID is not set. "
            "This variable is injected by the host CLI at container start."
        )
        import sys

        sys.exit(1)

    sessions_dir = home_dir() / "sessions"

    session = Session(
        model_id=_config.global_.model,
        model=model,
        session_id=session_id,
    )

    _agent = AgentHarness(
        session=session,
        llm_client=llm_client,
        model_name=model.name,
        log_dir=sessions_dir,
        model_catalog=_catalog,
        region=_config.global_.region,
        subagents=_config.agent.subagents,
    )

    log.info(
        "Agent started",
        extra={
            "model": _config.global_.model,
            "region": _config.global_.region,
            "session": session_id,
        },
    )
    yield
    log.info("Agent shutting down")


async def status(request: Request) -> JSONResponse:
    """Health check + session metadata."""
    if _agent is None:
        return JSONResponse({"status": "starting"}, status_code=503)
    return JSONResponse(
        {
            "status": "ok",
            "model": _agent.session.model_id,
            "session_id": _agent.session.session_id,
            "turn_count": _agent.session.turn_index,
            "turn_active": _agent.turn_active,
        }
    )


async def events(request: Request):
    """Replay canonical persisted events as ordered NDJSON."""
    if _agent is None:
        return JSONResponse({"error": "no session"}, status_code=503)
    after = request.query_params.get("after")
    try:
        items = read_event_lines(_agent.log_path)
        if after is not None and after and not any(item["id"] == after for item in items):
            return JSONResponse({"error": "cursor_not_found", "cursor": after}, status_code=409)
        start = (
            next((i + 1 for i, item in enumerate(items) if item["id"] == after), 0) if after else 0
        )
        body = "".join(item["line"] + "\n" for item in items[start:])
        return Response(body, media_type="application/x-ndjson")
    except Exception as exc:
        log.warning("Failed to replay events", exc_info=True)
        return JSONResponse({"error": str(exc)}, status_code=500)


async def _handle_model_switch(command: SwitchModelCommand, websocket: WebSocket) -> None:
    """Handle a model switch request.

    Guards against active turns, validates the model key, rebuilds the LLM
    client and session state, then broadcasts confirmation.
    """
    assert _agent is not None
    assert _catalog is not None
    assert _config is not None

    turn_index = _agent.session.turn_index or 0

    # Guard: cannot switch during active turn
    if _agent.turn_active:
        await _agent.event_bus.broadcast_serialized(
            serialize_event(
                TurnError(turn_index=turn_index, message="Cannot switch model during active turn")
            )
        )
        return

    # Validate model key
    try:
        new_model = get_model(_catalog, command.model_key)
    except KeyError:
        await _agent.event_bus.broadcast_serialized(
            serialize_event(
                TurnError(
                    turn_index=turn_index,
                    message=f"Unknown model: {command.model_key}",
                )
            )
        )
        return

    # Rebuild LLM client
    new_llm = create_llm_client(new_model, _config.global_.region)

    # Update harness state via public method
    _agent.switch_model(command.model_key, new_model, new_llm)

    await _agent.event_bus.publish(
        ModelSwitch(
            id=str(ULID()),
            model_key=command.model_key,
            sent_at=datetime.now(UTC).isoformat(),
        )
    )

    log.info("Model switched", extra={"model_key": command.model_key, "model_name": new_model.name})


async def stream(websocket: WebSocket) -> None:
    """Bidirectional WebSocket endpoint for event streaming.

    On connect: sends SessionSnapshot + SessionInfo events, adds to broadcast set.
    Receives: message and interrupt commands.
    On disconnect: removes from broadcast set.
    """
    await websocket.accept()

    if _agent is None:
        await websocket.close(code=1013, reason="Agent not ready")
        return

    # Send authoritative session snapshot on connect (replay cursor + accounting)
    acct = session_accounting(_agent.log_path)
    snapshot = SessionSnapshot(
        protocol_version=PROTOCOL_VERSION,
        model=_agent.session.model.name,
        session_id=_agent.session.session_id,
        latest_event_id=acct["latest_event_id"],
        status="ready",
        git_branch=_read_git_branch(),
        total_cost=acct["total_cost"],
        total_input_tokens=acct["total_input_tokens"],
        total_output_tokens=acct["total_output_tokens"],
        total_cache_read_tokens=acct["total_cache_read_tokens"],
        total_cache_write_tokens=acct["total_cache_write_tokens"],
    )
    await websocket.send_text(serialize_event(snapshot))

    # Send session info on connect
    info = SessionInfo(
        protocol_version=PROTOCOL_VERSION,
        model=_agent.session.model.name,
        session_id=_agent.session.session_id,
        git_branch=_read_git_branch(),
    )
    await websocket.send_text(serialize_event(info))

    # Register for ordered delivery after the transitional connect frames.
    _agent.event_bus.add_client(websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                command = deserialize_command(raw)
            except (ValueError, KeyError) as e:
                log.warning("Malformed WS message: %s", e)
                continue

            if isinstance(command, MessageCommand):
                # Store task reference to prevent garbage collection.
                # Task removes itself from the set on completion.
                task = asyncio.create_task(_agent.handle_message(command.content))
                task.add_done_callback(lambda t: _active_tasks.discard(t))
                _active_tasks.add(task)
            elif isinstance(command, InterruptCommand):
                _agent.interrupt(command.target)
            elif isinstance(command, SwitchModelCommand):
                await _handle_model_switch(command, websocket)

    except WebSocketDisconnect:
        pass
    finally:
        _agent.event_bus.discard_client(websocket)


async def shell_log(request: Request) -> JSONResponse:
    """Log a direct shell command (! prefix) to the session JSONL.

    Accepts JSON: {command: str, exit_code: int, output: str}.
    Writes a MessageEntry with role="shell".
    """
    if _agent is None:
        return JSONResponse({"error": "no session"}, status_code=503)

    try:
        body = await request.json()
        content = json.dumps(
            {
                "command": body.get("command", ""),
                "exit_code": body.get("exit_code", 0),
                "output": body.get("output", ""),
            },
            ensure_ascii=False,
        )
        entry = MessageEntry(
            id=str(ULID()),
            when=datetime.now(UTC).isoformat(),
            role="shell",
            content=content,
        )
        write_entry(_agent.log_path, entry)
        return JSONResponse({"ok": True})
    except Exception as e:
        log.warning("Failed to log shell command", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


app = Starlette(
    routes=[
        Route("/status", status, methods=["GET"]),
        Route("/events", events, methods=["GET"]),
        Route("/shell", shell_log, methods=["POST"]),
        WebSocketRoute("/stream", stream),
    ],
    lifespan=lifespan,
)

"""Archie agent — Starlette application with WebSocket and HTTP endpoints.

Endpoints:
- GET /status — health check + session metadata
- GET /history — conversation history for client catch-up
- WS /stream — bidirectional event streaming
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from archie_shared.config import load_config
from archie_shared.events import (
    PROTOCOL_VERSION,
    InterruptCommand,
    MessageCommand,
    SessionInfo,
    deserialize_command,
    serialize_event,
)
from archie_shared.models import get_model_info
from archie_shared.types import TextBlock, ToolResultBlock, ToolUseBlock
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from archie_agent.agent import AgentLoop
from archie_agent.llm.bedrock import BedrockClient
from archie_agent.session import Session

log = logging.getLogger(__name__)

# Module-level agent reference, set during lifespan
_agent: AgentLoop | None = None

# Strong references to active tasks (prevents GC of fire-and-forget coroutines)
_active_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app):
    """Initialize the agent loop on startup."""
    global _agent

    config = load_config()
    model_info = get_model_info(config.model)

    # Determine target region (model-specific or session default)
    region = model_info.region or config.region

    llm_client = BedrockClient(
        model_id=config.model,
        region=region,
        max_output_tokens=model_info.max_output_tokens,
    )

    session_id = os.environ.get("ARCHIE_SESSION_ID", "unknown")
    sessions_dir = Path(os.environ.get("ARCHIE_SESSIONS_DIR", "/archie/sessions"))

    session = Session(
        model_id=config.model,
        model_info=model_info,
        session_id=session_id,
        _log_dir=sessions_dir,
    )

    # Minimal system prompt for v1
    system_prompt = (
        f"You are Archie, a helpful AI assistant.\nModel: {model_info.name}\nBe concise and direct."
    )

    _agent = AgentLoop(
        session=session,
        llm_client=llm_client,
        model_info=model_info,
        system_prompt=system_prompt,
    )

    log.info(
        "Agent started", extra={"model": config.model, "region": region, "session": session_id}
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


async def history(request: Request) -> JSONResponse:
    """Return conversation history for client catch-up.

    Uses content-block array format, forward-compatible with tool blocks.
    Each turn includes turn_index for client reconciliation.
    """
    if _agent is None:
        return JSONResponse([], status_code=503)

    turns = []
    for turn in _agent.session.turns:
        content_blocks = []
        for block in turn.content:
            match block:
                case TextBlock(text=text):
                    content_blocks.append({"type": "text", "text": text})
                case ToolUseBlock(tool_use_id=tid, name=name, input=inp):
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "tool_use_id": tid,
                            "name": name,
                            "input": inp,
                        }
                    )
                case ToolResultBlock(tool_use_id=tid, content=content, is_error=is_error):
                    content_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tid,
                            "content": content,
                            "is_error": is_error,
                        }
                    )
        turns.append(
            {
                "turn_index": turn.turn_index,
                "role": turn.role,
                "content": content_blocks,
            }
        )

    return JSONResponse(turns)


async def stream(websocket: WebSocket) -> None:
    """Bidirectional WebSocket endpoint for event streaming.

    On connect: sends SessionInfo event, adds to broadcast set.
    Receives: message and interrupt commands.
    On disconnect: removes from broadcast set.
    """
    await websocket.accept()

    if _agent is None:
        await websocket.close(code=1013, reason="Agent not ready")
        return

    # Send session info on connect
    info = SessionInfo(
        protocol_version=PROTOCOL_VERSION,
        model=_agent.session.model_id,
        session_id=_agent.session.session_id,
    )
    await websocket.send_text(serialize_event(info))

    # Register for broadcast
    _agent.clients.add(websocket)

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
                _agent.interrupt()

    except WebSocketDisconnect:
        pass
    finally:
        _agent.clients.discard(websocket)


app = Starlette(
    routes=[
        Route("/status", status, methods=["GET"]),
        Route("/history", history, methods=["GET"]),
        WebSocketRoute("/stream", stream),
    ],
    lifespan=lifespan,
)

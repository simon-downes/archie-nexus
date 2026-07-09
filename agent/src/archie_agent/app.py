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

from archie_shared.config import home_dir
from archie_shared.events import (
    PROTOCOL_VERSION,
    InterruptCommand,
    MessageCommand,
    SessionInfo,
    deserialize_command,
    serialize_event,
)
from archie_shared.models import get_model, load_models
from archie_shared.schemas import load_nexus_config
from archie_shared.types import TextBlock, ToolResultBlock, ToolUseBlock
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from archie_agent.harness import AgentHarness
from archie_agent.llm.bedrock import BedrockClient
from archie_agent.session import Session

log = logging.getLogger(__name__)

# Module-level agent reference, set during lifespan
_agent: AgentHarness | None = None

# Strong references to active tasks (prevents GC of fire-and-forget coroutines)
_active_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app):
    """Initialize the agent loop on startup."""
    global _agent

    config = load_nexus_config()
    catalog = load_models(home_dir() / "models.yaml")
    model = get_model(catalog, config.global_.model)

    # Determine target region (model-specific or session default)
    region = model.provider.region or config.global_.region

    llm_client = BedrockClient(
        model_id=model.provider.endpoint,
        region=region,
        max_output_tokens=model.max_output_tokens,
        can_cache=model.can_cache,
    )

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
        model_id=config.global_.model,
        model=model,
        session_id=session_id,
    )

    # System prompt — concise instructions; detailed tool docs are in the tool description
    system_prompt = (
        f"You are Archie, a helpful AI assistant.\nModel: {model.name}\n\n"
        "You have access to an `exec` tool that runs Python code inside your container. "
        "Use it to inspect and modify the /workspace project. The exec environment provides "
        "async functions: read, write, edit, grep, glob, shell. Define `async def main()` "
        "and return results.\n\n"
        "Be concise and direct. Use tools proactively to answer questions."
    )

    _agent = AgentHarness(
        session=session,
        llm_client=llm_client,
        system_prompt=system_prompt,
        log_dir=sessions_dir,
    )

    log.info(
        "Agent started",
        extra={"model": config.global_.model, "region": region, "session": session_id},
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
    Includes error/interrupted entries for TUI replay.
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

    # Append display entries (errors, interruptions) for TUI replay
    for entry in _agent.session.display_entries:
        turns.append(
            {
                "turn_index": entry.turn_index,
                "role": entry.role,
                "content": [{"type": "text", "text": entry.content}] if entry.content else [],
            }
        )

    # Sort by turn_index so errors appear in correct position
    turns.sort(key=lambda t: t["turn_index"])

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
        model=_agent.session.model.name,
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

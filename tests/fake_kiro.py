"""Scripted ACP agent standing in for ``kiro-cli acp`` in tests (077 M2).

Run with ``sys.executable tests/fake_kiro.py``. Behaviour:

- ``FAKE_KIRO_RECORD``: JSONL file; the fake appends ``{"env": {...}}`` at start and
  every request or response it receives.
- ``FAKE_KIRO_HOME``: Kiro home holding ``agents/archie.json`` (for the MCP URL).
- ``FAKE_KIRO_SCENARIO``: startup variant (``ok`` by default).
- The prompt text picks the turn: ``error`` → JSON-RPC error; ``exit`` → exit 3
  mid-turn; ``sleep`` → MCP ``shell sleep 30`` until ``session/cancel``;
  ``permission`` → ask permission; anything else → text, ``tool_load``, MCP
  ``shell echo KIRO-OK``, text.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

RECORD = os.environ.get("FAKE_KIRO_RECORD")
HOME = Path(os.environ.get("FAKE_KIRO_HOME", str(Path.home() / ".kiro")))
SCENARIO = os.environ.get("FAKE_KIRO_SCENARIO", "ok")
SESSION = "sess_fake"
MODELS = ["auto", "claude-opus-5.5", "claude-sonnet-5.5", "gpt-5.6-luna", "gpt-5.6-sol"]

state = {"contentCollection": "enabled", "autopilot": "on", "model": "auto", "mode": "archie"}
cancelled = asyncio.Event()
pending: dict[int, asyncio.Future] = {}
next_id = 1000


def record(entry: dict) -> None:
    if RECORD:
        with open(RECORD, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


def send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def update(payload: dict) -> None:
    send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": SESSION, "update": payload},
        }
    )


def options() -> list[dict]:
    model_opts = [
        {
            "value": m,
            "name": m,
            "_meta": {"kiro": {"effortLevels": ["low", "medium", "high"] if "claude" in m else []}},
        }
        for m in MODELS
    ]
    return [
        {
            "type": "select",
            "id": "mode",
            "category": "mode",
            "currentValue": state["mode"],
            "options": [{"value": "vibe"}, {"value": "archie"}, {"value": "archie-worker"}],
        },
        {
            "type": "select",
            "id": "model",
            "category": "model",
            "currentValue": state["model"],
            "options": model_opts,
        },
        {
            "type": "select",
            "id": "autopilot",
            "currentValue": state["autopilot"],
            "options": [{"value": "on"}, {"value": "off"}],
        },
        {
            "type": "select",
            "id": "contentCollection",
            "currentValue": state["contentCollection"],
            "options": [{"value": "enabled"}, {"value": "disabled"}],
        },
    ]


def mcp_url() -> str:
    agent = json.loads((HOME / "agents" / "archie.json").read_text())
    return agent["mcpServers"]["archie"]["url"]


async def call_mcp(name: str, arguments: dict) -> dict:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with (
        streamable_http_client(mcp_url()) as (read, write, _),
        ClientSession(read, write) as s,
    ):
        await s.initialize()
        result = await s.call_tool(name, arguments)
    text = "".join(c.text for c in result.content if getattr(c, "type", "") == "text")
    images = [
        f"data:{c.mimeType};base64,{c.data}"
        for c in result.content
        if getattr(c, "type", "") == "image"
    ]
    return {"text": text, "isError": result.isError, "images": images}


def chunk(text: str, meta: dict | None = None) -> None:
    payload = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
    if meta:
        payload["_meta"] = {"kiro": meta}
    update(payload)


def completion(credits: float, status: str = "success") -> None:
    update(
        {
            "sessionUpdate": "session_info_update",
            "_meta": {
                "kiro": {
                    "kind": "turn_completion",
                    "status": status,
                    "elapsedTime": 1234,
                    "promptTurnSummaries": [
                        {"unit": "credit", "unitPlural": "credits", "usage": credits}
                    ],
                }
            },
        }
    )


async def archie_tool(call_id: str, name: str, arguments: dict) -> dict:
    raw = {"tool_id": f"archie::{name}", "arguments": arguments}
    meta = {"_meta": {"kiro": {"serverName": "archie", "toolOrigin": "default"}}}
    update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": call_id,
            "title": f"@archie/{name}",
            "kind": "other",
            "status": "pending",
            "rawInput": raw,
            **meta,
        }
    )
    update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": call_id,
            "status": "in_progress",
            "rawInput": raw,
            **meta,
        }
    )
    result = await call_mcp(name, arguments)
    update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": call_id,
            "status": "failed" if result["isError"] else "completed",
            "title": f"@archie/{name}",
            "rawInput": raw,
            "rawOutput": {
                "response": result["text"],
                "imageBase64Urls": result["images"],
                "message": result["text"],
            },
            "content": [{"type": "content", "content": {"type": "text", "text": result["text"]}}],
            **meta,
        }
    )
    return result


async def ask_permission() -> dict:
    global next_id
    next_id += 1
    fut = asyncio.get_running_loop().create_future()
    pending[next_id] = fut
    send(
        {
            "jsonrpc": "2.0",
            "id": next_id,
            "method": "session/request_permission",
            "params": {
                "sessionId": SESSION,
                "toolCall": {
                    "toolCallId": "perm1",
                    "status": "pending",
                    "title": "Run builtin shell",
                },
                "options": [{"optionId": "accept", "kind": "allow_once"}],
            },
        }
    )
    return await fut


async def prompt(rid: int, params: dict) -> None:
    text = params["prompt"][0]["text"]
    cancelled.clear()
    if text == "error":
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "model exploded"}})
        return
    if text == "exit":
        chunk("Starting")
        await asyncio.sleep(0.1)
        os._exit(3)
    if text == "sleep":
        chunk("Sleeping.")
        task = asyncio.create_task(archie_tool("tc_sleep", "shell", {"command": "sleep 30"}))
        await cancelled.wait()
        completion(0.25, "aborted")
        update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc_sleep",
                "status": "failed",
                "rawOutput": "This tool was interrupted before it reported a result.",
            }
        )
        await asyncio.wait({task}, timeout=5)
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "cancelled"}})
        return
    if text == "permission":
        response = await ask_permission()
        record({"permission_response": response})
        chunk("Asked.")
        completion(0.1)
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        return
    chunk("Running ")
    chunk("the command.")
    update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc_load",
            "status": "completed",
            "title": "Tool Load",
            "rawInput": {"tool_ids": ["archie::shell"]},
            "rawOutput": {"message": "loaded"},
        }
    )
    await archie_tool("tc_shell", "shell", {"command": "echo KIRO-OK"})
    chunk("Done: KIRO-OK.")
    completion(0.5)
    send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})


async def session_new(rid: int, params: dict) -> None:
    state["mode"] = (params.get("_meta") or {}).get("kiro", {}).get("modeId", "vibe")
    send(
        {
            "jsonrpc": "2.0",
            "method": "_kiro/mcp/status",
            "params": {
                "sessionId": SESSION,
                "servers": [{"name": "archie", "status": "connecting"}],
            },
        }
    )
    await asyncio.sleep(0.01)
    send(
        {
            "jsonrpc": "2.0",
            "method": "_kiro/mcp/status",
            "params": {
                "sessionId": SESSION,
                "servers": [{"name": "archie", "status": "connected"}],
            },
        }
    )
    send(
        {
            "jsonrpc": "2.0",
            "method": "_kiro/tools/didChange",
            "params": {
                "sessionId": SESSION,
                "tags": [
                    {"source": "builtin", "tag": "read"},
                    {"source": "mcp", "tag": "@archie/shell"},
                ],
            },
        }
    )
    send(
        {"jsonrpc": "2.0", "id": rid, "result": {"sessionId": SESSION, "configOptions": options()}}
    )


async def handle(msg: dict) -> None:
    rid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1}})
    elif method == "session/new":
        await session_new(rid, params)
    elif method == "session/set_config_option":
        if not (SCENARIO == "sticky_collection" and params["configId"] == "contentCollection"):
            state[params["configId"]] = params["value"]
        send({"jsonrpc": "2.0", "id": rid, "result": {"configOptions": options()}})
    elif method == "session/prompt":
        asyncio.create_task(prompt(rid, params))
    elif method == "session/cancel":
        cancelled.set()
    elif rid is not None and method is None:
        fut = pending.pop(rid, None)
        if fut is not None and not fut.done():
            fut.set_result(msg.get("result"))
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": method}})


async def main() -> None:
    record({"env": {"KIRO_API_KEY": os.environ.get("KIRO_API_KEY"), "argv": sys.argv[1:]}})
    print("(node:1) ExperimentalWarning: SQLite is an experimental feature", file=sys.stderr)
    print("[INFO] routine progress", file=sys.stderr)
    print("fake kiro: started", file=sys.stderr, flush=True)
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=1 << 24)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while line := await reader.readline():
        msg = json.loads(line)
        record({"in": msg})
        await handle(msg)


if __name__ == "__main__":
    asyncio.run(main())

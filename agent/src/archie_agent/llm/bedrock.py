"""AWS Bedrock converse_stream wrapper.

Handles all communication with AWS Bedrock. Wraps the low-level boto3 EventStream
API into a simple generator that yields typed Python objects.

Key design decisions:
- Synchronous generator. The agent loop runs it in a background thread via
  asyncio.to_thread, keeping the async event loop free.
- EventStream is explicitly closed in a finally block. Unlike nextgen (short-lived
  process), nexus is a long-lived server — abandoned streams leak HTTP connections.
- Translates internal Turn objects to Bedrock's wire format. No Bedrock-specific
  types leak out to the rest of the application.
"""

import json
import logging
import time
from collections.abc import Generator
from typing import Any

import boto3
from archie_shared.types import TextBlock, ToolResultBlock, ToolUseBlock
from botocore.config import Config

from archie_agent.llm._types import Done, StreamEvent, TextDelta, ToolUseEvent, ToolUseStart, Usage
from archie_agent.session import Turn

log = logging.getLogger(__name__)


def _turns_to_bedrock_messages(turns: list[Turn]) -> list[dict]:
    """Translate internal Turn objects to Bedrock's message format."""
    messages = []
    for turn in turns:
        content_blocks: list[dict[str, Any]] = []
        for block in turn.content:
            match block:
                case TextBlock(text=text):
                    content_blocks.append({"text": text})
                case ToolUseBlock(tool_use_id=tid, name=name, input=inp):
                    content_blocks.append(
                        {"toolUse": {"toolUseId": tid, "name": name, "input": inp}}
                    )
                case ToolResultBlock(tool_use_id=tid, content=content, is_error=is_error):
                    content_blocks.append(
                        {
                            "toolResult": {
                                "toolUseId": tid,
                                "content": [{"text": content}],
                                "status": "error" if is_error else "success",
                            }
                        }
                    )
        messages.append({"role": turn.role, "content": content_blocks})
    return messages


class BedrockClient:
    """Wrapper around Bedrock's converse_stream API.

    Handles:
    - Translating internal types to Bedrock wire format
    - Parsing the EventStream into typed events
    - Prompt cache point placement (system + history tail)
    - Retrying on throttling (exponential backoff)
    - Explicit stream close to prevent connection leaks
    """

    def __init__(self, model_id: str, region: str, max_output_tokens: int = 32_768):
        self.model_id = model_id
        self._region = region
        self.max_output_tokens = max_output_tokens
        self.client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(read_timeout=300, retries={"max_attempts": 0}),
        )
        self._cache_supported: bool = True

    def stream(
        self,
        messages: list[Turn],
        system: str,
        tool_config: list[dict] | None = None,
    ) -> Generator[StreamEvent]:
        """Send a conversation to Bedrock and yield response events.

        The EventStream is explicitly closed in a finally block to prevent
        connection leaks in this long-lived server process.
        """
        bedrock_messages = _turns_to_bedrock_messages(messages)

        # System prompt cache point
        system_blocks: list[dict[str, Any]] = [{"text": system}]
        if self._cache_supported:
            system_blocks.append({"cachePoint": {"type": "default"}})

        # History tail cache point
        if self._cache_supported and bedrock_messages:
            bedrock_messages[-1]["content"].append({"cachePoint": {"type": "default"}})

        params: dict = {
            "modelId": self.model_id,
            "messages": bedrock_messages,
            "system": system_blocks,
            "inferenceConfig": {"maxTokens": self.max_output_tokens},
        }

        if tool_config:
            params["toolConfig"] = {"tools": tool_config}

        t0 = time.time()
        response = self._call_with_retry(params)
        request_id = response.get("ResponseMetadata", {}).get("RequestId", "")
        event_stream = response["stream"]

        current_block_type: str | None = None
        current_tool_use_id: str = ""
        current_tool_name: str = ""
        current_tool_input_json: str = ""
        usage: Usage | None = None
        stop_reason: str = "unknown"

        try:
            for event in event_stream:
                if "contentBlockStart" in event:
                    start = event["contentBlockStart"].get("start", {})
                    if "toolUse" in start:
                        current_block_type = "tool_use"
                        current_tool_use_id = start["toolUse"]["toolUseId"]
                        current_tool_name = start["toolUse"]["name"]
                        current_tool_input_json = ""
                        if current_tool_name:
                            yield ToolUseStart(
                                tool_use_id=current_tool_use_id, name=current_tool_name
                            )
                    else:
                        current_block_type = "text"

                elif "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"]["delta"]
                    if "text" in delta:
                        yield TextDelta(text=delta["text"])
                    elif "toolUse" in delta:
                        current_tool_input_json += delta["toolUse"].get("input", "")

                elif "contentBlockStop" in event:
                    if current_block_type == "tool_use":
                        input_truncated = False
                        try:
                            parsed_input = (
                                json.loads(current_tool_input_json)
                                if current_tool_input_json
                                else {}
                            )
                        except json.JSONDecodeError:
                            log.warning(
                                "Failed to parse tool args JSON for %s (likely max_tokens): %s",
                                current_tool_name,
                                current_tool_input_json[:200],
                            )
                            parsed_input = {}
                            input_truncated = True
                        yield ToolUseEvent(
                            tool_use_id=current_tool_use_id,
                            name=current_tool_name,
                            input=parsed_input,
                            input_truncated=input_truncated,
                        )
                    current_block_type = None

                elif "metadata" in event:
                    raw = event["metadata"].get("usage", {})
                    usage = Usage(
                        input_tokens=raw.get("inputTokens", 0),
                        output_tokens=raw.get("outputTokens", 0),
                        cache_read_input_tokens=raw.get("cacheReadInputTokens", 0),
                        cache_write_input_tokens=raw.get("cacheWriteInputTokens", 0),
                    )
                    yield usage

                elif "messageStop" in event:
                    stop_reason = event["messageStop"].get("stopReason", "end_turn")
                    yield Done(stop_reason=stop_reason)
        finally:
            # Explicitly close the EventStream to release the HTTP connection.
            # Critical in a long-lived server — GC alone is not reliable.
            try:
                event_stream.close()
            except Exception:
                pass

        log.info(
            "Bedrock request completed",
            extra={
                "model": self.model_id,
                "duration_s": round(time.time() - t0, 2),
                "stop_reason": stop_reason,
                "input": usage.input_tokens if usage else 0,
                "output": usage.output_tokens if usage else 0,
                "cache_read": usage.cache_read_input_tokens if usage else 0,
                "cache_write": usage.cache_write_input_tokens if usage else 0,
                "aws_request_id": request_id,
            },
        )

    def invoke(self, messages: list[Turn], system: str) -> str:
        """Non-streaming call. Returns the response text."""
        params = {
            "modelId": self.model_id,
            "messages": _turns_to_bedrock_messages(messages),
            "system": [{"text": system}],
        }
        for attempt in range(3):
            try:
                response = self.client.converse(**params)
                break
            except self.client.exceptions.ThrottlingException:
                if attempt == 2:
                    raise
                delay = 2**attempt
                log.warning("Throttled by Bedrock (invoke), retrying in %ds", delay)
                time.sleep(delay)

        output = response.get("output", {}).get("message", {}).get("content", [])
        return "".join(block.get("text", "") for block in output)

    def _call_with_retry(self, params: dict, max_retries: int = 3) -> dict:
        """Call converse_stream with retry on throttling.

        If cachePoint is rejected, retry without it and disable caching.
        """
        for attempt in range(max_retries):
            try:
                return self.client.converse_stream(**params)
            except self.client.exceptions.ThrottlingException:
                if attempt == max_retries - 1:
                    raise
                delay = 2**attempt
                log.warning("Throttled by Bedrock, retrying in %ds", delay)
                time.sleep(delay)
            except (
                self.client.exceptions.ValidationException,
                self.client.exceptions.AccessDeniedException,
            ) as e:
                msg_text = str(e)
                if self._cache_supported and (
                    "cachePoint" in msg_text or "prompt caching" in msg_text
                ):
                    log.warning("cachePoint not supported, disabling prompt caching")
                    self._cache_supported = False
                    params["system"] = [
                        b for b in params.get("system", []) if "cachePoint" not in b
                    ]
                    for msg in params.get("messages", []):
                        msg["content"] = [
                            b for b in msg.get("content", []) if "cachePoint" not in b
                        ]
                    return self.client.converse_stream(**params)
                raise
        raise RuntimeError("Unreachable")

"""AWS Bedrock Converse API client."""

import json
import logging
import time
from collections.abc import Generator
from typing import Any

import boto3
from archie_shared.types import TextBlock, ToolResultBlock, ToolUseBlock
from botocore.config import Config

from archie_agent.llm._types import Done, StreamEvent, TextDelta, ToolUseEvent, ToolUseStart, Usage
from archie_agent.prompt import SystemPrompt, flatten_system_prompt
from archie_agent.session import Turn

log = logging.getLogger(__name__)


def _shape_tool_config_for_bedrock(neutral_config: list[dict]) -> list[dict]:
    """Convert neutral tool configs to Bedrock's toolSpec format."""
    return [
        {
            "toolSpec": {
                "name": item["name"],
                "description": item["description"],
                "inputSchema": {"json": item["input_schema"]},
            }
        }
        for item in neutral_config
    ]


def _turns_to_bedrock_messages(turns: list[Turn]) -> list[dict]:
    """Translate internal Turn objects to Bedrock's message format."""
    messages = []
    for turn in turns:
        content_blocks: list[dict[str, Any]] = []
        for block in turn.content:
            match block:
                case TextBlock(text=text):
                    if text:
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
                                "content": [{"text": content or "empty"}],
                                "status": "error" if is_error else "success",
                            }
                        }
                    )
        if content_blocks:
            messages.append({"role": turn.role, "content": content_blocks})
    return messages


class BedrockClient:
    """Wrapper around Bedrock's converse_stream API."""

    def __init__(
        self, model_id: str, region: str, max_output_tokens: int = 32_768, can_cache: bool = False
    ):
        self.model_id = model_id
        self._region = region
        self.max_output_tokens = max_output_tokens
        self.client = self._create_client(region)
        self._cache_supported: bool = can_cache

    def _create_client(self, region: str):
        """Create a boto3 client using Archie credentials or the default chain."""
        from archie_shared.credentials import get_credential

        cred = get_credential("bedrock")
        kwargs: dict[str, Any] = {
            "region_name": region,
            "config": Config(read_timeout=300, retries={"max_attempts": 0}),
        }
        if cred and cred.aws_access_key_id and cred.aws_secret_access_key:
            kwargs["aws_access_key_id"] = cred.aws_access_key_id
            kwargs["aws_secret_access_key"] = cred.aws_secret_access_key
            if cred.aws_session_token:
                kwargs["aws_session_token"] = cred.aws_session_token
        elif cred:
            log.warning(
                "Bedrock credentials file is missing required keys "
                "(aws_access_key_id, aws_secret_access_key) — falling back to default chain"
            )
        return boto3.client("bedrock-runtime", **kwargs)

    def stream(
        self,
        messages: list[Turn],
        system: SystemPrompt | str,
        tool_config: list[dict] | None = None,
        history_boundary: str | None = None,
    ) -> Generator[StreamEvent]:
        """Send a conversation to Bedrock and yield response events."""
        del history_boundary
        bedrock_messages = _turns_to_bedrock_messages(messages)
        system_blocks: list[dict[str, Any]] = [{"text": flatten_system_prompt(system)}]
        if self._cache_supported:
            system_blocks.append({"cachePoint": {"type": "default"}})
        if self._cache_supported and bedrock_messages:
            bedrock_messages[-1]["content"].append({"cachePoint": {"type": "default"}})

        params: dict = {
            "modelId": self.model_id,
            "messages": bedrock_messages,
            "system": system_blocks,
            "inferenceConfig": {"maxTokens": self.max_output_tokens},
        }
        if tool_config:
            params["toolConfig"] = {"tools": _shape_tool_config_for_bedrock(tool_config)}

        t0 = time.time()
        response = self._call_with_retry(params)
        request_id = response.get("ResponseMetadata", {}).get("RequestId", "")
        event_stream = response["stream"]
        current_block_type: str | None = None
        current_tool_use_id = ""
        current_tool_name = ""
        current_tool_input_json = ""
        usage: Usage | None = None
        stop_reason = "unknown"

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
                            parsed_input = json.loads(current_tool_input_json) if current_tool_input_json else {}
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
                    # Converse inputTokens is already the uncached portion. It
                    # is valid for a cache-read prefix to exceed that suffix;
                    # unlike Responses, there is no raw total to subtract from.
                    usage = Usage(
                        input_tokens=raw.get("inputTokens", 0),
                        output_tokens=raw.get("outputTokens", 0),
                        cache_read_tokens=raw.get("cacheReadInputTokens", 0),
                        cache_write_tokens=raw.get("cacheWriteInputTokens", 0),
                    )
                    yield usage
                elif "messageStop" in event:
                    stop_reason = event["messageStop"].get("stopReason", "end_turn")
                    yield Done(stop_reason=stop_reason)
        finally:
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
                "cache_read": usage.cache_read_tokens if usage else 0,
                "cache_write": usage.cache_write_tokens if usage else 0,
                "aws_request_id": request_id,
            },
        )

    def invoke(
        self,
        messages: list[Turn],
        system: SystemPrompt | str,
        tool_config: list[dict] | None = None,
        history_boundary: str | None = None,
    ) -> str:
        """Non-streaming Converse call."""
        del tool_config, history_boundary
        params = {
            "modelId": self.model_id,
            "messages": _turns_to_bedrock_messages(messages),
            "system": [{"text": flatten_system_prompt(system)}],
        }
        for attempt in range(3):
            try:
                response = self.client.converse(**params)
                break
            except self.client.exceptions.ThrottlingException:
                if attempt == 2:
                    raise
                time.sleep(2**attempt)
        output = response.get("output", {}).get("message", {}).get("content", [])
        return "".join(block.get("text", "") for block in output)

    def _call_with_retry(self, params: dict, max_retries: int = 3) -> dict:
        """Call Converse with existing throttling, auth, and cache fallback behavior."""
        for attempt in range(max_retries):
            try:
                return self.client.converse_stream(**params)
            except self.client.exceptions.ThrottlingException:
                if attempt == max_retries - 1:
                    raise
                time.sleep(2**attempt)
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
                    params["system"] = [b for b in params.get("system", []) if "cachePoint" not in b]
                    for msg in params.get("messages", []):
                        msg["content"] = [b for b in msg.get("content", []) if "cachePoint" not in b]
                    return self.client.converse_stream(**params)
                if self._try_refresh_credentials():
                    return self.client.converse_stream(**params)
                raise
            except Exception as e:
                if "ExpiredToken" in str(e) or "expired" in str(e).lower():
                    if self._try_refresh_credentials():
                        return self.client.converse_stream(**params)
                raise
        raise RuntimeError("Unreachable")

    def _try_refresh_credentials(self) -> bool:
        """Re-read credentials and recreate the boto3 client."""
        from archie_shared.credentials import get_credential

        cred = get_credential("bedrock")
        if not cred or not cred.aws_access_key_id:
            return False
        self.client = self._create_client(self._region)
        return True

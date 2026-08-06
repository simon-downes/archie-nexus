"""OpenAI Responses API client for OpenAI models hosted on Amazon Bedrock.

GPT-5.6 Luna is exposed through Bedrock's ``bedrock-mantle`` endpoint rather
than the ``bedrock-runtime`` Converse API used by :mod:`bedrock`.  The OpenAI
SDK's Bedrock client generates a short-lived bearer token from the same AWS
credentials used by the existing Bedrock client.
"""

import json
import logging
import time
from collections.abc import Generator
from typing import Any

from archie_shared.types import TextBlock, ToolResultBlock, ToolUseBlock
from aws_bedrock_token_generator import provide_token
from botocore.credentials import CredentialProvider, Credentials
from botocore.session import Session
from openai import BedrockOpenAI
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFailedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseFunctionToolCall,
    ResponseIncompleteEvent,
    ResponseOutputItemAddedEvent,
    ResponseTextDeltaEvent,
)

from archie_agent.llm._types import Done, StreamEvent, TextDelta, ToolUseEvent, ToolUseStart, Usage
from archie_agent.session import Turn

log = logging.getLogger(__name__)


class _StoredBedrockCredentialProvider(CredentialProvider):
    """Read Archie-managed credentials, falling back to the AWS chain."""

    METHOD = "archie"

    def load(self) -> Credentials | None:
        from archie_shared.credentials import get_credential

        credential = get_credential("bedrock")
        if credential and credential.aws_access_key_id and credential.aws_secret_access_key:
            return Credentials(
                access_key=credential.aws_access_key_id,
                secret_key=credential.aws_secret_access_key,
                token=credential.aws_session_token,
            )
        return Session().get_credentials()


def _turns_to_responses_input(turns: list[Turn]) -> list[dict[str, Any]]:
    """Translate provider-neutral turns to Responses API input items."""
    items: list[dict[str, Any]] = []
    for turn in turns:
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []

        for block in turn.content:
            match block:
                case TextBlock(text=text):
                    text_parts.append(text)
                case ToolUseBlock(tool_use_id=tool_use_id, name=name, input=input_data):
                    tool_calls.append(
                        {
                            "type": "function_call",
                            "call_id": tool_use_id,
                            "name": name,
                            "arguments": json.dumps(input_data),
                        }
                    )
                case ToolResultBlock(tool_use_id=tool_use_id, content=content, is_error=is_error):
                    output = content
                    if is_error:
                        output = f"Tool error: {output}"
                    tool_results.append(
                        {
                            "type": "function_call_output",
                            "call_id": tool_use_id,
                            "output": output,
                        }
                    )

        if text_parts:
            items.append({"role": turn.role, "content": "".join(text_parts)})
        items.extend(tool_calls)
        items.extend(tool_results)

    return items


def _tool_config_to_responses(tool_config: list[dict]) -> list[dict[str, Any]]:
    """Translate neutral tool configs to Responses API function tools."""
    return [
        {
            "type": "function",
            "name": item["name"],
            "description": item.get("description", ""),
            "parameters": item["input_schema"],
            "strict": False,
        }
        for item in tool_config
    ]


def _response_usage(response: Response) -> Usage:
    """Extract Responses API usage, including cached input tokens.

    Field access is direct so a rename or removal in the OpenAI SDK raises
    loudly instead of silently billing zero tokens.  ``input_tokens_details``
    is optional on the wire, so its sub-fields are only read when present.
    """
    raw_usage = response.usage
    if raw_usage is None:
        log.warning("Responses API returned no usage for model %s", response.model)
        return Usage(input_tokens=0, output_tokens=0)

    input_details = raw_usage.input_tokens_details
    cached_tokens = input_details.cached_tokens if input_details else 0
    cache_write_tokens = input_details.cache_write_tokens if input_details else 0
    return Usage(
        input_tokens=raw_usage.input_tokens,
        output_tokens=raw_usage.output_tokens,
        cache_read_input_tokens=cached_tokens or 0,
        cache_write_input_tokens=cache_write_tokens or 0,
    )


class BedrockOpenAIClient:
    """LLM client for Bedrock models exposed through the Responses API."""

    def __init__(
        self,
        model_id: str,
        region: str,
        max_output_tokens: int = 32_768,
        can_cache: bool = False,
    ):
        self.model_id = model_id
        self._region = region
        self.max_output_tokens = max_output_tokens
        self.can_cache = can_cache
        self.client = BedrockOpenAI(
            aws_region=region,
            bedrock_token_provider=lambda: provide_token(
                region=region,
                aws_credentials_provider=_StoredBedrockCredentialProvider(),
            ),
            max_retries=2,
        )

    def stream(
        self,
        messages: list[Turn],
        system: str,
        tool_config: list[dict] | None = None,
    ) -> Generator[StreamEvent]:
        """Stream text, client-side tool calls, usage, and completion events."""
        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "input": _turns_to_responses_input(messages),
            "instructions": system,
            "max_output_tokens": self.max_output_tokens,
            "store": False,
            "stream": True,
        }
        if tool_config:
            kwargs["tools"] = _tool_config_to_responses(tool_config)

        t0 = time.time()
        tool_calls_seen = False
        tool_calls: dict[str, dict[str, Any]] = {}
        item_to_call: dict[str, str] = {}
        usage_emitted = False
        done_emitted = False

        response_stream = self.client.responses.create(**kwargs)
        try:
            for event in response_stream:
                if isinstance(event, ResponseTextDeltaEvent):
                    if event.delta:
                        yield TextDelta(text=event.delta)

                elif isinstance(event, ResponseOutputItemAddedEvent):
                    item = event.item
                    if isinstance(item, ResponseFunctionToolCall):
                        call_id = item.call_id
                        if item.id:
                            item_to_call[item.id] = call_id
                        tool_calls[call_id] = {"name": item.name, "arguments": ""}
                        tool_calls_seen = True
                        yield ToolUseStart(tool_use_id=call_id, name=item.name)

                elif isinstance(event, ResponseFunctionCallArgumentsDeltaEvent):
                    call_id = item_to_call.get(event.item_id, event.item_id)
                    if call_id in tool_calls:
                        tool_calls[call_id]["arguments"] += event.delta

                elif isinstance(event, ResponseFunctionCallArgumentsDoneEvent):
                    call_id = item_to_call.get(event.item_id, event.item_id)
                    if call_id not in tool_calls:
                        tool_calls[call_id] = {"name": event.name, "arguments": ""}
                    tool_calls[call_id]["arguments"] = event.arguments
                    raw_arguments = tool_calls[call_id]["arguments"]
                    try:
                        input_data = json.loads(raw_arguments) if raw_arguments else {}
                        input_truncated = False
                    except json.JSONDecodeError:
                        log.warning("Failed to parse Responses tool arguments for %s", call_id)
                        input_data = {}
                        input_truncated = True
                    yield ToolUseEvent(
                        tool_use_id=call_id,
                        name=tool_calls[call_id]["name"],
                        input=input_data,
                        input_truncated=input_truncated,
                    )

                elif isinstance(event, ResponseCompletedEvent | ResponseIncompleteEvent):
                    if not usage_emitted:
                        yield _response_usage(event.response)
                        usage_emitted = True

                    if isinstance(event, ResponseIncompleteEvent):
                        details = event.response.incomplete_details
                        reason = details.reason if details else None
                        stop_reason = (
                            "max_tokens" if reason == "max_output_tokens" else "incomplete"
                        )
                    else:
                        stop_reason = "tool_use" if tool_calls_seen else "end_turn"
                    yield Done(stop_reason=stop_reason)
                    done_emitted = True

                elif isinstance(event, ResponseFailedEvent):
                    error = event.response.error
                    message = error.message if error else "Responses API request failed"
                    raise RuntimeError(message)

        finally:
            response_stream.close()

        if not usage_emitted:
            yield Usage(input_tokens=0, output_tokens=0)
        if not done_emitted:
            yield Done(stop_reason="tool_use" if tool_calls_seen else "end_turn")

        log.info(
            "Bedrock Responses request complete",
            extra={
                "model": self.model_id,
                "region": self._region,
                "duration_s": round(time.time() - t0, 2),
                "tool_calls": len(tool_calls),
            },
        )

    def invoke(self, messages: list[Turn], system: str) -> str:
        """Make a non-streaming Responses API call and return its text."""
        response = self.client.responses.create(
            model=self.model_id,
            input=_turns_to_responses_input(messages),
            instructions=system,
            max_output_tokens=self.max_output_tokens,
            store=False,
        )
        return response.output_text or ""

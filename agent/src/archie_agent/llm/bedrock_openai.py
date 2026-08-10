"""OpenAI Responses API client for GPT-5.6 Luna on Amazon Bedrock."""

import copy
import hashlib
import json
import logging
import re
import time
from collections.abc import Generator
from typing import Any

from archie_shared.models import normalize_responses_usage
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
from archie_agent.prompt import SystemPrompt, flatten_system_prompt
from archie_agent.session import Turn

log = logging.getLogger(__name__)
_CACHE_OPTIONS = {"mode": "explicit", "ttl": "30m"}
_MARKER = {"mode": "explicit"}


class _StoredBedrockCredentialProvider(CredentialProvider):
    """Read Archie-managed credentials, falling back to the AWS chain."""

    METHOD = "archie"

    def load(self) -> Credentials | None:
        from archie_shared.credentials import get_credential

        credential = get_credential("bedrock")
        if credential and credential.aws_access_key_id and credential.aws_secret_access_key:
            return Credentials(credential.aws_access_key_id, credential.aws_secret_access_key, credential.aws_session_token)
        return Session().get_credentials()


def _prompt_sections(system: SystemPrompt | str) -> tuple[str, str | None]:
    if isinstance(system, SystemPrompt):
        dynamic = system.dynamic_system.text if system.dynamic_system else None
        return system.static_system.text, dynamic or None
    return flatten_system_prompt(system), None


def _turns_to_responses_input(turns: list[Turn]) -> list[dict[str, Any]]:
    """Translate neutral turns to typed Responses input items."""
    items: list[dict[str, Any]] = []
    for turn in turns:
        text_parts: list[str] = []
        calls: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for block in turn.content:
            match block:
                case TextBlock(text=text) if text:
                    text_parts.append(text)
                case ToolUseBlock(tool_use_id=tid, name=name, input=inp):
                    calls.append({"type": "function_call", "call_id": tid, "name": name, "arguments": json.dumps(inp)})
                case ToolResultBlock(tool_use_id=tid, content=content, is_error=is_error):
                    output = f"Tool error: {content}" if is_error else content
                    results.append({"type": "function_call_output", "call_id": tid, "output": [{"type": "input_text", "text": output}]})
        if text_parts:
            text = "".join(text_parts)
            if turn.role == "assistant":
                # Responses input messages use plain assistant content for prior
                # model text. input_text is only valid for user/developer input.
                items.append({"role": "assistant", "content": text})
            else:
                items.append({"role": turn.role, "content": [{"type": "input_text", "text": text}]})
        items.extend(calls)
        items.extend(results)
    return items


def _tool_config_to_responses(tool_config: list[dict]) -> list[dict[str, Any]]:
    return [{"type": "function", "name": item["name"], "description": item.get("description", ""), "parameters": item["input_schema"], "strict": False} for item in tool_config]


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _cacheable_static_text(text: str) -> str:
    return re.sub(r"\n*<agents\.md>.*?</agents\.md>\n*", "\n", text, flags=re.DOTALL)


def prompt_cache_key(system: SystemPrompt | str, tool_config: list[dict] | None = None) -> str:
    """Build a stable key from schema, static prompt, and ordered tools."""
    static, _ = _prompt_sections(system)
    payload = {"schema": "v1", "static_system": _cacheable_static_text(static), "tools": _tool_config_to_responses(tool_config or [])}
    digest = hashlib.sha256(_canonical_json(payload).encode()).hexdigest()[:32]
    return f"archie:luna:v1:{digest}"


_prompt_cache_key = prompt_cache_key


def _block_fingerprint(index: int, kind: str, text: str) -> str:
    return hashlib.sha256(_canonical_json({"index": index, "kind": kind, "text": text}).encode()).hexdigest()


def _iter_eligible_blocks(items: list[dict[str, Any]]):
    for index, item in enumerate(items):
        if item.get("type") == "function_call_output":
            for block_index, block in enumerate(item.get("output", [])):
                if block.get("type") == "input_text" and block.get("text"):
                    yield index, block_index, "tool_result", block["text"]
        elif item.get("role") == "user":
            for block_index, block in enumerate(item.get("content", [])):
                if block.get("type") == "input_text" and block.get("text"):
                    yield index, block_index, "user", block["text"]


def _apply_conversation_boundary(items, previous_fingerprint, history_boundary):
    eligible = list(_iter_eligible_blocks(items))
    if not eligible:
        return items, previous_fingerprint
    target = next((item for item in reversed(eligible) if history_boundary is None or item[3] == history_boundary), None)
    if target is None:
        return items, previous_fingerprint
    target_fp = _block_fingerprint(target[0], target[2], target[3])
    old = next((item for item in eligible if previous_fingerprint and _block_fingerprint(item[0], item[2], item[3]) == previous_fingerprint), None)
    if previous_fingerprint and old is None:
        log.warning("Previous Responses conversation cache marker no longer matches history")
    markers = [old] if old is not None and target_fp != previous_fingerprint else []
    markers.append(target)
    for item_index, block_index, _, _ in markers:
        item = items[item_index]
        blocks = item["output"] if item.get("type") == "function_call_output" else item["content"]
        blocks[block_index]["prompt_cache_breakpoint"] = copy.deepcopy(_MARKER)
    return items, target_fp


def _developer_message(system, cache_enabled):
    static, dynamic = _prompt_sections(system)
    first = {"type": "input_text", "text": static}
    if cache_enabled:
        first["prompt_cache_breakpoint"] = copy.deepcopy(_MARKER)
    content = [first]
    if dynamic:
        second = {"type": "input_text", "text": dynamic}
        if cache_enabled:
            second["prompt_cache_breakpoint"] = copy.deepcopy(_MARKER)
        content.append(second)
    return {"type": "message", "role": "developer", "content": content}


def _remove_cache_metadata(kwargs):
    fallback = copy.deepcopy(kwargs)
    fallback.pop("prompt_cache_key", None)
    extra = fallback.get("extra_body")
    if isinstance(extra, dict):
        extra.pop("prompt_cache_options", None)
        if not extra:
            fallback.pop("extra_body", None)
    for item in fallback.get("input", []):
        blocks = item.get("output", []) if item.get("type") == "function_call_output" else item.get("content", [])
        for block in blocks:
            block.pop("prompt_cache_breakpoint", None)
    return fallback


def _is_cache_validation_error(error: Exception) -> bool:
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None) or getattr(response, "status_code", None)
    return isinstance(status, int) and 400 <= status < 500 and any(word in str(error).lower() for word in ("cache", "breakpoint", "prompt_cache"))


def _response_usage(response: Response) -> Usage:
    raw = response.usage
    if raw is None:
        log.warning("Responses API returned no usage for model %s", response.model)
        return Usage(input_tokens=0, output_tokens=0)
    details = raw.input_tokens_details
    input_tokens, output_tokens, cache_read, cache_write = normalize_responses_usage(
        raw.input_tokens, raw.output_tokens, details.cached_tokens if details else 0, details.cache_write_tokens if details else 0
    )
    return Usage(input_tokens, output_tokens, cache_read, cache_write)


class BedrockOpenAIClient:
    """LLM client for Bedrock models exposed through the Responses API."""

    def __init__(self, model_id, region, max_output_tokens=32_768, can_cache=False):
        self.model_id = model_id
        self._region = region
        self.max_output_tokens = max_output_tokens
        self.can_cache = can_cache
        self._cache_enabled = can_cache
        self._previous_conversation_fingerprint = None
        self.client = BedrockOpenAI(aws_region=region, bedrock_token_provider=lambda: provide_token(region=region, aws_credentials_provider=_StoredBedrockCredentialProvider()), max_retries=2)

    def _request_kwargs(self, messages, system, tool_config, stream, history_boundary):
        cache_enabled = getattr(self, "_cache_enabled", getattr(self, "can_cache", False))
        previous = getattr(self, "_previous_conversation_fingerprint", None)
        items = _turns_to_responses_input(messages)
        if cache_enabled:
            items, fingerprint = _apply_conversation_boundary(items, previous, history_boundary)
            if fingerprint is not None:
                self._previous_conversation_fingerprint = fingerprint
        kwargs = {"model": self.model_id, "input": [_developer_message(system, cache_enabled), *items], "max_output_tokens": self.max_output_tokens, "store": False}
        if stream:
            kwargs["stream"] = True
        if tool_config:
            kwargs["tools"] = _tool_config_to_responses(tool_config)
        if cache_enabled:
            kwargs["prompt_cache_key"] = prompt_cache_key(system, tool_config)
            kwargs["extra_body"] = {"prompt_cache_options": copy.deepcopy(_CACHE_OPTIONS)}
        return kwargs

    def _create_with_cache_fallback(self, kwargs):
        try:
            return self.client.responses.create(**kwargs)
        except Exception as error:
            enabled = getattr(self, "_cache_enabled", getattr(self, "can_cache", False))
            if not enabled or not _is_cache_validation_error(error):
                raise
            log.warning("Responses cache validation failed; retrying without cache metadata")
            self._cache_enabled = False
            return self.client.responses.create(**_remove_cache_metadata(kwargs))

    def stream(self, messages, system, tool_config=None, history_boundary=None) -> Generator[StreamEvent]:
        kwargs = self._request_kwargs(messages, system, tool_config, True, history_boundary)
        start = time.time()
        calls_seen = False
        calls = {}
        item_calls = {}
        usage_emitted = False
        done_emitted = False
        response_stream = self._create_with_cache_fallback(kwargs)
        try:
            for event in response_stream:
                if isinstance(event, ResponseTextDeltaEvent):
                    if event.delta:
                        yield TextDelta(event.delta)
                elif isinstance(event, ResponseOutputItemAddedEvent) and isinstance(event.item, ResponseFunctionToolCall):
                    item = event.item
                    calls[item.call_id] = {"name": item.name, "arguments": ""}
                    if item.id:
                        item_calls[item.id] = item.call_id
                    calls_seen = True
                    yield ToolUseStart(item.call_id, item.name)
                elif isinstance(event, ResponseFunctionCallArgumentsDeltaEvent):
                    call_id = item_calls.get(event.item_id, event.item_id)
                    if call_id in calls:
                        calls[call_id]["arguments"] += event.delta
                elif isinstance(event, ResponseFunctionCallArgumentsDoneEvent):
                    call_id = item_calls.get(event.item_id, event.item_id)
                    calls.setdefault(call_id, {"name": event.name, "arguments": ""})["arguments"] = event.arguments
                    try:
                        data = json.loads(event.arguments) if event.arguments else {}
                        truncated = False
                    except json.JSONDecodeError:
                        data, truncated = {}, True
                    yield ToolUseEvent(call_id, calls[call_id]["name"], data, truncated)
                elif isinstance(event, ResponseCompletedEvent | ResponseIncompleteEvent):
                    if not usage_emitted:
                        yield _response_usage(event.response)
                        usage_emitted = True
                    if isinstance(event, ResponseIncompleteEvent):
                        detail = event.response.incomplete_details
                        reason = detail.reason if detail else None
                        stop_reason = "max_tokens" if reason == "max_output_tokens" else "incomplete"
                    else:
                        stop_reason = "tool_use" if calls_seen else "end_turn"
                    yield Done(stop_reason)
                    done_emitted = True
                elif isinstance(event, ResponseFailedEvent):
                    error = event.response.error
                    raise RuntimeError(error.message if error else "Responses API request failed")
        finally:
            response_stream.close()
        if not usage_emitted:
            yield Usage(0, 0)
        if not done_emitted:
            yield Done("tool_use" if calls_seen else "end_turn")
        log.info("Bedrock Responses request complete", extra={"model": self.model_id, "region": self._region, "duration_s": round(time.time() - start, 2), "tool_calls": len(calls)})

    def invoke(self, messages, system, tool_config=None, history_boundary=None) -> str:
        response = self._create_with_cache_fallback(self._request_kwargs(messages, system, tool_config, False, history_boundary))
        return response.output_text or ""

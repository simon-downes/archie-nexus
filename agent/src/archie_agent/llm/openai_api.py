"""OpenAI Responses API client for models served directly by api.openai.com.

Request building, cache breakpoints and usage normalisation reuse the helpers in
``bedrock_openai``; only client construction and authentication differ.
"""

import copy
import json
import logging
import os
import time
from collections.abc import Generator

from openai import OpenAI
from openai.types.responses import (
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
from archie_agent.llm.bedrock_openai import (
    _CACHE_OPTIONS,
    _apply_conversation_boundary,
    _developer_message,
    _is_cache_validation_error,
    _remove_cache_metadata,
    _response_usage,
    _tool_config_to_responses,
    _turns_to_responses_input,
    prompt_cache_key,
)

log = logging.getLogger(__name__)

OPENAI_BASE_URL = "https://api.openai.com/v1"
_MISSING_KEY = "No OpenAI API key: run `archie auth login openai` or set OPENAI_API_KEY"


def _api_key() -> str | None:
    """Return the stored ``openai.api_key``, else ``OPENAI_API_KEY``.

    A malformed stored entry raises the store's ``ValueError`` rather than falling back.
    """
    from archie_shared.credentials import get_credential

    credential = get_credential("openai")
    stored = getattr(credential, "api_key", None) if credential else None
    return stored or os.environ.get("OPENAI_API_KEY") or None


class OpenAIClient:
    """LLM client for OpenAI models through the public Responses API."""

    def __init__(self, model_id, max_output_tokens=32_768, can_cache=False, can_see=False):
        self.model_id = model_id
        self.max_output_tokens = max_output_tokens
        self.can_cache = can_cache
        self.can_see = can_see
        self._cache_enabled = can_cache
        self._previous_conversation_fingerprint = None
        self.client: OpenAI | None = None

    def _sdk(self) -> OpenAI:
        """Build the SDK client on the first request that finds a key, then reuse it."""
        if self.client is None:
            key = _api_key()
            if not key:
                raise RuntimeError(_MISSING_KEY)
            self.client = OpenAI(api_key=key, base_url=OPENAI_BASE_URL, max_retries=2)
        return self.client

    def _request_kwargs(self, messages, system, tool_config, stream, history_boundary):
        cache_enabled = self._cache_enabled
        items = _turns_to_responses_input(messages, self.can_see)
        if cache_enabled:
            items, fingerprint = _apply_conversation_boundary(
                items, self._previous_conversation_fingerprint, history_boundary
            )
            if fingerprint is not None:
                self._previous_conversation_fingerprint = fingerprint
        kwargs = {
            "model": self.model_id,
            "input": [_developer_message(system, cache_enabled), *items],
            "max_output_tokens": self.max_output_tokens,
            "store": False,
        }
        if stream:
            kwargs["stream"] = True
        if tool_config:
            kwargs["tools"] = _tool_config_to_responses(tool_config)
        if cache_enabled:
            kwargs["prompt_cache_key"] = prompt_cache_key(system, tool_config)
            kwargs["extra_body"] = {"prompt_cache_options": copy.deepcopy(_CACHE_OPTIONS)}
        return kwargs

    def _create_with_cache_fallback(self, kwargs):
        sdk = self._sdk()
        try:
            return sdk.responses.create(**kwargs)
        except Exception as error:
            if not self._cache_enabled or not _is_cache_validation_error(error):
                raise
            log.warning("Responses cache validation failed; retrying without cache metadata")
            self._cache_enabled = False
            return sdk.responses.create(**_remove_cache_metadata(kwargs))

    def stream(
        self, messages, system, tool_config=None, history_boundary=None
    ) -> Generator[StreamEvent]:
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
                elif isinstance(event, ResponseOutputItemAddedEvent) and isinstance(
                    event.item, ResponseFunctionToolCall
                ):
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
                    calls.setdefault(call_id, {"name": event.name, "arguments": ""})[
                        "arguments"
                    ] = event.arguments
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
                        stop_reason = (
                            "max_tokens" if reason == "max_output_tokens" else "incomplete"
                        )
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
        log.info(
            "OpenAI Responses request complete",
            extra={
                "model": self.model_id,
                "duration_s": round(time.time() - start, 2),
                "tool_calls": len(calls),
            },
        )

    def invoke(self, messages, system, tool_config=None, history_boundary=None) -> str:
        response = self._create_with_cache_fallback(
            self._request_kwargs(messages, system, tool_config, False, history_boundary)
        )
        return response.output_text or ""

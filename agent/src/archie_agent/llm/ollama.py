"""Ollama local model client.

Wraps the ollama Python library to provide the same streaming interface as
BedrockClient. Translates between internal Turn/ContentBlock types and
Ollama's message format.

Key differences from Bedrock:
- System prompt is a message with role "system" (not a separate API field)
- Tool calls use OpenAI function-calling format
- No prompt caching — cache fields are always 0
- Token counts arrive only on the final streamed chunk (done=True)
- No tool_use_id in responses — we generate ULIDs ourselves
- Stop reason uses different vocabulary (mapped to internal equivalents)

Threading: synchronous like BedrockClient. The agent loop calls stream()
from a worker thread; the ollama library uses httpx under the hood.
"""

import logging
import time
from collections.abc import Generator

import httpx
import ollama as _ollama
from archie_shared.types import TextBlock, ToolResultBlock, ToolUseBlock
from ulid import ULID

from archie_agent.llm._types import (
    Done,
    StreamEvent,
    TextDelta,
    ToolUseEvent,
    ToolUseStart,
    Usage,
)
from archie_agent.session import Turn

log = logging.getLogger(__name__)

# Default connection settings
_DEFAULT_HOST = "http://host.docker.internal:11434"
_DEFAULT_TIMEOUT = 240.0


def _turns_to_ollama_messages(turns: list[Turn], system: str) -> list[dict]:
    """Translate internal Turn objects to Ollama's message format.

    Prepends the system prompt as the first message. Maps content blocks:
    - TextBlock → content string
    - ToolUseBlock → assistant message with tool_calls
    - ToolResultBlock → tool role message
    """
    messages: list[dict] = [{"role": "system", "content": system}]

    for turn in turns:
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        tool_results: list[dict] = []

        for block in turn.content:
            match block:
                case TextBlock(text=text):
                    text_parts.append(text)
                case ToolUseBlock(tool_use_id=_, name=name, input=inp):
                    tool_calls.append({"function": {"name": name, "arguments": inp}})
                case ToolResultBlock(tool_use_id=_, content=content, is_error=_):
                    tool_results.append({"role": "tool", "content": content})

        # Assistant turn with tool calls
        if turn.role == "assistant" and tool_calls:
            msg: dict = {"role": "assistant"}
            if text_parts:
                msg["content"] = "".join(text_parts)
            msg["tool_calls"] = [{"function": tc["function"]} for tc in tool_calls]
            messages.append(msg)
        elif tool_results:
            # Tool result messages (user turn containing ToolResultBlocks)
            for tr in tool_results:
                messages.append(tr)
        elif text_parts:
            messages.append({"role": turn.role, "content": "".join(text_parts)})

    return messages


def _tool_config_to_ollama(tool_config: list[dict]) -> list[dict]:
    """Translate neutral tool config to Ollama/OpenAI function format.

    Input:  [{"name": ..., "description": ..., "input_schema": {...}}]
    Output: [{"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}]
    """
    tools = []
    for tc in tool_config:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    "description": tc.get("description", ""),
                    "parameters": tc.get("input_schema", {}),
                },
            }
        )
    return tools


class OllamaClient:
    """Ollama local model client implementing the LLMClient protocol.

    Connects to a running Ollama instance and provides streaming chat with
    tool-calling support. Token usage is reported when available.
    """

    def __init__(
        self,
        model_id: str,
        host: str = _DEFAULT_HOST,
        max_context_tokens: int = 128_000,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self.model_id = model_id
        self._host = host
        self._max_context_tokens = max_context_tokens
        self.client = _ollama.Client(host=host, timeout=httpx.Timeout(timeout))

    def stream(
        self,
        messages: list[Turn],
        system: str,
        tool_config: list[dict] | None = None,
    ) -> Generator[StreamEvent]:
        """Stream a response from the Ollama model.

        Yields TextDelta for incremental text, ToolUseStart/ToolUseEvent for
        tool calls, Usage for token counts, and Done when generation completes.
        """
        ollama_messages = _turns_to_ollama_messages(messages, system)
        tools = _tool_config_to_ollama(tool_config) if tool_config else None

        kwargs: dict = {
            "model": self.model_id,
            "messages": ollama_messages,
            "stream": True,
            "options": {"num_ctx": self._max_context_tokens},
        }
        if tools:
            kwargs["tools"] = tools

        t0 = time.time()
        stop_reason = "end_turn"
        input_tokens = 0
        output_tokens = 0

        try:
            response_stream = self.client.chat(**kwargs)
        except httpx.ConnectError:
            raise ConnectionError(
                f"Ollama is not reachable at {self._host}. Is the Ollama server running?"
            ) from None
        except _ollama.ResponseError as e:
            raise ConnectionError(f"Ollama error: {e.error}") from None

        # Accumulate tool calls across chunks (they arrive at end of stream)
        pending_tool_calls: list[ToolUseEvent] = []

        for chunk in response_stream:
            # Text content streams incrementally
            if chunk.message.content:
                yield TextDelta(text=chunk.message.content)

            # Tool calls (arrive on final chunks)
            if chunk.message.tool_calls:
                for tc in chunk.message.tool_calls:
                    input_truncated = False
                    args = tc.function.arguments
                    if not isinstance(args, dict):
                        args = {}
                        input_truncated = True
                    pending_tool_calls.append(
                        ToolUseEvent(
                            tool_use_id=str(ULID()),
                            name=tc.function.name,
                            input=args,
                            input_truncated=input_truncated,
                        )
                    )

            # Final chunk carries token counts
            if chunk.done:
                input_tokens = chunk.prompt_eval_count or 0
                output_tokens = chunk.eval_count or 0

                # Determine stop reason
                if pending_tool_calls:
                    stop_reason = "tool_use"
                elif chunk.done_reason == "length":
                    stop_reason = "max_tokens"
                else:
                    stop_reason = "end_turn"

        # Emit tool calls after stream completes (ToolUseStart before each ToolUseEvent)
        for call in pending_tool_calls:
            yield ToolUseStart(tool_use_id=call.tool_use_id, name=call.name)
            yield call

        yield Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_write_input_tokens=0,
        )
        yield Done(stop_reason=stop_reason)

        log.info(
            "Ollama request complete",
            extra={
                "model": self.model_id,
                "duration_s": round(time.time() - t0, 2),
                "stop_reason": stop_reason,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        )

    def invoke(self, messages: list[Turn], system: str) -> str:
        """Non-streaming call. Returns the response text."""
        ollama_messages = _turns_to_ollama_messages(messages, system)

        try:
            response = self.client.chat(model=self.model_id, messages=ollama_messages)
        except httpx.ConnectError:
            raise ConnectionError(
                f"Ollama is not reachable at {self._host}. Is the Ollama server running?"
            ) from None
        except _ollama.ResponseError as e:
            raise ConnectionError(f"Ollama error: {e.error}") from None

        return response.message.content or ""

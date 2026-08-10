"""Ollama local model client."""

import logging
import time
from collections.abc import Generator

import httpx
import ollama as _ollama
from archie_shared.types import TextBlock, ToolResultBlock, ToolUseBlock
from ulid import ULID

from archie_agent.llm._types import Done, StreamEvent, TextDelta, ToolUseEvent, ToolUseStart, Usage
from archie_agent.prompt import SystemPrompt, flatten_system_prompt
from archie_agent.session import Turn

log = logging.getLogger(__name__)
_DEFAULT_HOST = "http://host.docker.internal:11434"
_DEFAULT_TIMEOUT = 240.0


def _turns_to_ollama_messages(turns: list[Turn], system: str) -> list[dict]:
    """Translate internal turns to Ollama messages with one system message."""
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
        if turn.role == "assistant" and tool_calls:
            msg: dict = {"role": "assistant"}
            if text_parts:
                msg["content"] = "".join(text_parts)
            msg["tool_calls"] = [{"function": tc["function"]} for tc in tool_calls]
            messages.append(msg)
        elif tool_results:
            messages.extend(tool_results)
        elif text_parts:
            messages.append({"role": turn.role, "content": "".join(text_parts)})
    return messages


def _tool_config_to_ollama(tool_config: list[dict]) -> list[dict]:
    """Translate neutral tool configs to Ollama/OpenAI function format."""
    return [
        {
            "type": "function",
            "function": {
                "name": tc.get("name", ""),
                "description": tc.get("description", ""),
                "parameters": tc.get("input_schema", {}),
            },
        }
        for tc in tool_config
    ]


class OllamaClient:
    """Ollama local model client implementing the LLMClient protocol."""

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
        system: SystemPrompt | str,
        tool_config: list[dict] | None = None,
        history_boundary: str | None = None,
    ) -> Generator[StreamEvent]:
        """Stream an Ollama response; cache metadata is intentionally ignored."""
        del history_boundary
        ollama_messages = _turns_to_ollama_messages(messages, flatten_system_prompt(system))
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

        pending_tool_calls: list[ToolUseEvent] = []
        for chunk in response_stream:
            if chunk.message.content:
                yield TextDelta(text=chunk.message.content)
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
            if chunk.done:
                input_tokens = chunk.prompt_eval_count or 0
                output_tokens = chunk.eval_count or 0
                if pending_tool_calls:
                    stop_reason = "tool_use"
                elif chunk.done_reason == "length":
                    stop_reason = "max_tokens"
                else:
                    stop_reason = "end_turn"

        for call in pending_tool_calls:
            yield ToolUseStart(tool_use_id=call.tool_use_id, name=call.name)
            yield call
        yield Usage(input_tokens=input_tokens, output_tokens=output_tokens)
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

    def invoke(
        self,
        messages: list[Turn],
        system: SystemPrompt | str,
        tool_config: list[dict] | None = None,
        history_boundary: str | None = None,
    ) -> str:
        """Non-streaming call. Returns the response text."""
        del tool_config, history_boundary
        ollama_messages = _turns_to_ollama_messages(messages, flatten_system_prompt(system))
        try:
            response = self.client.chat(model=self.model_id, messages=ollama_messages)
        except httpx.ConnectError:
            raise ConnectionError(
                f"Ollama is not reachable at {self._host}. Is the Ollama server running?"
            ) from None
        except _ollama.ResponseError as e:
            raise ConnectionError(f"Ollama error: {e.error}") from None
        return response.message.content or ""

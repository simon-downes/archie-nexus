"""LLM client package.

Defines the LLMClient protocol — the provider-agnostic interface that the agent loop
types against. Stream event types are internal to the agent (not wire protocol events).

The agent loop translates internal stream events → wire protocol events before
broadcasting to clients.
"""

from collections.abc import Generator
from typing import Protocol

from archie_shared.models import BedrockProvider, ModelEntry, OllamaProvider

from archie_agent.llm._types import Done, StreamEvent, TextDelta, ToolUseEvent, ToolUseStart, Usage
from archie_agent.llm.bedrock import BedrockClient
from archie_agent.llm.fake import FakeLLMClient
from archie_agent.session import Turn


class LLMClient(Protocol):
    """Provider-agnostic LLM client interface."""

    model_id: str

    def stream(
        self,
        messages: list[Turn],
        system: str,
        tool_config: list[dict] | None = None,
    ) -> Generator[StreamEvent]:
        """Stream LLM response. Yields events as they arrive."""
        ...

    def invoke(self, messages: list[Turn], system: str) -> str:
        """Simple blocking call for one-shot prompts without tools."""
        ...


def create_llm_client(model: ModelEntry, default_region: str) -> LLMClient:
    """Create the appropriate LLM client based on the model's provider config.

    Dispatches on provider type. OllamaClient is lazy-imported to avoid loading
    the ollama package for bedrock-only sessions.
    """
    match model.provider:
        case BedrockProvider(model_id=model_id, region=region):
            return BedrockClient(
                model_id=model_id,
                region=region or default_region,
                max_output_tokens=model.max_output_tokens,
                can_cache=model.can_cache,
            )
        case OllamaProvider(model_id=model_id, endpoint=endpoint):
            from archie_agent.llm.ollama import OllamaClient

            return OllamaClient(
                model_id=model_id,
                host=f"http://{endpoint}",
                max_context_tokens=model.context,
            )
        case _:
            raise ValueError(f"Unknown provider type: {type(model.provider)}")


__all__ = [
    "BedrockClient",
    "Done",
    "FakeLLMClient",
    "LLMClient",
    "StreamEvent",
    "TextDelta",
    "ToolUseEvent",
    "ToolUseStart",
    "Usage",
    "create_llm_client",
]

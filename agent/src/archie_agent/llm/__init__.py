"""LLM client package and provider-neutral protocol."""

from collections.abc import Generator
from typing import Protocol

from archie_shared.models import BedrockOpenAIProvider, BedrockProvider, ModelEntry, OllamaProvider

from archie_agent.llm._types import Done, StreamEvent, TextDelta, ToolUseEvent, ToolUseStart, Usage
from archie_agent.llm.bedrock import BedrockClient
from archie_agent.llm.bedrock_openai import BedrockOpenAIClient
from archie_agent.llm.fake import FakeLLMClient
from archie_agent.prompt import SystemPrompt
from archie_agent.session import Turn


class LLMClient(Protocol):
    """Provider-agnostic LLM client interface."""

    model_id: str

    def stream(
        self,
        messages: list[Turn],
        system: SystemPrompt | str,
        tool_config: list[dict] | None = None,
        history_boundary: str | None = None,
    ) -> Generator[StreamEvent]:
        """Stream an LLM response and optionally identify its advancing boundary."""
        ...

    def invoke(
        self,
        messages: list[Turn],
        system: SystemPrompt | str,
        tool_config: list[dict] | None = None,
        history_boundary: str | None = None,
    ) -> str:
        """Make a blocking call for one-shot prompts."""
        ...


def create_llm_client(model: ModelEntry, default_region: str) -> LLMClient:
    """Create the appropriate LLM client based on the model provider config."""
    match model.provider:
        case BedrockProvider(model_id=model_id, region=region):
            return BedrockClient(
                model_id=model_id,
                region=region or default_region,
                max_output_tokens=model.max_output_tokens,
                can_cache=model.can_cache,
            )
        case BedrockOpenAIProvider(model_id=model_id, region=region):
            return BedrockOpenAIClient(
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
    "BedrockOpenAIClient",
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

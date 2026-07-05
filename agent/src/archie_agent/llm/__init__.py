"""LLM client package.

Defines the LLMClient protocol — the provider-agnostic interface that the agent loop
types against. Stream event types are internal to the agent (not wire protocol events).

The agent loop translates internal stream events → wire protocol events before
broadcasting to clients.
"""

from collections.abc import Generator
from typing import Protocol

from archie_agent.llm._types import Done, StreamEvent, TextDelta, ToolUseEvent, ToolUseStart, Usage
from archie_agent.llm.bedrock import BedrockClient
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


__all__ = [
    "BedrockClient",
    "Done",
    "LLMClient",
    "StreamEvent",
    "TextDelta",
    "ToolUseEvent",
    "ToolUseStart",
    "Usage",
]

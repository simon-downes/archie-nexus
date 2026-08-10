"""Deterministic fake LLM client for tests."""

import time
from collections.abc import Generator

from archie_agent.llm._types import StreamEvent
from archie_agent.prompt import SystemPrompt
from archie_agent.session import Turn


class FakeLLMClient:
    """Replay scripted event sequences, one sequence per stream call."""

    model_id = "fake-model"

    def __init__(self, responses: list[list[StreamEvent]], delay: float = 0.0):
        self._responses = responses
        self._delay = delay
        self._call_index = 0
        self.last_tool_config: list[dict] | None = None
        self.last_system: SystemPrompt | str | None = None
        self.calls: list[dict] = []

    def stream(
        self,
        messages: list[Turn],
        system: SystemPrompt | str,
        tool_config: list[dict] | None = None,
        history_boundary: str | None = None,
    ) -> Generator[StreamEvent]:
        """Yield the next scripted response sequence."""
        self.last_tool_config = tool_config
        self.last_system = system
        self.calls.append(
            {
                "messages": messages,
                "system": system,
                "tool_config": tool_config,
                "history_boundary": history_boundary,
            }
        )
        events = self._responses[self._call_index]
        self._call_index += 1
        first = True
        for event in events:
            if not first and self._delay:
                time.sleep(self._delay)
            first = False
            yield event

    def invoke(
        self,
        messages: list[Turn],
        system: SystemPrompt | str,
        tool_config: list[dict] | None = None,
        history_boundary: str | None = None,
    ) -> str:
        """Return a static response."""
        return "fake response"

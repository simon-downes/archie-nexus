"""Fake LLM client for deterministic testing.

Implements the LLMClient protocol with scripted StreamEvent sequences.
Replaces MagicMock/patch patterns for agent loop testing.
"""

import time
from collections.abc import Generator

from archie_agent.llm._types import StreamEvent
from archie_agent.session import Turn


class FakeLLMClient:
    """Scripted LLM client that yields pre-configured event sequences.

    Args:
        responses: One list of StreamEvents per expected stream() call.
                   Indexed sequentially; IndexError if exhausted.
        delay: Seconds to sleep between yielding events (for interrupt testing).
               Applies between events, not before the first.
    """

    model_id = "fake-echo"

    def __init__(self, responses: list[list[StreamEvent]], delay: float = 0.0) -> None:
        self._responses = responses
        self._delay = delay
        self._call_index = 0
        self.last_tool_config: list[dict] | None = None

    def stream(
        self,
        messages: list[Turn],
        system: str,
        tool_config: list[dict] | None = None,
    ) -> Generator[StreamEvent]:
        """Yield the next scripted response sequence."""
        self.last_tool_config = tool_config
        events = self._responses[self._call_index]
        self._call_index += 1
        first = True
        for event in events:
            if not first and self._delay > 0:
                time.sleep(self._delay)
            first = False
            yield event

    def invoke(self, messages: list[Turn], system: str) -> str:
        """Return a static response."""
        return "fake response"

"""Provider-neutral tool registry.

Defines tool specifications (name, description, schema, handler) and a registry
that produces neutral tool configs. Provider-specific shaping (e.g. Bedrock's
toolSpec envelope) lives in the respective LLM client, not here.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Specification for a single tool the model can call."""

    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]


class ToolRegistry:
    """Registry of available tools.

    Stores ToolSpecs and produces provider-neutral tool configs for passing to
    LLM clients.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """Register a tool specification."""
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        """Look up a tool by name."""
        return self._tools.get(name)

    def to_tool_config(self) -> list[dict[str, Any]]:
        """Return neutral tool configs for all registered tools.

        Format: [{"name": ..., "description": ..., "input_schema": ...}]
        This is NOT provider-shaped — each LLM client converts as needed.
        """
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.schema,
            }
            for spec in self._tools.values()
        ]

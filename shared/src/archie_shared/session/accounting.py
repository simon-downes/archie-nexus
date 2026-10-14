"""Cost aggregation for canonical request events and tool-linked scopes.

Subagent scope contract
-----------------------
A subagent's activity is attributed on the canonical event stream using only
launching tool IDs:

- A child agent's ``scope`` equals the ``tool_use_id`` of the ``tool_call`` that
  launched it. The root agent has ``scope = None``.
- Each provider request a child makes emits exactly one ``llm_request`` whose
  ``scope`` is that child's scope.
- A request is uniquely identified by ``(scope, turn, iteration, request_id)``.
- Parent/child links are reconstructed from ``tool_call`` events: the tool call
  that produced ``tool_use_id == S`` carries the ``scope`` of the *parent* that
  issued it, so ``parent_of(S) = tool_call(tool_use_id=S).scope``.
- ``scope_direct_costs`` sums ``cost_usd`` for requests billed directly to each
  scope; ``scope_inclusive_costs`` additionally rolls each scope's cost up
  through its parent chain to the root (``None``).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from archie_shared.events import LLMRequest, ToolCall


@dataclass
class UsageTotals:
    """Running token and cost totals folded from ``llm_request`` events."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    requests: int = 0

    def add(self, event: LLMRequest) -> None:
        self.input_tokens += event.input_tokens
        self.output_tokens += event.output_tokens
        self.cache_read_tokens += event.cache_read_tokens
        self.cache_write_tokens += event.cache_write_tokens
        self.cost_usd += event.cost_usd
        self.requests += 1

    @classmethod
    def of(cls, events) -> UsageTotals:
        """Totals over every ``llm_request`` in ``events``."""
        totals = cls()
        for event in events:
            if isinstance(event, LLMRequest):
                totals.add(event)
        return totals


def _parent_map(events: list[LLMRequest | ToolCall]) -> dict[str, str | None]:
    """Map each launched scope (``tool_use_id``) to its issuing parent scope."""
    parents: dict[str, str | None] = {}
    for event in events:
        if isinstance(event, ToolCall):
            parents[event.tool_use_id] = event.scope
    return parents


def scope_direct_costs(events: list[LLMRequest | ToolCall]) -> dict[str | None, float]:
    """Return the cost billed directly to each scope (no descendant rollup)."""
    direct: dict[str | None, float] = defaultdict(float)
    for event in events:
        if isinstance(event, LLMRequest):
            direct[event.scope] += event.cost_usd
    return {scope: round(value, 6) for scope, value in direct.items()}


def scope_inclusive_costs(events: list[LLMRequest | ToolCall]) -> dict[str | None, float]:
    """Return each scope's direct cost plus all descendant costs rolled up.

    Every scope's direct cost is added to each ancestor along its parent chain
    up to the root (``None``). A scope with no known parent tool call rolls up
    to the root.
    """
    direct: dict[str | None, float] = defaultdict(float)
    for event in events:
        if isinstance(event, LLMRequest):
            direct[event.scope] += event.cost_usd

    parents = _parent_map(events)
    inclusive: dict[str | None, float] = defaultdict(float)
    for scope, cost in direct.items():
        inclusive[scope] += cost
        current = scope
        seen: set[str | None] = {scope}
        while current is not None:
            parent = parents.get(current)
            if parent in seen:
                break
            if parent is None:
                inclusive[None] += cost
                break
            inclusive[parent] += cost
            seen.add(parent)
            current = parent
    return {scope: round(value, 6) for scope, value in inclusive.items()}


# Backwards-compatible alias: inclusive aggregation.
scope_costs = scope_inclusive_costs

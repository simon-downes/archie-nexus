"""Shared immutable Slack invocation context."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class OperationContext:
    """Non-secret invocation state shared across Slack boundaries."""

    principal_id: str
    workspace_id: str | None
    policy: dict[str, Any]
    deadline: float
    correlation: str | None = None

    @classmethod
    def create(
        cls,
        *,
        principal_id: str = "authenticated-user",
        workspace_id: str | None = None,
        policy: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> OperationContext:
        return cls(principal_id, workspace_id, policy or {}, time.monotonic() + timeout)

    def check_deadline(self) -> None:
        if time.monotonic() >= self.deadline:
            from .errors import SlackDeadlineError

            raise SlackDeadlineError("Slack operation deadline expired")

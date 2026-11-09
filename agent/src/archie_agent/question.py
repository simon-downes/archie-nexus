"""Root-only ``question`` tool: ask the user one question and wait for the answer.

The pending question is live state held by :class:`QuestionBroker`. It is published through
``session_status`` and ``/status``, never persisted: the tool's ``tool_call`` and
``tool_result`` are the durable record.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from archie_shared.events import PendingQuestion, QuestionOption

from archie_agent.tools import ToolSpec

log = logging.getLogger(__name__)

MIN_OPTIONS = 2
MAX_OPTIONS = 4

SELECTED_PREFIX = "User selected option "
ANSWERED_PREFIX = "User answered: "
DISMISSED_RESULT = "User dismissed the question without answering. Do not assume an answer."


class QuestionError(Exception):
    """An answer that does not fit the pending question; ``kind`` names the error notice."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class Answer:
    """One user reply: exactly one of ``option``, ``text`` or ``dismiss``."""

    option: int | None = None
    text: str | None = None
    dismiss: bool = False


class QuestionBroker:
    """Holds at most one pending question and wakes its waiter when it is answered."""

    def __init__(self, on_change: Callable[[], Awaitable[None]] | None = None) -> None:
        self._on_change = on_change
        self._pending: PendingQuestion | None = None
        self._future: asyncio.Future[Answer] | None = None

    @property
    def pending(self) -> PendingQuestion | None:
        """The question waiting for an answer, or ``None``."""
        return self._pending

    async def ask(self, question: PendingQuestion) -> Answer:
        """Publish ``question`` and wait for its answer.

        Raises:
            QuestionError: another question is already pending.
        """
        if self._pending is not None:
            raise QuestionError("question_pending", "another question is already waiting")
        future: asyncio.Future[Answer] = asyncio.get_running_loop().create_future()
        self._pending, self._future = question, future
        try:
            await self._notify()
            return await future
        finally:
            self._pending, self._future = None, None
            await asyncio.shield(self._notify())

    def answer(self, question_id: str, answer: Answer) -> None:
        """Resolve the pending question.

        Raises:
            QuestionError: ``question_not_pending`` for a stale or unknown id,
                ``invalid_answer`` for an option index out of range.
        """
        pending, future = self._pending, self._future
        if pending is None or future is None or future.done() or pending.id != question_id:
            raise QuestionError("question_not_pending", f"No pending question {question_id!r}")
        if answer.option is not None and not 0 <= answer.option < len(pending.options):
            raise QuestionError("invalid_answer", f"Option {answer.option} is out of range")
        future.set_result(answer)

    async def _notify(self) -> None:
        if self._on_change is None:
            return
        try:
            await self._on_change()
        except Exception:  # noqa: BLE001 — status publication is best effort
            log.warning("Failed to publish question status", exc_info=True)


def _parse(tool_use_id: str, question, options, recommended) -> PendingQuestion | str:
    """Validate tool input; return the pending question or an error message."""
    if not isinstance(question, str) or not question.strip():
        return "question must be a non-empty string"
    if not isinstance(options, list) or not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        return f"options must be a list of {MIN_OPTIONS} to {MAX_OPTIONS} items"
    parsed: list[QuestionOption] = []
    for index, option in enumerate(options):
        if not isinstance(option, dict):
            return f"options[{index}] must be an object with a label"
        label, description = option.get("label"), option.get("description", "")
        if not isinstance(label, str) or not label.strip():
            return f"options[{index}].label must be a non-empty string"
        if not isinstance(description, str):
            return f"options[{index}].description must be a string"
        parsed.append(QuestionOption(label=label.strip(), description=description.strip()))
    if recommended is not None and (
        isinstance(recommended, bool)
        or not isinstance(recommended, int)
        or not 0 <= recommended < len(parsed)
    ):
        return f"recommended must be an option index from 0 to {len(parsed) - 1}"
    return PendingQuestion(
        id=tool_use_id, question=question.strip(), options=parsed, recommended=recommended
    )


def format_answer(question: PendingQuestion, answer: Answer) -> str:
    """The model-facing result text for one answer."""
    if answer.option is not None:
        label = question.options[answer.option].label
        return f"{SELECTED_PREFIX}{answer.option + 1}: {label}"
    if answer.text is not None:
        return f"{ANSWERED_PREFIX}{answer.text}"
    return DISMISSED_RESULT


def create_question_tool(broker: QuestionBroker) -> ToolSpec:
    """Create the root ``question`` tool bound to ``broker``.

    The harness passes the call's ``tool_use_id`` as ``_tool_use_id``; it becomes the
    question id that answers refer to.
    """

    async def handler(
        question=None, options=None, recommended=None, _tool_use_id: str = "", **kwargs
    ) -> str:
        parsed = _parse(_tool_use_id, question, options, recommended)
        if isinstance(parsed, str):
            return f"Error: {parsed}"
        try:
            answer = await broker.ask(parsed)
        except QuestionError as exc:
            return f"Error: {exc}"
        return format_answer(parsed, answer)

    return ToolSpec(
        name="question",
        description=(
            "Ask the user one question and wait for the answer. Use it for a decision "
            "with 2-4 realistic options where the code and context don't settle the "
            "choice; don't use it for open-ended discussion or facts you can look up. "
            "Put your recommendation first or mark it with `recommended`, and give each "
            "option a short description of its trade-off. The user can pick an option, "
            "type a different answer, or dismiss the question. One question per call; "
            "never call it more than once at a time."
        ),
        schema={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question, one decision."},
                "options": {
                    "type": "array",
                    "minItems": MIN_OPTIONS,
                    "maxItems": MAX_OPTIONS,
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "Short option name."},
                            "description": {
                                "type": "string",
                                "description": "Optional one-line trade-off.",
                            },
                        },
                        "required": ["label"],
                    },
                },
                "recommended": {
                    "type": "integer",
                    "description": "Optional 0-based index of the recommended option.",
                },
            },
            "required": ["question", "options"],
        },
        handler=handler,
    )

"""Pure agent loop — async generator that drives streamed tool turns."""

import asyncio
import inspect
import logging
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from archie_shared.types import TextBlock, ToolResultBlock, ToolUseBlock

from archie_agent.events import (
    AgentEvent,
    IterationStart,
    TextDelta,
    ToolCall,
    ToolResult,
    TurnComplete,
    TurnError,
    TurnInterrupted,
    Usage,
)
from archie_agent.llm._types import Done, StreamEvent, ToolUseEvent, ToolUseStart
from archie_agent.llm._types import TextDelta as LLMTextDelta
from archie_agent.llm._types import Usage as LLMUsage
from archie_agent.prompt import SystemPrompt

if TYPE_CHECKING:
    from archie_agent.llm import LLMClient
    from archie_agent.session import Turn

log = logging.getLogger(__name__)
_SENTINEL_DONE = object()
_DEFAULT_MAX_ITERATIONS = 100


@dataclass
class _WorkerError:
    msg: str


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    sent_at: str


@dataclass
class RequestFinished:
    context: RequestContext
    duration_ms: int
    status: str
    usage: Usage | None
    stop_reason: str | None
    error: str | None


@dataclass
class _RequestResult:
    text_blocks: list[TextBlock] = field(default_factory=list)
    tool_use_blocks: list[ToolUseBlock] = field(default_factory=list)
    stop_reason: str | None = None
    usage: Usage | None = None
    interrupted: bool = False
    failed: bool = False
    error_msg: str | None = None


def _latest_history_boundary(messages: list["Turn"]) -> str | None:
    for turn in reversed(messages):
        for block in reversed(turn.content):
            if isinstance(block, TextBlock) and turn.role == "user" and block.text:
                return block.text
            if isinstance(block, ToolResultBlock) and block.content:
                return f"Tool error: {block.content}" if block.is_error else block.content
    return None


async def _execute_tool_batch(
    blocks: list[ToolUseBlock],
    execute_tool: Callable[[ToolUseBlock], Awaitable[ToolResultBlock]],
    interrupt: threading.Event,
    interrupt_async: asyncio.Event | None = None,
) -> AsyncGenerator[tuple[int, ToolResultBlock]]:
    """Execute one model tool batch concurrently and yield completed results.

    Interruption is delivered event-driven via ``interrupt_async`` (set by the
    harness through ``call_soon_threadsafe``); the batch races its workers
    against that event with a single ``asyncio.wait``. The threading ``interrupt``
    is only consulted for the pre-dispatch fast path. Callers that do not supply
    ``interrupt_async`` get a local event that is never set, so a pre-set
    threading event is honored solely by the pre-dispatch check below.
    """

    def cancelled(block: ToolUseBlock) -> ToolResultBlock:
        return ToolResultBlock(tool_use_id=block.tool_use_id, content="cancelled", is_error=True)

    if interrupt.is_set():
        for position, block in enumerate(blocks):
            yield position, cancelled(block)
        return

    if interrupt_async is None:
        interrupt_async = asyncio.Event()

    async def worker(position: int, block: ToolUseBlock) -> tuple[int, ToolResultBlock]:
        try:
            result = execute_tool(block)
            if inspect.isawaitable(result):
                result = await result
            if result.tool_use_id != block.tool_use_id:
                return position, ToolResultBlock(
                    tool_use_id=block.tool_use_id,
                    content=(
                        f"ValueError: tool result ID {result.tool_use_id!r} does not match "
                        f"requested ID {block.tool_use_id!r}"
                    ),
                    is_error=True,
                )
            return position, result
        except asyncio.CancelledError:
            return position, cancelled(block)
        except Exception as error:
            log.warning("execute_tool raised for %s: %s", block.name, error)
            return position, ToolResultBlock(
                tool_use_id=block.tool_use_id,
                content=f"{type(error).__name__}: {error}",
                is_error=True,
            )

    tasks = [asyncio.create_task(worker(position, block)) for position, block in enumerate(blocks)]
    task_positions = {task: position for position, task in enumerate(tasks)}
    pending: set[asyncio.Task[tuple[int, ToolResultBlock]]] = set(tasks)
    emitted: set[int] = set()
    interrupt_wait = asyncio.create_task(interrupt_async.wait())
    try:
        while pending:
            done, _ = await asyncio.wait(
                {*pending, interrupt_wait}, return_when=asyncio.FIRST_COMPLETED
            )

            if interrupt_async.is_set() or interrupt.is_set():
                for task in pending:
                    task.cancel()
                outcomes = await asyncio.gather(*pending, return_exceptions=True)
                for task, outcome in zip(pending, outcomes, strict=True):
                    if isinstance(outcome, tuple):
                        position, result = outcome
                    else:
                        position = task_positions[task]
                        result = cancelled(blocks[position])
                    if position not in emitted:
                        emitted.add(position)
                        yield position, result
                return

            for task in done:
                if task is interrupt_wait or task not in pending:
                    continue
                pending.remove(task)
                position, result = task.result()
                emitted.add(position)
                yield position, result
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        interrupt_wait.cancel()
        await asyncio.gather(interrupt_wait, return_exceptions=True)


async def run_loop(
    *,
    messages: "list[Turn]",
    system: SystemPrompt | str,
    llm: "LLMClient",
    interrupt: threading.Event,
    interrupt_async: asyncio.Event | None = None,
    tool_config: list[dict] | None = None,
    execute_tool: Callable[[ToolUseBlock], Awaitable[ToolResultBlock]] | None = None,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    request_context_factory: Callable[[], RequestContext] | None = None,
) -> AsyncGenerator[AgentEvent | RequestFinished]:
    """Yield AgentEvents by streaming from the LLM client in a tool loop."""
    working_messages: list[Turn] = list(messages)
    for iteration in range(max_iterations):
        yield IterationStart(index=iteration)
        result = _RequestResult()
        request_context = (
            request_context_factory()
            if request_context_factory
            else RequestContext(request_id="", sent_at=datetime.now(UTC).isoformat())
        )
        started = datetime.now(UTC)
        async for event in _stream_once(
            messages=working_messages,
            system=system,
            llm=llm,
            interrupt=interrupt,
            tool_config=tool_config,
            history_boundary=_latest_history_boundary(working_messages),
            result=result,
        ):
            yield event
        if request_context_factory is not None:
            status = (
                "interrupted"
                if result.interrupted
                else "error"
                if result.failed
                else "completed"
                if result.usage is not None
                else "no_usage"
            )
            yield RequestFinished(
                context=request_context,
                duration_ms=max(0, int((datetime.now(UTC) - started).total_seconds() * 1000)),
                status=status,
                usage=result.usage,
                stop_reason=result.stop_reason,
                error=result.error_msg,
            )
        if result.failed:
            yield TurnError(error=result.error_msg or "unknown error")
            return
        if result.interrupted:
            if result.tool_use_blocks and execute_tool:
                from archie_agent.session import Turn as TurnType

                repair_results = [
                    ToolResultBlock(
                        tool_use_id=block.tool_use_id, content="cancelled", is_error=True
                    )
                    for block in result.tool_use_blocks
                ]
                for repair_result in repair_results:
                    yield ToolResult(
                        tool_use_id=repair_result.tool_use_id,
                        content=repair_result.content,
                        is_error=repair_result.is_error,
                    )
                working_messages.append(
                    TurnType(role="assistant", content=result.text_blocks + result.tool_use_blocks)
                )
                working_messages.append(TurnType(role="user", content=repair_results))
            yield TurnInterrupted()
            return
        has_tool_use = bool(result.tool_use_blocks)
        if result.usage is not None:
            yield result.usage
        if has_tool_use and result.stop_reason == "tool_use" and execute_tool:
            from archie_agent.session import Turn as TurnType

            working_messages.append(
                TurnType(role="assistant", content=result.text_blocks + result.tool_use_blocks)
            )
            for block in result.tool_use_blocks:
                yield ToolCall(tool_use_id=block.tool_use_id, name=block.name, input=block.input)
            tool_results: list[ToolResultBlock | None] = [None] * len(result.tool_use_blocks)
            async for position, result_block in _execute_tool_batch(
                result.tool_use_blocks, execute_tool, interrupt, interrupt_async
            ):
                tool_results[position] = result_block
                yield ToolResult(
                    tool_use_id=result_block.tool_use_id,
                    content=result_block.content,
                    is_error=result_block.is_error,
                    duration_ms=result_block.duration_ms,
                    result_lines=result_block.result_lines,
                    result_bytes=result_block.result_bytes,
                )
            ordered_results = [
                block
                if block is not None
                else ToolResultBlock(
                    tool_use_id=source.tool_use_id, content="cancelled", is_error=True
                )
                for source, block in zip(result.tool_use_blocks, tool_results, strict=True)
            ]
            working_messages.append(TurnType(role="user", content=ordered_results))
            if interrupt.is_set():
                yield TurnInterrupted()
                return
            continue
        yield TurnComplete(stop_reason=result.stop_reason or "end_turn")
        return
    yield TurnError(error="max tool iterations")


async def _stream_once(
    *,
    messages: "list[Turn]",
    system: SystemPrompt | str,
    llm: "LLMClient",
    interrupt: threading.Event,
    tool_config: list[dict] | None,
    history_boundary: str | None,
    result: _RequestResult,
) -> AsyncGenerator[AgentEvent]:
    """Stream one request while supporting older test doubles."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[StreamEvent | _WorkerError | object] = asyncio.Queue()

    def _worker() -> None:
        gen: Generator[StreamEvent] | None = None
        try:
            kwargs = {"messages": messages, "system": system, "tool_config": tool_config}
            try:
                parameters = inspect.signature(llm.stream).parameters
            except (TypeError, ValueError):
                parameters = {}
            if "history_boundary" in parameters:
                kwargs["history_boundary"] = history_boundary
            gen = llm.stream(**kwargs)
            for event in gen:
                if interrupt.is_set():
                    gen.close()
                    break
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as error:
            log.exception("Stream worker error")
            loop.call_soon_threadsafe(
                queue.put_nowait, _WorkerError(f"{type(error).__name__}: {error}")
            )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL_DONE)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    current_tool_use_id = ""
    current_tool_name = ""
    try:
        while True:
            event = await queue.get()
            if event is _SENTINEL_DONE:
                if interrupt.is_set():
                    result.interrupted = True
                break
            if isinstance(event, _WorkerError):
                result.failed = True
                result.error_msg = event.msg
                while await queue.get() is not _SENTINEL_DONE:
                    pass
                break
            if interrupt.is_set():
                result.interrupted = True
                while await queue.get() is not _SENTINEL_DONE:
                    pass
                break
            if isinstance(event, LLMTextDelta):
                result.text_blocks.append(TextBlock(text=event.text))
                yield TextDelta(text=event.text)
            elif isinstance(event, ToolUseStart):
                current_tool_use_id, current_tool_name = event.tool_use_id, event.name
            elif isinstance(event, ToolUseEvent):
                result.tool_use_blocks.append(
                    ToolUseBlock(
                        tool_use_id=event.tool_use_id or current_tool_use_id,
                        name=event.name or current_tool_name,
                        input=event.input,
                    )
                )
            elif isinstance(event, LLMUsage):
                result.usage = Usage(
                    input_tokens=event.input_tokens,
                    output_tokens=event.output_tokens,
                    cache_read_tokens=event.cache_read_tokens,
                    cache_write_tokens=event.cache_write_tokens,
                )
            elif isinstance(event, Done):
                result.stop_reason = event.stop_reason
    finally:
        thread.join(timeout=5.0)
        if thread.is_alive():
            log.warning(
                "Stream worker thread did not exit within 5s (likely blocked on network I/O)"
            )

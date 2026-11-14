"""Graceful agent shutdown: a running turn is interrupted and logs its end first."""

import asyncio
import threading
from collections.abc import Generator

from archie_agent import app as app_module
from archie_agent.harness import AgentHarness
from archie_agent.llm._types import Done, StreamEvent, TextDelta
from archie_agent.loop import _RequestResult, stream_once
from archie_agent.session import Session, Turn
from archie_shared.events import TurnError, TurnInterrupted, UserMessage, decode_event
from archie_shared.models import BedrockProvider, CostConfig, ModelEntry, Rate
from archie_shared.types import TextBlock


class SilentLLM:
    """Sends one text delta, then stays silent until released (a slow provider)."""

    model_id = "silent"

    def __init__(self) -> None:
        self.release = threading.Event()
        self.closed = threading.Event()

    def stream(self, messages, system, tool_config=None) -> Generator[StreamEvent]:
        try:
            yield TextDelta(text="partial")
            self.release.wait(10)
            yield Done(stop_reason="end_turn")
        finally:
            self.closed.set()


def _harness(tmp_path, llm) -> AgentHarness:
    model = ModelEntry(
        name="Test Model",
        provider=BedrockProvider(model_id="test-model-id", region="us-east-1"),
        cost=CostConfig(rates=[Rate(input=3.0, output=15.0)]),
        context=200000,
        max_output_tokens=4096,
    )
    session = Session(model_id="test-model", model=model, session_id="shutdown-001")
    return AgentHarness(session=session, llm_client=llm, model_name="Test", log_dir=tmp_path)


def _events(tmp_path) -> list:
    path = tmp_path / "shutdown-001.jsonl"
    if not path.exists():
        return []
    return [decode_event(line, persisted=True) for line in path.read_bytes().splitlines()]


async def test_interrupt_ends_a_request_while_the_provider_is_silent():
    llm = SilentLLM()
    interrupt, interrupt_async = threading.Event(), asyncio.Event()
    result = _RequestResult()
    seen = []

    async def consume():
        async for event in stream_once(
            messages=[Turn(role="user", content=[TextBlock(text="hi")], turn_index=1)],
            system="s",
            llm=llm,
            interrupt=interrupt,
            tool_config=None,
            history_boundary=None,
            result=result,
            request_id="r1",
            interrupt_async=interrupt_async,
        ):
            seen.append(event)
            interrupt.set()
            interrupt_async.set()

    await asyncio.wait_for(consume(), timeout=2)
    assert result.interrupted and len(seen) == 1
    # The abandoned worker closes the provider stream at its next event.
    llm.release.set()
    assert await asyncio.to_thread(llm.closed.wait, 2)


async def test_shutdown_interrupts_the_turn_and_logs_its_end(tmp_path, monkeypatch):
    llm = SilentLLM()
    harness = _harness(tmp_path, llm)
    task = asyncio.create_task(harness.handle_message("go"))
    monkeypatch.setattr(app_module, "_active_tasks", {task})
    while not any(isinstance(e, UserMessage) for e in _events(tmp_path)):
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)  # the request is now waiting on the silent provider

    await asyncio.wait_for(app_module._end_active_turn(harness, grace=5), timeout=3)
    assert task.done()
    terminal = [e for e in _events(tmp_path) if isinstance(e, TurnInterrupted | TurnError)]
    assert [type(e) for e in terminal] == [TurnInterrupted]
    assert not harness.turn_active
    llm.release.set()


async def test_shutdown_cancels_a_turn_that_ignores_the_interrupt(tmp_path, monkeypatch):
    harness = _harness(tmp_path, SilentLLM())
    started = asyncio.Event()

    async def stuck():
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(stuck())
    monkeypatch.setattr(app_module, "_active_tasks", {task})
    await started.wait()
    await app_module._end_active_turn(harness, grace=0.05)
    assert task.cancelled()


async def test_shutdown_with_no_turn_does_nothing(tmp_path, monkeypatch):
    harness = _harness(tmp_path, SilentLLM())
    monkeypatch.setattr(app_module, "_active_tasks", set())
    calls = []
    monkeypatch.setattr(harness, "interrupt", lambda *a: calls.append(a))
    await app_module._end_active_turn(harness, grace=1)
    assert calls == []

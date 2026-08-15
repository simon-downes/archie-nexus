import asyncio
import threading

from archie_agent.harness import AgentHarness
from archie_shared.events import InterruptCommand, deserialize_command, serialize_command


def test_targeted_interrupt_command_round_trip():
    command = InterruptCommand(target=("task-use-1", 2))

    decoded = deserialize_command(serialize_command(command))

    assert decoded == command


def test_untargeted_interrupt_command_preserves_empty_shape():
    command = InterruptCommand()

    assert command.to_json() == {"type": "interrupt", "data": {}}
    assert deserialize_command(serialize_command(command)) == command


def test_targeted_interrupt_rejects_partial_target():
    import pytest

    with pytest.raises(ValueError):
        deserialize_command('{"type":"interrupt","data":{"scope":"task"}}')


def test_harness_targeted_interrupt_signals_only_target(tmp_path):
    harness = object.__new__(AgentHarness)
    calls: list[str] = []
    target_event = threading.Event()
    sibling_event = threading.Event()
    target_async = asyncio.Event()
    sibling_async = asyncio.Event()

    harness._children = {
        ("parent", 0): (target_event, target_async, lambda: calls.append("target")),
        ("parent", 1): (sibling_event, sibling_async, lambda: calls.append("sibling")),
    }

    harness.interrupt(("parent", 0))

    assert target_event.is_set()
    assert target_async.is_set()
    assert not sibling_event.is_set()
    assert not sibling_async.is_set()
    assert calls == ["target"]

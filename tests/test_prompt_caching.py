"""Focused regressions for session prompt snapshots and Responses boundaries."""

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from archie_agent.harness import AgentHarness
from archie_agent.llm._types import Done, TextDelta, ToolUseEvent
from archie_agent.llm.bedrock_openai import BedrockOpenAIClient, _turns_to_responses_input
from archie_agent.llm.fake import FakeLLMClient
from archie_agent.prompt import PromptSection, SystemPrompt
from archie_agent.session import Session, Turn
from archie_shared.models import BedrockProvider, ModelEntry
from archie_shared.types import TextBlock, ToolResultBlock, ToolUseBlock


def _model() -> ModelEntry:
    return ModelEntry(name="Test", context=100_000, provider=BedrockProvider(model_id="test"))


def test_tool_result_is_typed_and_function_call_has_no_marker():
    items = _turns_to_responses_input(
        [
            Turn(role="assistant", content=[ToolUseBlock("call", "exec", {"source": "x"})]),
            Turn(role="user", content=[ToolResultBlock("call", "result")]),
        ]
    )
    assert items[0]["type"] == "function_call"
    assert "prompt_cache_breakpoint" not in items[0]
    assert items[1]["output"] == [{"type": "input_text", "text": "result"}]


def test_advancing_marker_retains_old_and_marks_new_within_limit():
    class _Stream(list):
        def close(self):
            pass

    client = BedrockOpenAIClient.__new__(BedrockOpenAIClient)
    client.model_id = "model"
    client._region = "region"
    client.max_output_tokens = 100
    client.can_cache = True
    client._cache_enabled = True
    client._previous_conversation_fingerprint = None
    client.client = MagicMock()
    client.client.responses.create.side_effect = [_Stream([]), _Stream([])]
    system = SystemPrompt(PromptSection("static"), PromptSection("dynamic"))
    turns = [Turn(role="user", content=[TextBlock("first")])]
    list(client.stream(turns, system))
    turns.append(Turn(role="user", content=[TextBlock("second")]))
    list(client.stream(turns, system))
    request = client.client.responses.create.call_args_list[1].kwargs
    marked = []
    for item in request["input"]:
        blocks = item.get("content", []) if item.get("role") else item.get("output", [])
        marked.extend(block["text"] for block in blocks if "prompt_cache_breakpoint" in block)
    assert marked == ["static", "dynamic", "first", "second"]
    assert len(marked) <= 4


@pytest.mark.asyncio
async def test_run_loop_reuses_one_prompt_snapshot_for_tool_requests():
    llm = FakeLLMClient(
        responses=[
            [ToolUseEvent("call", "exec", {"source": "x"}), Done("tool_use")],
            [TextDelta("done"), Done("end_turn")],
        ]
    )
    session_messages = [Turn(role="user", content=[TextBlock("question")])]
    from archie_agent.loop import run_loop

    async def execute_tool(block):
        return ToolResultBlock(block.tool_use_id, "result")

    async for _event in run_loop(
        messages=session_messages,
        system=SystemPrompt(PromptSection("static"), PromptSection("dynamic")),
        llm=llm,
        interrupt=threading.Event(),
        execute_tool=execute_tool,
    ):
        pass
    assert len(llm.calls) == 2
    assert llm.calls[0]["system"] is llm.calls[1]["system"]


def test_harness_captures_agents_context_once(monkeypatch, tmp_path):
    snapshots = iter(["first rules", "changed rules"])
    monkeypatch.setattr("archie_agent.harness.read_agents_context", lambda: next(snapshots))
    llm = FakeLLMClient(responses=[[Done("end_turn")], [Done("end_turn")]])
    session = Session(model_id="test", model=_model(), session_id="snapshot")
    harness = AgentHarness(session, llm, "model", Path(tmp_path))
    first = harness._build_prompt()
    second = harness._build_prompt()
    assert "first rules" in first.static_system.text
    assert "first rules" in second.static_system.text
    assert "changed rules" not in second.static_system.text



def _marked_texts(request: dict) -> list[str]:
    marked: list[str] = []
    for item in request["input"]:
        blocks = item.get("content", []) if item.get("role") else item.get("output", [])
        marked.extend(
            block["text"]
            for block in blocks
            if "prompt_cache_breakpoint" in block and "text" in block
        )
    return marked


def test_batched_tool_results_mark_only_final_result():
    class _Stream(list):
        def close(self):
            pass

    client = BedrockOpenAIClient.__new__(BedrockOpenAIClient)
    client.model_id = "model"
    client._region = "region"
    client.max_output_tokens = 100
    client.can_cache = True
    client._cache_enabled = True
    client._previous_conversation_fingerprint = None
    client.client = MagicMock()
    client.client.responses.create.return_value = _Stream([])
    system = SystemPrompt(PromptSection("static"), PromptSection("dynamic"))
    turns = [
        Turn(
            role="assistant",
            content=[
                ToolUseBlock("call-1", "exec", {"source": "one"}),
                ToolUseBlock("call-2", "exec", {"source": "two"}),
            ],
        ),
        Turn(
            role="user",
            content=[
                ToolResultBlock("call-1", "first result"),
                ToolResultBlock("call-2", "final result"),
            ],
        ),
    ]

    list(client.stream(turns, system))

    request = client.client.responses.create.call_args.kwargs
    assert _marked_texts(request) == ["static", "dynamic", "final result"]
    function_calls = [item for item in request["input"] if item["type"] == "function_call"]
    assert all("prompt_cache_breakpoint" not in item for item in function_calls)


def test_tool_result_marker_advances_to_new_user_suffix():
    class _Stream(list):
        def close(self):
            pass

    client = BedrockOpenAIClient.__new__(BedrockOpenAIClient)
    client.model_id = "model"
    client._region = "region"
    client.max_output_tokens = 100
    client.can_cache = True
    client._cache_enabled = True
    client._previous_conversation_fingerprint = None
    client.client = MagicMock()
    client.client.responses.create.side_effect = [_Stream([]), _Stream([])]
    system = SystemPrompt(PromptSection("static"), PromptSection("dynamic"))
    turns = [Turn(role="user", content=[ToolResultBlock("call", "tool result")])]

    list(client.stream(turns, system))
    turns.append(Turn(role="user", content=[TextBlock("new user input")]))
    list(client.stream(turns, system))

    request = client.client.responses.create.call_args_list[1].kwargs
    assert _marked_texts(request) == ["static", "dynamic", "tool result", "new user input"]
    assert len(_marked_texts(request)) == 4


def test_marker_mismatch_falls_back_to_new_boundary(caplog):
    class _Stream(list):
        def close(self):
            pass

    client = BedrockOpenAIClient.__new__(BedrockOpenAIClient)
    client.model_id = "model"
    client._region = "region"
    client.max_output_tokens = 100
    client.can_cache = True
    client._cache_enabled = True
    client._previous_conversation_fingerprint = None
    client.client = MagicMock()
    client.client.responses.create.side_effect = [_Stream([]), _Stream([])]
    system = SystemPrompt(PromptSection("static"), PromptSection("dynamic"))

    list(client.stream([Turn(role="user", content=[TextBlock("old")])], system))
    with caplog.at_level("WARNING"):
        list(client.stream([Turn(role="user", content=[TextBlock("new")])], system))

    request = client.client.responses.create.call_args_list[1].kwargs
    assert _marked_texts(request) == ["static", "dynamic", "new"]
    assert "no longer matches history" in caplog.text

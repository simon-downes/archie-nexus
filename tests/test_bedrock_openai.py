"""Request-shape and usage tests for the Bedrock OpenAI Responses adapter."""

from unittest.mock import MagicMock

from archie_agent.llm._types import Done, TextDelta, ToolUseEvent, ToolUseStart, Usage
from archie_agent.llm.bedrock_openai import BedrockOpenAIClient, prompt_cache_key
from archie_agent.prompt import PromptSection, SystemPrompt
from archie_agent.session import Turn
from archie_shared.types import TextBlock
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseFunctionToolCall,
    ResponseOutputItemAddedEvent,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseTextDeltaEvent,
    ResponseUsage,
)
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails


def _text_delta(delta: str) -> ResponseTextDeltaEvent:
    return ResponseTextDeltaEvent(
        content_index=0,
        delta=delta,
        item_id="msg_1",
        logprobs=[],
        output_index=0,
        sequence_number=0,
        type="response.output_text.delta",
    )


def _usage(input_tokens: int, output_tokens: int, cached: int, cache_write: int) -> ResponseUsage:
    return ResponseUsage(
        input_tokens=input_tokens,
        input_tokens_details=InputTokensDetails(
            cached_tokens=cached, cache_write_tokens=cache_write
        ),
        output_tokens=output_tokens,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        total_tokens=input_tokens + output_tokens,
    )


def _response(usage: ResponseUsage | None) -> Response:
    return Response(
        id="resp_1",
        created_at=0.0,
        model="openai.gpt-5.6-luna",
        object="response",
        output=[],
        parallel_tool_calls=False,
        tool_choice="auto",
        tools=[],
        usage=usage,
    )


def _completed(response: Response) -> ResponseCompletedEvent:
    return ResponseCompletedEvent(response=response, sequence_number=0, type="response.completed")


def _function_call_item(call_id: str, item_id: str, name: str) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        arguments="", call_id=call_id, name=name, type="function_call", id=item_id
    )


def _response_stream(events: list):
    class ResponseStream(list):
        pass

    stream = ResponseStream(events)
    stream.close = MagicMock()
    return stream


def _make_client(can_cache: bool = False) -> tuple[BedrockOpenAIClient, MagicMock]:
    fake_client = MagicMock()
    client = BedrockOpenAIClient.__new__(BedrockOpenAIClient)
    client.client = fake_client
    client.model_id = "openai.gpt-5.6-luna"
    client._region = "us-east-1"
    client.max_output_tokens = 32_768
    client.can_cache = can_cache
    client._cache_enabled = can_cache
    client._previous_conversation_fingerprint = None
    return client, fake_client


def test_stream_text_and_usage_normalizes_billable_input():
    stream = _response_stream(
        [_text_delta("Hello"), _text_delta(" world"), _completed(_response(_usage(100, 20, 30, 5)))]
    )
    client, fake_client = _make_client()
    fake_client.responses.create.return_value = stream
    events = list(client.stream([Turn(role="user", content=[TextBlock(text="hi")])], "system"))
    assert [type(event) for event in events] == [TextDelta, TextDelta, Usage, Done]
    assert events[2].input_tokens == 65
    assert events[2].cache_read_tokens == 30
    assert events[2].cache_write_tokens == 5


def test_cached_input_over_raw_total_is_sanitized():
    response = _response(_usage(20, 5, 30, 5))
    client, _ = _make_client()
    usage = client  # keep construction through the public adapter below
    assert usage
    from archie_agent.llm.bedrock_openai import _response_usage

    normalized = _response_usage(response)
    assert normalized.input_tokens == 20
    assert normalized.cache_read_tokens == 0
    assert normalized.cache_write_tokens == 0


def test_developer_prompt_and_cache_metadata_shape():
    system = SystemPrompt(PromptSection("static"), PromptSection("dynamic"))
    stream = _response_stream([_completed(_response(_usage(10, 2, 0, 0)))])
    client, fake_client = _make_client(can_cache=True)
    fake_client.responses.create.return_value = stream
    list(
        client.stream(
            [Turn(role="user", content=[TextBlock(text="hi")])],
            system,
            tool_config=[{"name": "exec", "description": "run", "input_schema": {}}],
        )
    )
    request = fake_client.responses.create.call_args.kwargs
    assert "instructions" not in request
    assert request["input"][0]["type"] == "message"
    assert request["input"][0]["role"] == "developer"
    assert [block["type"] for block in request["input"][0]["content"]] == [
        "input_text",
        "input_text",
    ]
    assert all(
        block["prompt_cache_breakpoint"] == {"mode": "explicit"}
        for block in request["input"][0]["content"]
    )
    assert request["extra_body"]["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    assert request["tools"][0]["name"] == "exec"
    assert (
        sum(
            "prompt_cache_breakpoint" in block
            for item in request["input"]
            if item.get("type") == "message"
            for block in item.get("content", [])
        )
        == 2
    )


def test_multi_turn_assistant_history_uses_valid_message_shape():
    stream = _response_stream([_completed(_response(_usage(10, 2, 0, 0)))])
    client, fake_client = _make_client()
    fake_client.responses.create.return_value = stream
    messages = [
        Turn(role="user", content=[TextBlock(text="first")]),
        Turn(role="assistant", content=[TextBlock(text="answer")]),
        Turn(role="user", content=[TextBlock(text="second")]),
    ]

    list(client.stream(messages, "system"))

    request = fake_client.responses.create.call_args.kwargs
    assert request["input"][1] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "first"}],
    }
    assert request["input"][2] == {"role": "assistant", "content": "answer"}
    assert request["input"][3] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "second"}],
    }


def test_stream_maps_function_call_and_never_marks_function_call_item():
    stream = _response_stream(
        [
            ResponseOutputItemAddedEvent(
                item=_function_call_item("call_1", "fc_1", "exec"),
                output_index=0,
                sequence_number=0,
                type="response.output_item.added",
            ),
            ResponseFunctionCallArgumentsDeltaEvent(
                delta='{"command":',
                item_id="fc_1",
                output_index=0,
                sequence_number=0,
                type="response.function_call_arguments.delta",
            ),
            ResponseFunctionCallArgumentsDoneEvent(
                arguments='{"command":"pwd"}',
                item_id="fc_1",
                name="exec",
                output_index=0,
                sequence_number=0,
                type="response.function_call_arguments.done",
            ),
            _completed(_response(_usage(120, 15, 0, 0))),
        ]
    )
    client, fake_client = _make_client(can_cache=True)
    fake_client.responses.create.return_value = stream
    events = list(client.stream([], "system", tool_config=[{"name": "exec", "input_schema": {}}]))
    assert isinstance(events[0], ToolUseStart)
    assert isinstance(events[1], ToolUseEvent)
    request = fake_client.responses.create.call_args.kwargs
    assert all(
        "prompt_cache_breakpoint" not in item
        for item in request["input"]
        if item.get("type") == "function_call"
    )


def test_invoke_uses_same_developer_shape():
    response = _response(_usage(10, 5, 0, 0))
    response.output = [
        ResponseOutputMessage(
            id="msg_1",
            content=[ResponseOutputText(annotations=[], text="answer", type="output_text")],
            role="assistant",
            status="completed",
            type="message",
        )
    ]
    client, fake_client = _make_client(can_cache=True)
    fake_client.responses.create.return_value = response
    assert client.invoke([Turn(role="user", content=[TextBlock(text="q")])], "system") == "answer"
    request = fake_client.responses.create.call_args.kwargs
    assert request["input"][0]["role"] == "developer"
    assert "instructions" not in request
    assert "stream" not in request


def test_cache_key_excludes_model_dynamic_and_agents_but_changes_tools():
    first = SystemPrompt(
        PromptSection("static\n<agents.md>one</agents.md>"), PromptSection("dynamic-one")
    )
    second = SystemPrompt(
        PromptSection("static\n<agents.md>two</agents.md>"), PromptSection("dynamic-two")
    )
    tools = [{"name": "a", "description": "", "input_schema": {}}]
    assert prompt_cache_key(first, tools) == prompt_cache_key(second, tools)
    assert prompt_cache_key(first, tools) != prompt_cache_key(
        first, tools + [{"name": "b", "input_schema": {}}]
    )


def test_cache_validation_retries_without_metadata():
    client, fake_client = _make_client(can_cache=True)
    stream = _response_stream([_completed(_response(_usage(5, 1, 0, 0)))])
    error = RuntimeError("400 invalid prompt cache breakpoint")
    error.status_code = 400
    fake_client.responses.create.side_effect = [error, stream]
    list(client.stream([Turn(role="user", content=[TextBlock(text="hi")])], "system"))
    first, second = fake_client.responses.create.call_args_list
    assert "prompt_cache_key" in first.kwargs
    assert "prompt_cache_key" not in second.kwargs
    assert "extra_body" not in second.kwargs
    assert all(
        "prompt_cache_breakpoint" not in block
        for item in second.kwargs["input"]
        if item.get("type") == "message"
        for block in item.get("content", [])
    )
    assert client._cache_enabled is False

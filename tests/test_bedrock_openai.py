"""Tests for the Bedrock OpenAI Responses API client."""

from unittest.mock import MagicMock

from archie_agent.llm._types import Done, TextDelta, ToolUseEvent, ToolUseStart, Usage
from archie_agent.llm.bedrock_openai import BedrockOpenAIClient
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
    return ResponseCompletedEvent(
        response=response, sequence_number=0, type="response.completed"
    )


def _function_call_item(call_id: str, item_id: str, name: str) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        arguments="",
        call_id=call_id,
        name=name,
        type="function_call",
        id=item_id,
    )


def _response_stream(events: list):
    class ResponseStream(list):
        pass

    stream = ResponseStream(events)
    stream.close = MagicMock()
    return stream


def _make_client() -> tuple[BedrockOpenAIClient, MagicMock]:
    fake_client = MagicMock()
    client = BedrockOpenAIClient.__new__(BedrockOpenAIClient)
    client.client = fake_client
    client.model_id = "openai.gpt-5.6-luna"
    client._region = "us-east-1"
    client.max_output_tokens = 32_768
    return client, fake_client


def test_stream_text_and_usage():
    stream = _response_stream(
        [
            _text_delta("Hello"),
            _text_delta(" world"),
            _completed(_response(_usage(100, 20, cached=30, cache_write=5))),
        ]
    )
    client, fake_client = _make_client()
    fake_client.responses.create.return_value = stream

    events = list(client.stream([Turn(role="user", content=[TextBlock(text="hi")])], "system"))

    assert [type(event) for event in events] == [TextDelta, TextDelta, Usage, Done]
    assert events[0].text == "Hello"
    assert events[1].text == " world"
    assert events[2].input_tokens == 100
    assert events[2].cache_read_input_tokens == 30
    assert events[2].cache_write_input_tokens == 5
    assert events[3].stop_reason == "end_turn"
    stream.close.assert_called_once()


def test_stream_maps_response_function_call_item_id_to_call_id():
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
            _completed(_response(_usage(120, 15, cached=0, cache_write=0))),
        ]
    )
    client, fake_client = _make_client()
    fake_client.responses.create.return_value = stream

    events = list(client.stream([], "system", tool_config=[{"name": "exec", "input_schema": {}}]))

    assert isinstance(events[0], ToolUseStart)
    assert events[0].tool_use_id == "call_1"
    assert isinstance(events[1], ToolUseEvent)
    assert events[1].tool_use_id == "call_1"
    assert events[1].name == "exec"
    assert events[1].input == {"command": "pwd"}
    assert isinstance(events[2], Usage)
    assert isinstance(events[3], Done)
    assert events[3].stop_reason == "tool_use"
    fake_client.responses.create.assert_called_once()
    request = fake_client.responses.create.call_args.kwargs
    assert request["model"] == "openai.gpt-5.6-luna"
    assert request["instructions"] == "system"
    assert request["tools"][0]["type"] == "function"


def test_invoke_returns_output_text():
    response = _response(_usage(10, 5, cached=0, cache_write=0))
    response.output = [
        ResponseOutputMessage(
            id="msg_1",
            content=[ResponseOutputText(annotations=[], text="the answer", type="output_text")],
            role="assistant",
            status="completed",
            type="message",
        )
    ]
    client, fake_client = _make_client()
    fake_client.responses.create.return_value = response

    result = client.invoke([Turn(role="user", content=[TextBlock(text="q")])], "system")

    assert result == "the answer"
    request = fake_client.responses.create.call_args.kwargs
    assert request["model"] == "openai.gpt-5.6-luna"
    assert request["instructions"] == "system"
    assert request["store"] is False
    assert "stream" not in request


def test_invoke_returns_empty_string_when_no_text():
    client, fake_client = _make_client()
    fake_client.responses.create.return_value = _response(None)

    assert client.invoke([], "system") == ""


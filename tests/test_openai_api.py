"""Tests for the direct OpenAI Responses API client."""

from unittest.mock import MagicMock

import pytest
from archie_agent.llm import OpenAIClient, create_llm_client
from archie_agent.llm._types import Done, TextDelta, ToolUseEvent, ToolUseStart, Usage
from archie_agent.llm.openai_api import OPENAI_BASE_URL
from archie_agent.prompt import PromptSection, SystemPrompt
from archie_agent.session import Turn
from archie_shared.credentials import replace_credential, save_store
from archie_shared.models import DEFAULT_MODELS
from archie_shared.types import ImageBlock, TextBlock, ToolResultBlock
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseError,
    ResponseFailedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseFunctionToolCall,
    ResponseIncompleteEvent,
    ResponseOutputItemAddedEvent,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseTextDeltaEvent,
    ResponseUsage,
)
from openai.types.responses.response import IncompleteDetails
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

MODEL_KEY = "openai-gpt-6-luna"


@pytest.fixture(autouse=True)
def _isolated_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def _usage(input_tokens=10, output_tokens=2, cached=0, written=0) -> ResponseUsage:
    return ResponseUsage(
        input_tokens=input_tokens,
        input_tokens_details=InputTokensDetails(cached_tokens=cached, cache_write_tokens=written),
        output_tokens=output_tokens,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        total_tokens=input_tokens + output_tokens,
    )


def _response(usage=None, **extra) -> Response:
    return Response(
        id="resp_1",
        created_at=0.0,
        model="gpt-6-luna",
        object="response",
        output=[],
        parallel_tool_calls=False,
        tool_choice="auto",
        tools=[],
        usage=usage or _usage(),
        **extra,
    )


def _completed(response=None) -> ResponseCompletedEvent:
    return ResponseCompletedEvent(
        response=response or _response(), sequence_number=0, type="response.completed"
    )


def _stream(events):
    class ResponseStream(list):
        pass

    stream = ResponseStream(events)
    stream.close = MagicMock()
    return stream


def _client(can_cache=False, can_see=False) -> tuple[OpenAIClient, MagicMock]:
    client = OpenAIClient(
        "gpt-6-luna", max_output_tokens=128_000, can_cache=can_cache, can_see=can_see
    )
    fake = MagicMock()
    client.client = fake
    return client, fake


def _user(text="hi") -> list[Turn]:
    return [Turn(role="user", content=[TextBlock(text=text)])]


def test_factory_routes_openai_provider_to_openai_client():
    client = create_llm_client(DEFAULT_MODELS[MODEL_KEY], "eu-west-1")
    assert isinstance(client, OpenAIClient)
    assert client.model_id == "gpt-6-luna"
    assert client.max_output_tokens == 128_000
    assert client.can_cache and client.can_see


@pytest.fixture
def sdk(monkeypatch):
    """Patch the SDK constructor (the network boundary) and record its kwargs."""
    built = []

    def factory(**kwargs):
        fake = MagicMock()
        fake.responses.create.return_value = _stream([_completed()])
        built.append((kwargs, fake))
        return fake

    monkeypatch.setattr("archie_agent.llm.openai_api.OpenAI", factory)
    return built


def test_sdk_client_uses_stored_key_and_public_endpoint(monkeypatch, sdk):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    replace_credential("openai", {"api_key": "sk-stored"})
    client = OpenAIClient("gpt-6-luna")
    list(client.stream(_user(), "system"))
    list(client.stream(_user(), "system"))
    assert len(sdk) == 1
    kwargs, fake = sdk[0]
    assert kwargs["api_key"] == "sk-stored"
    assert kwargs["base_url"] == OPENAI_BASE_URL
    assert fake.responses.create.call_count == 2


def test_env_key_is_used_when_nothing_is_stored(monkeypatch, sdk):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    list(OpenAIClient("gpt-6-luna").stream(_user(), "system"))
    assert sdk[0][0]["api_key"] == "sk-env"


def test_missing_key_fails_the_request_not_construction(sdk):
    client = OpenAIClient("gpt-6-luna")
    with pytest.raises(RuntimeError, match="archie auth login openai"):
        list(client.stream(_user(), "system"))
    with pytest.raises(RuntimeError, match="archie auth login openai"):
        client.invoke(_user(), "system")
    assert sdk == []
    replace_credential("openai", {"api_key": "sk-later"})
    events = list(client.stream(_user(), "system"))
    assert events[-1] == Done("end_turn")
    assert sdk[0][0]["api_key"] == "sk-later"


def test_malformed_stored_entry_raises_instead_of_using_env(monkeypatch, sdk):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    save_store({"openai": {"token": "wrong-field"}})
    with pytest.raises(ValueError):
        list(OpenAIClient("gpt-6-luna").stream(_user(), "system"))
    assert sdk == []


def test_request_shape_with_cache_tools_and_images():
    client, fake = _client(can_cache=True, can_see=True)
    fake.responses.create.return_value = _stream([_completed()])
    image = ImageBlock(format="png", data=b"hi", width=1, height=1)
    messages = [
        Turn(role="user", content=[TextBlock(text="look")]),
        Turn(
            role="user",
            content=[ToolResultBlock(tool_use_id="call_1", content="shot", images=[image])],
        ),
    ]
    system = SystemPrompt(PromptSection("static"), PromptSection("dynamic"))
    list(
        client.stream(
            messages,
            system,
            tool_config=[{"name": "exec", "description": "run", "input_schema": {}}],
        )
    )
    request = fake.responses.create.call_args.kwargs
    assert request["model"] == "gpt-6-luna"
    assert request["store"] is False
    assert request["stream"] is True
    assert request["max_output_tokens"] == 128_000
    assert request["input"][0]["role"] == "developer"
    assert all("prompt_cache_breakpoint" in b for b in request["input"][0]["content"])
    assert request["extra_body"]["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    assert request["prompt_cache_key"].startswith("archie:")
    assert request["tools"][0]["name"] == "exec"
    output = request["input"][-1]["output"]
    assert output[0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert output[1]["type"] == "input_image"


def test_request_without_cache_has_no_cache_metadata():
    client, fake = _client()
    fake.responses.create.return_value = _stream([_completed()])
    list(client.stream(_user(), "system"))
    request = fake.responses.create.call_args.kwargs
    assert "prompt_cache_key" not in request
    assert "extra_body" not in request
    assert "prompt_cache_breakpoint" not in request["input"][0]["content"][0]


def test_stream_maps_text_tool_calls_usage_and_stop_reason():
    events = [
        ResponseTextDeltaEvent(
            content_index=0,
            delta="Hello",
            item_id="msg_1",
            logprobs=[],
            output_index=0,
            sequence_number=0,
            type="response.output_text.delta",
        ),
        ResponseOutputItemAddedEvent(
            item=ResponseFunctionToolCall(
                arguments="", call_id="call_1", name="exec", type="function_call", id="fc_1"
            ),
            output_index=1,
            sequence_number=1,
            type="response.output_item.added",
        ),
        ResponseFunctionCallArgumentsDoneEvent(
            arguments='{"command":"pwd"}',
            item_id="fc_1",
            name="exec",
            output_index=1,
            sequence_number=2,
            type="response.function_call_arguments.done",
        ),
        _completed(_response(_usage(100, 20, 30, 5))),
    ]
    client, fake = _client()
    fake.responses.create.return_value = _stream(events)
    out = list(client.stream(_user(), "system"))
    assert [type(e) for e in out] == [TextDelta, ToolUseStart, ToolUseEvent, Usage, Done]
    assert out[2].input == {"command": "pwd"}
    assert (out[3].input_tokens, out[3].cache_read_tokens, out[3].cache_write_tokens) == (65, 30, 5)
    assert out[4].stop_reason == "tool_use"


def test_incomplete_max_output_maps_to_max_tokens():
    response = _response(incomplete_details=IncompleteDetails(reason="max_output_tokens"))
    event = ResponseIncompleteEvent(
        response=response, sequence_number=0, type="response.incomplete"
    )
    client, fake = _client()
    fake.responses.create.return_value = _stream([event])
    out = list(client.stream(_user(), "system"))
    assert out[-1] == Done("max_tokens")


def test_cache_validation_error_retries_once_without_cache_metadata():
    client, fake = _client(can_cache=True)
    error = RuntimeError("400 invalid prompt_cache breakpoint")
    error.status_code = 400
    fake.responses.create.side_effect = [error, _stream([_completed()])]
    list(client.stream(_user(), "system"))
    first, second = fake.responses.create.call_args_list
    assert "prompt_cache_key" in first.kwargs
    assert "prompt_cache_key" not in second.kwargs
    assert "extra_body" not in second.kwargs
    fake.responses.create.side_effect = None
    fake.responses.create.return_value = _stream([_completed()])
    list(client.stream(_user("again"), "system"))
    third = fake.responses.create.call_args.kwargs
    assert "prompt_cache_key" not in third
    assert "extra_body" not in third


def _call_added(call_id="call_1", item_id="fc_1"):
    return ResponseOutputItemAddedEvent(
        item=ResponseFunctionToolCall(
            arguments="", call_id=call_id, name="exec", type="function_call", id=item_id
        ),
        output_index=0,
        sequence_number=0,
        type="response.output_item.added",
    )


def _args_done(arguments, item_id="fc_1"):
    return ResponseFunctionCallArgumentsDoneEvent(
        arguments=arguments,
        item_id=item_id,
        name="exec",
        output_index=0,
        sequence_number=0,
        type="response.function_call_arguments.done",
    )


def test_argument_deltas_accumulate_and_malformed_json_is_truncated():
    delta = ResponseFunctionCallArgumentsDeltaEvent(
        delta='{"command":',
        item_id="fc_1",
        output_index=0,
        sequence_number=0,
        type="response.function_call_arguments.delta",
    )
    client, fake = _client()
    fake.responses.create.return_value = _stream(
        [_call_added(), delta, _args_done('{"command":'), _completed()]
    )
    out = [e for e in client.stream(_user(), "system") if isinstance(e, ToolUseEvent)]
    assert out[0].input == {}
    assert out[0].input_truncated is True


def test_failed_event_raises_and_closes_stream():
    failed = ResponseFailedEvent(
        response=_response(error=ResponseError(code="server_error", message="boom")),
        sequence_number=0,
        type="response.failed",
    )
    client, fake = _client()
    stream = _stream([failed])
    fake.responses.create.return_value = stream
    with pytest.raises(RuntimeError, match="boom"):
        list(client.stream(_user(), "system"))
    stream.close.assert_called_once()


def test_invoke_returns_output_text_without_stream_flag():
    response = _response()
    response.output = [
        ResponseOutputMessage(
            id="msg_1",
            content=[ResponseOutputText(annotations=[], text="answer", type="output_text")],
            role="assistant",
            status="completed",
            type="message",
        )
    ]
    client, fake = _client()
    fake.responses.create.return_value = response
    assert client.invoke(_user("q"), "system") == "answer"
    assert "stream" not in fake.responses.create.call_args.kwargs

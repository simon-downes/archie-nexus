"""Tests for the Bedrock streaming client with mocked boto3."""

from unittest.mock import MagicMock, patch

from archie_agent.llm import BedrockClient, Done, TextDelta, Usage
from archie_agent.session import Turn
from archie_shared.types import TextBlock


def _make_mock_stream(events: list[dict]):
    """Create a mock EventStream that yields the given events."""
    stream = MagicMock()
    stream.__iter__ = MagicMock(return_value=iter(events))
    stream.close = MagicMock()
    return stream


def test_stream_text_response():
    """Verify stream yields TextDelta, Usage, and Done for a simple text response."""
    mock_events = [
        {"contentBlockStart": {"start": {}, "contentBlockIndex": 0}},
        {"contentBlockDelta": {"delta": {"text": "Hello"}, "contentBlockIndex": 0}},
        {"contentBlockDelta": {"delta": {"text": " world"}, "contentBlockIndex": 0}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {
            "metadata": {
                "usage": {
                    "inputTokens": 100,
                    "outputTokens": 10,
                    "cacheReadInputTokens": 50,
                    "cacheWriteInputTokens": 5,
                }
            }
        },
        {"messageStop": {"stopReason": "end_turn"}},
    ]

    mock_stream = _make_mock_stream(mock_events)

    with patch("archie_agent.llm.bedrock.boto3") as mock_boto3:
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse_stream.return_value = {
            "ResponseMetadata": {"RequestId": "test-123"},
            "stream": mock_stream,
        }

        client = BedrockClient(model_id="test-model", region="us-east-1", can_cache=True)
        messages = [Turn(role="user", content=[TextBlock(text="hi")], turn_index=1)]

        events = list(client.stream(messages, system="You are helpful."))

    # Should yield: TextDelta("Hello"), TextDelta(" world"), Usage(...), Done(...)
    assert len(events) == 4
    assert isinstance(events[0], TextDelta)
    assert events[0].text == "Hello"
    assert isinstance(events[1], TextDelta)
    assert events[1].text == " world"
    assert isinstance(events[2], Usage)
    assert events[2].input_tokens == 100
    assert events[2].output_tokens == 10
    assert events[2].cache_read_input_tokens == 50
    assert events[2].cache_write_input_tokens == 5
    assert isinstance(events[3], Done)
    assert events[3].stop_reason == "end_turn"

    # Verify stream was closed
    mock_stream.close.assert_called_once()


def test_stream_closes_on_early_exit():
    """Verify EventStream is closed even if consumer stops early (interrupt)."""
    mock_events = [
        {"contentBlockStart": {"start": {}, "contentBlockIndex": 0}},
        {"contentBlockDelta": {"delta": {"text": "Hello"}, "contentBlockIndex": 0}},
        {"contentBlockDelta": {"delta": {"text": " world"}, "contentBlockIndex": 0}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"metadata": {"usage": {"inputTokens": 50, "outputTokens": 5}}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]

    mock_stream = _make_mock_stream(mock_events)

    with patch("archie_agent.llm.bedrock.boto3") as mock_boto3:
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse_stream.return_value = {
            "ResponseMetadata": {"RequestId": "test-456"},
            "stream": mock_stream,
        }

        client = BedrockClient(model_id="test-model", region="us-east-1", can_cache=True)
        messages = [Turn(role="user", content=[TextBlock(text="hi")], turn_index=1)]

        # Only consume the first event then break (simulating interrupt)
        gen = client.stream(messages, system="test")
        first = next(gen)
        assert isinstance(first, TextDelta)
        gen.close()  # This triggers the finally block

    # Stream must still be closed
    mock_stream.close.assert_called_once()



def test_stream_preserves_converse_cache_counts_separately_from_uncached_input():
    """Converse inputTokens is uncached and may be smaller than cache-read input."""
    mock_events = [
        {
            "metadata": {
                "usage": {
                    "inputTokens": 10,
                    "outputTokens": 2,
                    "cacheReadInputTokens": 20,
                    "cacheWriteInputTokens": 5,
                }
            }
        },
        {"messageStop": {"stopReason": "end_turn"}},
    ]
    mock_stream = _make_mock_stream(mock_events)

    with patch("archie_agent.llm.bedrock.boto3") as mock_boto3:
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse_stream.return_value = {
            "ResponseMetadata": {},
            "stream": mock_stream,
        }

        client = BedrockClient(model_id="test-model", region="us-east-1")
        events = list(client.stream([], system="test"))

    usage = next(event for event in events if isinstance(event, Usage))
    assert usage.input_tokens == 10
    assert usage.cache_read_tokens == 20
    assert usage.cache_write_tokens == 5

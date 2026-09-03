"""Tests for OllamaClient with mocked ollama.Client."""

from unittest.mock import MagicMock, patch

import httpx
import pytest
from archie_agent.llm._types import Done, TextDelta, ToolUseEvent, ToolUseStart, Usage
from archie_agent.session import Turn
from archie_shared.types import TextBlock, ToolResultBlock, ToolUseBlock


@pytest.fixture
def mock_ollama_client():
    """Patch ollama.Client so OllamaClient can be instantiated without a server."""
    with patch("archie_agent.llm.ollama._ollama.Client") as mock_cls:
        client_instance = MagicMock()
        mock_cls.return_value = client_instance
        yield client_instance


def _make_chunk(
    content="",
    tool_calls=None,
    done=False,
    done_reason="stop",
    prompt_eval_count=None,
    eval_count=None,
):
    """Create a mock chunk object mimicking ollama's response format."""
    chunk = MagicMock()
    chunk.message.content = content
    chunk.message.tool_calls = tool_calls
    chunk.done = done
    chunk.done_reason = done_reason
    chunk.prompt_eval_count = prompt_eval_count
    chunk.eval_count = eval_count
    return chunk


class TestOllamaClientStream:
    """Test streaming responses."""

    def test_text_streaming(self, mock_ollama_client):
        """Text chunks are yielded as TextDelta events."""
        from archie_agent.llm.ollama import OllamaClient

        mock_ollama_client.chat.return_value = iter(
            [
                _make_chunk(content="Hello"),
                _make_chunk(content=" world"),
                _make_chunk(done=True, prompt_eval_count=10, eval_count=5),
            ]
        )

        client = OllamaClient(model_id="test:latest", host="http://localhost:11434")
        messages = [Turn(role="user", content=[TextBlock(text="Hi")])]
        events = list(client.stream(messages, system="You are helpful."))

        # Should have: TextDelta("Hello"), TextDelta(" world"), Usage, Done
        text_events = [e for e in events if isinstance(e, TextDelta)]
        assert len(text_events) == 2
        assert text_events[0].text == "Hello"
        assert text_events[1].text == " world"

        usage = next(e for e in events if isinstance(e, Usage))
        assert usage.input_tokens == 10
        assert usage.output_tokens == 5
        assert usage.cache_read_input_tokens == 0
        assert usage.cache_write_input_tokens == 0

        done = next(e for e in events if isinstance(e, Done))
        assert done.stop_reason == "end_turn"

    def test_tool_calling(self, mock_ollama_client):
        """Tool calls are accumulated and emitted after stream completes."""
        from archie_agent.llm.ollama import OllamaClient

        tc = MagicMock()
        tc.function.name = "read_file"
        tc.function.arguments = {"path": "/tmp/test.txt"}

        mock_ollama_client.chat.return_value = iter(
            [
                _make_chunk(content="Let me read that."),
                _make_chunk(tool_calls=[tc], done=True, prompt_eval_count=20, eval_count=15),
            ]
        )

        client = OllamaClient(model_id="test:latest", host="http://localhost:11434")
        messages = [Turn(role="user", content=[TextBlock(text="Read the file")])]
        tool_config = [{"name": "read_file", "description": "Read a file", "input_schema": {}}]
        events = list(client.stream(messages, system="sys", tool_config=tool_config))

        # Should have: TextDelta, ToolUseStart, ToolUseEvent, Usage, Done
        starts = [e for e in events if isinstance(e, ToolUseStart)]
        uses = [e for e in events if isinstance(e, ToolUseEvent)]
        assert len(starts) == 1
        assert len(uses) == 1
        assert starts[0].name == "read_file"
        assert uses[0].name == "read_file"
        assert uses[0].input == {"path": "/tmp/test.txt"}
        assert uses[0].input_truncated is False
        assert uses[0].tool_use_id  # should be a non-empty ULID string

        done = next(e for e in events if isinstance(e, Done))
        assert done.stop_reason == "tool_use"

    def test_malformed_tool_arguments(self, mock_ollama_client):
        """Malformed tool arguments set input_truncated=True."""
        from archie_agent.llm.ollama import OllamaClient

        tc = MagicMock()
        tc.function.name = "write_file"
        tc.function.arguments = "not a dict"  # malformed

        mock_ollama_client.chat.return_value = iter(
            [
                _make_chunk(tool_calls=[tc], done=True, prompt_eval_count=5, eval_count=3),
            ]
        )

        client = OllamaClient(model_id="test:latest", host="http://localhost:11434")
        messages = [Turn(role="user", content=[TextBlock(text="Write")])]
        events = list(client.stream(messages, system="sys"))

        uses = [e for e in events if isinstance(e, ToolUseEvent)]
        assert len(uses) == 1
        assert uses[0].input == {}
        assert uses[0].input_truncated is True

    def test_max_tokens_stop_reason(self, mock_ollama_client):
        """done_reason='length' maps to stop_reason='max_tokens'."""
        from archie_agent.llm.ollama import OllamaClient

        mock_ollama_client.chat.return_value = iter(
            [
                _make_chunk(content="truncated output"),
                _make_chunk(done=True, done_reason="length", prompt_eval_count=100, eval_count=50),
            ]
        )

        client = OllamaClient(model_id="test:latest", host="http://localhost:11434")
        messages = [Turn(role="user", content=[TextBlock(text="Write an essay")])]
        events = list(client.stream(messages, system="sys"))

        done = next(e for e in events if isinstance(e, Done))
        assert done.stop_reason == "max_tokens"

    def test_missing_token_counts_default_zero(self, mock_ollama_client):
        """None token counts on final chunk default to 0."""
        from archie_agent.llm.ollama import OllamaClient

        mock_ollama_client.chat.return_value = iter(
            [
                _make_chunk(content="hi", done=True, prompt_eval_count=None, eval_count=None),
            ]
        )

        client = OllamaClient(model_id="test:latest", host="http://localhost:11434")
        messages = [Turn(role="user", content=[TextBlock(text="hi")])]
        events = list(client.stream(messages, system="sys"))

        usage = next(e for e in events if isinstance(e, Usage))
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0


class TestOllamaClientErrors:
    """Test error handling."""

    def test_connection_error(self, mock_ollama_client):
        """httpx.ConnectError raises ConnectionError with helpful message."""
        from archie_agent.llm.ollama import OllamaClient

        mock_ollama_client.chat.side_effect = httpx.ConnectError("Connection refused")

        client = OllamaClient(model_id="test:latest", host="http://localhost:11434")
        messages = [Turn(role="user", content=[TextBlock(text="hi")])]

        with pytest.raises(ConnectionError, match="not reachable"):
            list(client.stream(messages, system="sys"))

    def test_response_error(self, mock_ollama_client):
        """ollama.ResponseError raises ConnectionError."""
        import ollama as _ollama
        from archie_agent.llm.ollama import OllamaClient

        mock_ollama_client.chat.side_effect = _ollama.ResponseError("model not found")

        client = OllamaClient(model_id="missing:latest", host="http://localhost:11434")
        messages = [Turn(role="user", content=[TextBlock(text="hi")])]

        with pytest.raises(ConnectionError, match="Ollama error"):
            list(client.stream(messages, system="sys"))


class TestOllamaClientInvoke:
    """Test non-streaming invoke."""

    def test_invoke_returns_text(self, mock_ollama_client):
        """invoke() returns the response text."""
        from archie_agent.llm.ollama import OllamaClient

        mock_ollama_client.chat.return_value = MagicMock(
            message=MagicMock(content="Hello from ollama")
        )

        client = OllamaClient(model_id="test:latest", host="http://localhost:11434")
        messages = [Turn(role="user", content=[TextBlock(text="Hi")])]
        result = client.invoke(messages, system="sys")
        assert result == "Hello from ollama"

    def test_invoke_connection_error(self, mock_ollama_client):
        """invoke() raises ConnectionError when unreachable."""
        from archie_agent.llm.ollama import OllamaClient

        mock_ollama_client.chat.side_effect = httpx.ConnectError("Connection refused")

        client = OllamaClient(model_id="test:latest", host="http://localhost:11434")
        messages = [Turn(role="user", content=[TextBlock(text="hi")])]

        with pytest.raises(ConnectionError, match="not reachable"):
            client.invoke(messages, system="sys")


class TestMessageTranslation:
    """Test message format translation."""

    def test_system_prompt_prepended(self, mock_ollama_client):
        """System prompt becomes first message with role='system'."""
        from archie_agent.llm.ollama import _turns_to_ollama_messages

        turns = [Turn(role="user", content=[TextBlock(text="Hello")])]
        messages = _turns_to_ollama_messages(turns, system="Be helpful")

        assert messages[0] == {"role": "system", "content": "Be helpful"}
        assert messages[1] == {"role": "user", "content": "Hello"}

    def test_tool_use_and_result_translation(self, mock_ollama_client):
        """ToolUseBlock and ToolResultBlock translate correctly."""
        from archie_agent.llm.ollama import _turns_to_ollama_messages

        turns = [
            Turn(role="user", content=[TextBlock(text="Read file")]),
            Turn(
                role="assistant",
                content=[
                    TextBlock(text="Reading..."),
                    ToolUseBlock(tool_use_id="abc", name="read", input={"path": "/tmp"}),
                ],
            ),
            Turn(
                role="user",
                content=[
                    ToolResultBlock(tool_use_id="abc", content="file contents", is_error=False),
                ],
            ),
        ]
        messages = _turns_to_ollama_messages(turns, system="sys")

        # system + user + assistant(with tool_calls) + tool result
        assert len(messages) == 4
        assert messages[2]["role"] == "assistant"
        assert messages[2]["content"] == "Reading..."
        assert messages[2]["tool_calls"][0]["function"]["name"] == "read"
        assert messages[3] == {"role": "tool", "content": "file contents"}


class TestToolConfigTranslation:
    """Test tool config format translation."""

    def test_neutral_to_openai_format(self, mock_ollama_client):
        """Neutral tool config translates to OpenAI function format."""
        from archie_agent.llm.ollama import _tool_config_to_ollama

        neutral = [
            {
                "name": "read_file",
                "description": "Read a file from disk",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ]
        result = _tool_config_to_ollama(neutral)

        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "read_file"
        assert result[0]["function"]["description"] == "Read a file from disk"
        assert result[0]["function"]["parameters"]["type"] == "object"


class TestProviderFactory:
    """Test create_llm_client dispatches correctly."""

    def test_bedrock_provider_returns_bedrock_client(self):
        """BedrockProvider creates a BedrockClient."""
        from unittest.mock import patch

        from archie_agent.llm import create_llm_client
        from archie_agent.llm.bedrock import BedrockClient
        from archie_shared.models import BedrockProvider, ModelEntry

        model = ModelEntry(
            name="Test",
            context=200_000,
            provider=BedrockProvider(model_id="test.model-id", region="us-east-1"),
            can_cache=True,
        )

        with patch.object(BedrockClient, "__init__", return_value=None):
            client = create_llm_client(model, default_region="eu-west-1")
            assert isinstance(client, BedrockClient)

    def test_ollama_provider_returns_ollama_client(self, mock_ollama_client):
        """OllamaProvider creates an OllamaClient."""
        from archie_agent.llm import create_llm_client
        from archie_agent.llm.ollama import OllamaClient
        from archie_shared.models import ModelEntry, OllamaProvider

        model = ModelEntry(
            name="Test Ollama",
            context=128_000,
            provider=OllamaProvider(model_id="test:latest", endpoint="localhost:11434"),
        )

        client = create_llm_client(model, default_region="eu-west-1")
        assert isinstance(client, OllamaClient)
        assert client.model_id == "test:latest"

    def test_bedrock_uses_model_region_over_default(self):
        """BedrockProvider.region takes precedence over default_region."""
        from unittest.mock import patch

        from archie_agent.llm import create_llm_client
        from archie_agent.llm.bedrock import BedrockClient
        from archie_shared.models import BedrockProvider, ModelEntry

        model = ModelEntry(
            name="Test",
            context=200_000,
            provider=BedrockProvider(model_id="test.model-id", region="us-west-2"),
        )

        with patch.object(BedrockClient, "__init__", return_value=None) as mock_init:
            create_llm_client(model, default_region="eu-west-1")
            mock_init.assert_called_once_with(
                model_id="test.model-id",
                region="us-west-2",
                max_output_tokens=32_768,
                can_cache=False,
            )

    def test_bedrock_falls_back_to_default_region(self):
        """When BedrockProvider.region is None, uses default_region."""
        from unittest.mock import patch

        from archie_agent.llm import create_llm_client
        from archie_agent.llm.bedrock import BedrockClient
        from archie_shared.models import BedrockProvider, ModelEntry

        model = ModelEntry(
            name="Test",
            context=200_000,
            provider=BedrockProvider(model_id="test.model-id"),
        )

        with patch.object(BedrockClient, "__init__", return_value=None) as mock_init:
            create_llm_client(model, default_region="eu-west-1")
            mock_init.assert_called_once_with(
                model_id="test.model-id",
                region="eu-west-1",
                max_output_tokens=32_768,
                can_cache=False,
            )

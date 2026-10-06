"""Tests for image support: read-tool image loading, provider translation, events."""

import base64
import io
import json

import pytest
from archie_agent.event_factory import EventFactory
from archie_agent.exec.tools import BinaryFileError
from archie_agent.exec.tools.fs import read
from archie_agent.exec.tools.images import (
    MAX_LONG_EDGE,
    MAX_PIXELS,
    ImageError,
    ToolImage,
    detect_image_format,
)
from archie_agent.harness import AgentHarness
from archie_agent.llm._images import IMAGE_UNSUPPORTED_NOTE
from archie_agent.llm._types import Done, TextDelta, ToolUseEvent, Usage
from archie_agent.llm.bedrock import _turns_to_bedrock_messages
from archie_agent.llm.bedrock_openai import _turns_to_responses_input
from archie_agent.llm.fake import FakeLLMClient
from archie_agent.llm.ollama import _turns_to_ollama_messages
from archie_agent.session import Session, Turn
from archie_shared.events import ToolResult, decode_event, encode_event
from archie_shared.models import DEFAULT_MODELS, BedrockProvider, ModelEntry
from archie_shared.types import ImageBlock, ToolResultBlock
from PIL import Image


def _png(size=(32, 16), mode="RGB", color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("archie_agent.exec.tools.fs.WORKSPACE", tmp_path)
    return tmp_path


def _block(images=()) -> ToolResultBlock:
    return ToolResultBlock(tool_use_id="t1", content="Image: a.png", images=images)


_IMG = ImageBlock(format="png", data=b"\x89PNGfake", width=2, height=1, source="a.png")


# --- Format detection ---


@pytest.mark.parametrize(
    ("head", "fmt"),
    [
        (b"\x89PNG\r\n\x1a\nrest", "png"),
        (b"\xff\xd8\xff\xe0rest", "jpeg"),
        (b"GIF89a....", "gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"),
        (b"\x00\x01\x02binary", None),
        (b"plain text", None),
    ],
)
def test_detect_image_format(head, fmt):
    assert detect_image_format(head) == fmt


# --- read tool ---


async def test_read_small_png_passes_through_unchanged(workspace):
    data = _png()
    (workspace / "a.png").write_bytes(data)
    result = await read("a.png")
    assert isinstance(result, ToolImage)
    assert result.image.data == data
    assert result.image.format == "png"
    assert (result.image.width, result.image.height) == (32, 16)
    assert "a.png" in result.caption and "32x16" in result.caption
    assert "resized" not in result.caption


async def test_read_large_image_is_downscaled_to_jpeg(workspace):
    (workspace / "big.png").write_bytes(_png(size=(4000, 2000)))
    result = await read("big.png")
    assert isinstance(result, ToolImage)
    w, h = result.image.width, result.image.height
    assert max(w, h) <= MAX_LONG_EDGE
    assert w * h <= MAX_PIXELS
    assert abs(w / h - 2.0) < 0.01  # aspect ratio preserved
    assert result.image.format == "jpeg"  # opaque → JPEG
    assert Image.open(io.BytesIO(result.image.data)).size == (w, h)
    assert "resized from 4000x2000" in result.caption


async def test_read_large_transparent_image_stays_png(workspace):
    (workspace / "alpha.png").write_bytes(_png(size=(3000, 3000), mode="RGBA", color=(0, 0, 0, 0)))
    result = await read("alpha.png")
    assert result.image.format == "png"
    assert result.image.width * result.image.height <= MAX_PIXELS


async def test_read_corrupt_image_raises(workspace):
    (workspace / "bad.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00garbage" * 10)
    with pytest.raises(ImageError):
        await read("bad.png")


async def test_read_non_image_binary_still_rejected(workspace):
    (workspace / "blob.bin").write_bytes(b"\x00\x01\x02\x03binary")
    with pytest.raises(BinaryFileError):
        await read("blob.bin")


async def test_read_inside_exec_returns_caption_text(workspace):
    from archie_agent.exec.runner import _wrap_for_audit

    (workspace / "a.png").write_bytes(_png())
    calls = []
    result = await _wrap_for_audit(read, "read", calls)("a.png")
    assert isinstance(result, str)
    assert "Image: a.png" in result
    assert "native `read`" in result
    assert calls and calls[0].ok


# --- Provider translation ---


def test_bedrock_converse_sends_image_blocks_when_model_can_see():
    msgs = _turns_to_bedrock_messages([Turn(role="user", content=[_block((_IMG,))])], can_see=True)
    content = msgs[0]["content"][0]["toolResult"]["content"]
    assert content[0] == {"text": "Image: a.png"}
    assert content[1] == {"image": {"format": "png", "source": {"bytes": _IMG.data}}}


def test_bedrock_converse_omits_images_for_blind_model():
    msgs = _turns_to_bedrock_messages([Turn(role="user", content=[_block((_IMG,))])])
    content = msgs[0]["content"][0]["toolResult"]["content"]
    assert len(content) == 1
    assert IMAGE_UNSUPPORTED_NOTE in content[0]["text"]


def test_bedrock_converse_text_result_unchanged():
    msgs = _turns_to_bedrock_messages([Turn(role="user", content=[_block()])], can_see=True)
    assert msgs[0]["content"][0]["toolResult"]["content"] == [{"text": "Image: a.png"}]


def test_responses_sends_input_image_data_url_when_model_can_see():
    items = _turns_to_responses_input([Turn(role="user", content=[_block((_IMG,))])], can_see=True)
    output = items[0]["output"]
    assert output[0] == {"type": "input_text", "text": "Image: a.png"}
    assert output[1]["type"] == "input_image"
    expected = "data:image/png;base64," + base64.b64encode(_IMG.data).decode()
    assert output[1]["image_url"] == expected


def test_responses_omits_images_for_blind_model():
    items = _turns_to_responses_input([Turn(role="user", content=[_block((_IMG,))])])
    output = items[0]["output"]
    assert [p["type"] for p in output] == ["input_text"]
    assert IMAGE_UNSUPPORTED_NOTE in output[0]["text"]


def test_ollama_never_sends_images():
    msgs = _turns_to_ollama_messages([Turn(role="user", content=[_block((_IMG,))])], system="s")
    tool_msg = msgs[1]
    assert tool_msg["role"] == "tool"
    assert "images" not in tool_msg
    assert IMAGE_UNSUPPORTED_NOTE in tool_msg["content"]


def test_catalog_vision_flags():
    assert DEFAULT_MODELS["bedrock-claude-sonnet-4-6"].can_see
    assert DEFAULT_MODELS["bedrock-openai-gpt-6-1-sol"].can_see
    assert DEFAULT_MODELS["bedrock-kimi-k3"].can_see
    assert not DEFAULT_MODELS["bedrock-glm-5"].can_see
    assert not DEFAULT_MODELS["bedrock-qwen3-coder-next"].can_see
    assert not any(m.can_see for k, m in DEFAULT_MODELS.items() if k.startswith("ollama-"))


# --- Persisted events ---


def test_tool_result_event_records_image_metadata_not_bytes(tmp_path):
    model = ModelEntry(name="m", context=1000, provider=BedrockProvider(model_id="m"))
    factory = EventFactory(tmp_path, model_key="m", model=model)
    event = factory.tool_result(
        turn=1,
        iteration=1,
        request_id="r",
        tool_use_id="t1",
        content="Image: a.png",
        is_error=False,
        duration_ms=1,
        result_bytes=12,
        images=(_IMG,),
    )
    line = encode_event(event)
    payload = json.loads(line)
    assert payload["images"] == [
        {"format": "png", "width": 2, "height": 1, "bytes": len(_IMG.data), "source": "a.png"}
    ]
    assert base64.b64encode(_IMG.data).decode() not in line
    assert decode_event(line, persisted=True) == event


def test_old_tool_result_decodes_with_empty_images():
    raw = (
        '{"type":"tool_result","id":"01J00000000000000000000002",'
        '"turn":1,"iteration":1,"scope":null,"request_id":"req",'
        '"tool_use_id":"tool","content":"old","is_error":false}'
    )
    event = decode_event(raw, persisted=True)
    assert isinstance(event, ToolResult)
    assert event.images == []


# --- Harness end-to-end ---


async def test_harness_native_read_image_reaches_transcript(tmp_path, workspace):
    (workspace / "a.png").write_bytes(_png())
    responses = [
        [
            ToolUseEvent(tool_use_id="tu_1", name="read", input={"path": "a.png"}),
            Usage(input_tokens=10, output_tokens=5),
            Done(stop_reason="tool_use"),
        ],
        [TextDelta(text="A red box."), Usage(input_tokens=20, output_tokens=5), Done("end_turn")],
    ]
    model = ModelEntry(name="m", context=1000, provider=BedrockProvider(model_id="m"))
    harness = AgentHarness(
        session=Session(model_id="m", model=model, session_id="img-1"),
        llm_client=FakeLLMClient(responses=responses),
        model_name="m",
        log_dir=tmp_path / "logs",
    )
    await harness.handle_message("look at a.png")

    results = [
        b for t in harness.session.turns for b in t.content if isinstance(b, ToolResultBlock)
    ]
    assert len(results) == 1
    assert not results[0].is_error
    assert results[0].content.startswith("Image: a.png")
    assert len(results[0].images) == 1
    assert results[0].images[0].format == "png"

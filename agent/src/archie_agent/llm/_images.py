"""Shared helpers for translating tool-result images to provider formats."""

import base64

from archie_shared.types import ImageBlock, ToolResultBlock

IMAGE_UNSUPPORTED_NOTE = (
    "[Image content omitted: the current model does not support image input. "
    "Only the description above is available.]"
)


def text_with_image_note(block: ToolResultBlock) -> str:
    """Tool-result text for providers/models that cannot receive images."""
    if not block.images:
        return block.content
    return f"{block.content}\n{IMAGE_UNSUPPORTED_NOTE}" if block.content else IMAGE_UNSUPPORTED_NOTE


def data_url(image: ImageBlock) -> str:
    """Encode an image as a base64 data URL."""
    return f"data:{image.media_type};base64,{base64.b64encode(image.data).decode('ascii')}"

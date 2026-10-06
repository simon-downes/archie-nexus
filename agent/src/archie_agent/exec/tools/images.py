"""Image loading for model input — format detection, resizing, re-encoding.

Images are downscaled to a model-friendly size rather than sent at whatever
resolution the provider will accept: vision models internally resample large
images anyway, so oversized inputs only cost tokens and latency.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from archie_shared.types import ImageBlock

from archie_agent.exec.tools import ToolError

# Long-edge cap. Claude resamples anything beyond ~1568px and OpenAI "high"
# detail tiles at 2048/768; 1568 is a good fit for both.
MAX_LONG_EDGE = 1568
# Total-pixel cap (~1.15 megapixels) — keeps very wide/tall images bounded too.
MAX_PIXELS = 1_200_000
# Hard limits on what we will open at all.
MAX_SOURCE_BYTES = 50 * 1024 * 1024
# Encoded output budget (Bedrock Converse rejects images over 3.75 MB).
MAX_ENCODED_BYTES = 3_750_000

# Magic-byte signatures → canonical format name.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


class ImageError(ToolError):
    """Image could not be decoded or reduced to a sendable size."""


@dataclass(frozen=True)
class ToolImage:
    """A tool return value carrying an image plus a text caption.

    Native tool handlers pass this through to the harness, which turns it into a
    ToolResultBlock with ``content=caption`` and ``images=(image,)``. Inside exec
    (no image channel) it degrades to its caption via ``__str__``.
    """

    image: ImageBlock
    caption: str

    def __str__(self) -> str:
        return self.caption


def detect_image_format(head: bytes) -> str | None:
    """Return the image format for a file header, or None if not a supported image."""
    for sig, fmt in _SIGNATURES:
        if head.startswith(sig):
            return fmt
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def _target_size(width: int, height: int) -> tuple[int, int]:
    scale = min(1.0, MAX_LONG_EDGE / max(width, height), (MAX_PIXELS / (width * height)) ** 0.5)
    if scale >= 1.0:
        return width, height
    # Floor so rounding never pushes the result over either cap.
    return max(1, int(width * scale)), max(1, int(height * scale))


def load_image(path: Path, display_path: str) -> ToolImage:
    """Load an image file, resize it for model input, and return a ToolImage.

    Small images in a supported format are passed through byte-for-byte. Larger
    images are downscaled (Lanczos) and re-encoded: PNG when the image has
    transparency, otherwise JPEG. Animated GIF/WebP are reduced to the first frame.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    size = path.stat().st_size
    if size > MAX_SOURCE_BYTES:
        raise ImageError(f"Image too large to load ({size} bytes): {display_path}")

    raw = path.read_bytes()
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise ImageError(f"Cannot decode image {display_path}: {e}") from e

    src_format = (img.format or "").lower()
    animated = getattr(img, "is_animated", False)
    # Apply EXIF orientation so the model sees the image upright.
    # (exif_transpose always returns a copy, so check the tag explicitly.)
    rotated = img.getexif().get(0x0112, 1) != 1
    if rotated:
        img = ImageOps.exif_transpose(img)
    width, height = img.size
    orig_w, orig_h = width, height
    target = _target_size(width, height)

    passthrough = (
        src_format in {"png", "jpeg", "gif", "webp"}
        and target == (width, height)
        and not animated
        and not rotated
        and len(raw) <= MAX_ENCODED_BYTES
    )
    if passthrough:
        data, fmt = raw, src_format
    else:
        if target != (width, height):
            img = img.resize(target, Image.Resampling.LANCZOS)
        data, fmt = _encode(img)
        if len(data) > MAX_ENCODED_BYTES:
            raise ImageError(f"Image could not be reduced below {MAX_ENCODED_BYTES} bytes")

    w, h = img.size
    resized = (w, h) != (orig_w, orig_h)
    note = f", resized from {orig_w}x{orig_h}" if resized else ""
    caption = f"Image: {display_path} ({fmt.upper()}, {w}x{h}{note}, {len(data)} bytes)"
    return ToolImage(
        image=ImageBlock(format=fmt, data=data, width=w, height=h, source=display_path),
        caption=caption,
    )


def _encode(img) -> tuple[bytes, str]:
    """Encode as PNG if the image has alpha, else JPEG."""
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    buf = io.BytesIO()
    if has_alpha:
        img.convert("RGBA").save(buf, format="PNG", optimize=True)
        return buf.getvalue(), "png"
    img.convert("RGB").save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue(), "jpeg"

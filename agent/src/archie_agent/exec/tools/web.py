"""Web tools — web_fetch and web_search.

These run inside the Docker sandbox. Network access works via Docker default networking.
Uses httpx for HTTP fetching and ddgs (DuckDuckGo) for search.

Imports are deferred inside functions to avoid startup cost.
"""

from __future__ import annotations

import asyncio
import re
from typing import Literal

from archie_agent.exec.tools import ContentTypeError, tool
from archie_agent.exec.tools.images import ToolImage, detect_image_format, image_from_bytes


@tool
async def web_fetch(
    url: str, mode: str = "selective", search_terms: str | None = None
) -> str | ToolImage:
    """Fetch content from a web URL. Images (PNG, JPEG, GIF, WebP) are returned as images.

    Args:
        url: The http(s) URL to fetch.
        mode: Extraction mode:
            - 'selective' (default): extract lines around search_terms matches.
              Falls back to first 8000 chars if no matches or no search_terms.
            - 'truncated': return first 8000 characters.
            - 'full': return complete content.
        search_terms: Keywords for selective mode (space-separated terms).

    Returns:
        Page content as cleaned text. Text is recognised from the content type
        (including +json/+xml types such as SVG); bodies with no type or
        application/octet-stream are treated as text if they have no null bytes.
        For image URLs, an image (downscaled for model input) with a short
        caption; mode/search_terms are ignored.
        Inside exec only the caption is returned — call the native `web_fetch`
        tool to view an image.

    Raises:
        ConnectionError: Network failure, DNS error, or non-2xx status.
        TimeoutError: Request exceeded 30 seconds.
        ContentTypeError: URL returned binary content that is not a supported image.
        ImageError: Image could not be decoded or reduced to a sendable size.
    """
    import httpx

    if not url:
        raise ConnectionError("URL must not be empty")

    if not url.startswith(("http://", "https://")):
        raise ConnectionError(f"Unsupported URL scheme: {url}")

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=30.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Archie/1.0)"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.TimeoutException as e:
        raise TimeoutError(f"Request timed out after 30s: {e}") from e
    except httpx.ConnectError as e:
        raise ConnectionError(f"Connection failed: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ConnectionError(f"HTTP {e.response.status_code}: {e}") from e
    except httpx.HTTPError as e:
        raise ConnectionError(f"HTTP error: {e}") from e

    # Classify by content type. Text types are trusted. Binary and generic
    # types are sniffed for a supported image format (the same check `read`
    # uses); generic types with no null bytes are treated as text, as `read`
    # treats files. Anything else binary is rejected.
    content_type = response.headers.get("content-type", "")
    kind = _classify_content_type(content_type)
    if kind != "text":
        head = response.content[:_SNIFF_BYTES]
        if detect_image_format(head) is not None:
            return await asyncio.to_thread(image_from_bytes, response.content, url)
        if kind == "binary" or b"\x00" in head:
            raise ContentTypeError(
                f"Non-text content type: {content_type or 'none'}. "
                "web_fetch supports text and PNG, JPEG, GIF and WebP images."
            )

    # Decode text content
    text = response.text

    # For HTML, strip to readable text
    if "text/html" in content_type:
        text = _html_to_text(text)

    # Apply mode
    if mode == "truncated":
        text_content = text[:8000]
    elif mode == "full":
        text_content = text
    else:
        # selective mode
        if search_terms:
            text_content = _extract_selective(text, search_terms)
        else:
            # No search terms — fall back to truncated
            text_content = text[:8000]

    # Prepend metadata header
    size = len(text_content)
    ct_short = content_type.split(";")[0].strip() if content_type else "unknown"
    header = f"URL: {url}\nType: {ct_short} · {size} chars\n\n"
    return header + text_content


@tool
async def web_search(query: str) -> str:
    """Search the web using DuckDuckGo.

    Args:
        query: Search query string.

    Returns:
        Formatted numbered results with title, URL, and snippet.
        Returns "No results found." if no results or on error.
    """
    if not query or not query.strip():
        return "No results found."

    from ddgs import DDGS

    try:
        results = DDGS(timeout=10).text(
            query.strip(), safesearch="off", backend="auto", max_results=8
        )
    except Exception:
        return "No results found."

    if not results:
        return "No results found."

    lines: list[str] = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "(no title)")
        url = r.get("href", "")
        snippet = r.get("body", "")
        lines.append(f"{i}. {title}\n   {url}\n   {snippet}")

    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


# Bytes inspected when sniffing a response body (matches `read`).
_SNIFF_BYTES = 8192

# Text media types beyond text/* and the application/json, application/xml and
# application/javascript prefixes (structured suffixes are handled separately).
_TEXT_PREFIXES = ("text/", "application/json", "application/xml", "application/javascript")
_TEXT_TYPES = frozenset(
    {
        "application/x-javascript",
        "application/ecmascript",
        "application/x-yaml",
        "application/yaml",
        "application/toml",
        "application/x-ndjson",
        "application/x-sh",
        "application/x-shellscript",
        "application/sql",
        "application/graphql",
    }
)

# Structured-syntax suffixes (RFC 6839) that mark a text type, e.g.
# application/problem+json, application/atom+xml, image/svg+xml.
_TEXT_SUFFIXES = ("+json", "+xml", "+yaml")

# Types that say nothing about the body; these are sniffed like `read` does.
_GENERIC_TYPES = frozenset(
    {"", "application/octet-stream", "binary/octet-stream", "application/unknown"}
)


def _classify_content_type(content_type: str) -> Literal["text", "binary", "generic"]:
    """Classify a content-type header as text, binary, or generic (needs sniffing)."""
    ct = content_type.lower().split(";")[0].strip()
    if ct.startswith(_TEXT_PREFIXES) or ct in _TEXT_TYPES or ct.endswith(_TEXT_SUFFIXES):
        return "text"
    if ct in _GENERIC_TYPES:
        return "generic"
    return "binary"


def _html_to_text(html: str) -> str:
    """Basic HTML to readable text conversion (strip tags, decode entities)."""
    import html as html_module

    # Remove script and style blocks
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Remove nav, header, footer
    text = re.sub(r"<(nav|header|footer)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Convert block elements to newlines
    text = re.sub(r"<(p|div|br|h[1-6]|li|tr)[^>]*>", "\n", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode HTML entities
    text = html_module.unescape(text)
    # Collapse whitespace
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def _extract_selective(text: str, search_terms: str) -> str:
    """Extract lines around search term matches (~10 lines before and after)."""
    terms = search_terms.lower().split()
    if not terms:
        return text[:8000]

    lines = text.splitlines()
    matching_indices: set[int] = set()

    for i, line in enumerate(lines):
        line_lower = line.lower()
        if any(term in line_lower for term in terms):
            matching_indices.add(i)

    if not matching_indices:
        # No matches — return truncated
        return text[:8000]

    # Expand each match by 10 lines in each direction
    include_indices: set[int] = set()
    for idx in matching_indices:
        start = max(0, idx - 10)
        end = min(len(lines), idx + 11)
        for i in range(start, end):
            include_indices.add(i)

    # Build output with section markers
    sorted_indices = sorted(include_indices)
    output_lines: list[str] = []
    prev_idx = -2

    for idx in sorted_indices:
        if idx > prev_idx + 1:
            if output_lines:
                output_lines.append("...")
        output_lines.append(lines[idx])
        prev_idx = idx

    return "\n".join(output_lines)
